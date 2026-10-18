"""The widget page's side of the native window's tray (spec 024): the ⚙ "Tray" section and the badge
count it publishes, against a fake board and a stubbed bridge. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_tray.py`.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import browser_helpers
import pytest
from browser_helpers import ORIGIN, Page, Pages, open_fake, row, until  # skips without playwright

from miniteams import web as W

pytestmark = [pytest.mark.browser, pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)]
pages = browser_helpers.pages

PAGE = W._page("widget.html")
A, B, C, D, E = (f"19:t{x}@thread.v2" for x in "abcde")
MENTION = {"kind": "me", "by": "Ann Lee", "text": "hi", "at": "2026-10-09T10:00:00.000Z", "edited": False}
ROWS = [
    row(A, "Fictional A", unread=True),
    row(B, "Fictional B", unread=True, mention=MENTION),
    row(C, "Fictional C", unread=True, muted=True),
    row(D, "Fictional D", seen_at="2026-10-09T11:00:00.000Z"),
    row(E, "Fictional E", unread=True, muted=True, mention=MENTION),
]
# What the native window injects (widget_qt.BOOT), stubbed: every badge() call lands in window.__badges.
BRIDGE = """window.__badges = [];
window.miniteamsTray = Promise.resolve({ badge: (pref, n) => window.__badges.push([pref, n]),
  settings: { connect: (f) => { window.__settings = f; } } });"""
LAST = "JSON.stringify(window.__badges.at(-1))"
TABS = """Object.fromEntries([...document.querySelectorAll('#tabs button')]
  .map(b => [b.firstChild.textContent, +b.querySelector('b').textContent]))"""
MENU = "[...document.querySelectorAll('.menu h3')].map(h => h.textContent)"


class Fake(browser_helpers.Fake):
    quiet: bool = False  # a reconnection gets no board frame until the test sends one
    sockets: int = 0

    def attach(self, ws: Any) -> None:
        super().attach(ws)
        self.sockets += 1
        if not self.quiet:
            self.send({"rows": ROWS, "page": "1", "opener": False, "browser": False, "me": "Me"})


async def _open(pages: Pages, fake: Fake, init: str = "") -> Page:
    page = await open_fake(
        pages,
        fake,
        ORIGIN + "/",
        PAGE,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
        init=init,
    )
    await page.wait_for_selector("a.name")
    return page


@pytest.fixture
async def native(pages: Pages) -> AsyncIterator[Fake]:
    fake = Fake()
    await _open(pages, fake, BRIDGE)
    yield fake
    await pages.close()
    assert pages.external == []


async def _published(page: Page, pref: str, n: int) -> None:
    await until(page, f"{LAST} === JSON.stringify([{pref!r}, {n}])")


async def test_without_the_bridge_the_page_is_unchanged(pages: Pages) -> None:
    """024-AC5: in a browser ⚙ has no Tray section and nothing is published."""
    fake = Fake()
    page = await _open(pages, fake)
    await page.locator("#gear").click()
    assert await page.evaluate(MENU) == ["Theme", "Display", "Middle click", "Chat navigation"]
    assert await page.locator("#tray").count() == 0
    assert await page.evaluate("'__badges' in window") is False
    await pages.close()


async def test_tray_section_shows_in_the_native_window(native: Fake) -> None:
    """024-AC5: with the bridge, ⚙ gains its Tray section, Unread chosen by default."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    assert await page.evaluate(MENU) == ["Theme", "Display", "Middle click", "Chat navigation", "Tray"]
    buttons = page.locator("#tray button")
    assert await buttons.all_text_contents() == ["Unread", "@", "Inbox", "None"]
    assert await page.locator("#tray button.on").text_content() == "Unread"


async def test_settings_from_the_tray_opens_the_section(native: Fake) -> None:
    """024-AC5: tray → Settings… (the bridge's `settings` signal) opens ⚙ on the Tray section."""
    page = native.page
    assert page is not None
    await until(page, "typeof window.__settings === 'function'")
    assert await page.locator(".menu").count() == 0
    await page.evaluate("window.__settings()")
    await page.locator(".menu #tray").wait_for()


async def test_badge_counts_each_tab_rule_without_the_filter(native: Fake) -> None:
    """024-AC4: each preference publishes its tab's count with an empty filter box (muted rules
    included); typing in the filter changes the tabs, not the badge; the choice is kept."""
    page = native.page
    assert page is not None
    tabs = await page.evaluate(TABS)
    assert tabs == {"Inbox": 3, "@": 2, "Unread": 2, "Seen": 1, "Muted": 2}
    await _published(page, "unread", tabs["Unread"])
    await page.locator("#gear").click()
    for label, pref in (("@", "mention"), ("Inbox", "all"), ("None", "none"), ("Unread", "unread")):
        await page.locator("#tray button", has_text=re.compile(f"^{re.escape(label)}$")).click()
        await _published(page, pref, 0 if pref == "none" else tabs[label])
        assert await page.evaluate("localStorage.getItem('badge')") == pref
    await page.locator("#tray button", has_text="Inbox").click()
    n = await page.evaluate("window.__badges.length")
    await page.locator("#q").fill("Fictional A")
    assert (await page.evaluate(TABS))["Inbox"] == 1
    native.send({"rows": ROWS})  # a broadcast while filtered still counts the whole tab
    await native.handled()
    assert await page.evaluate(f"window.__badges.slice({n}).every(([p, c]) => p === 'all' && c === 3)")
    await page.reload()
    await page.wait_for_selector("a.name")
    await _published(page, "all", 3)


async def test_badge_follows_board_broadcasts(native: Fake) -> None:
    """024-AC4: a new board frame publishes its new count."""
    page = native.page
    assert page is not None
    await _published(page, "unread", 2)
    native.send({"rows": [*ROWS, row("19:tf@thread.v2", "Fictional F", unread=True)]})
    await _published(page, "unread", 3)
    native.send({"rows": [r | {"unread": False} for r in ROWS]})
    await _published(page, "unread", 0)


async def test_a_lost_socket_clears_the_badge_until_the_board_is_back(native: Fake) -> None:
    """024-AC4: disconnected, the page publishes no count (a stale one would read as live); the board
    frame of the reconnection publishes it again."""
    page = native.page
    assert page is not None
    await _published(page, "unread", 2)
    await native.ws.close()
    await _published(page, "none", 0)
    await _published(page, "unread", 2)


async def test_a_choice_made_while_disconnected_publishes_no_count(native: Fake) -> None:
    """024-AC4: picking another count while the socket is down still shows none; the next board frame
    publishes the new choice."""
    page = native.page
    assert page is not None
    await _published(page, "unread", 2)
    native.quiet = True
    await native.ws.close()
    await _published(page, "none", 0)
    n = await page.evaluate("window.__badges.length")
    await page.locator("#gear").click()
    await page.locator("#tray button", has_text="Inbox").click()
    await until(page, f"window.__badges.length > {n}")
    assert await page.evaluate(LAST) == '["none",0]'
    assert await page.evaluate("localStorage.getItem('badge')") == "all"
    await browser_helpers.wait(lambda: native.sockets == 2, "reconnection")
    assert await page.evaluate(LAST) == '["none",0]'  # connected, but no board yet
    native.send({"rows": ROWS})
    await _published(page, "all", 3)
