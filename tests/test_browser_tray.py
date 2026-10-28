"""The widget page's side of the native window's tray (spec 024): the ⚙ "Tray" section, the badge
count it publishes and the notifications it raises, against a fake board and a stubbed bridge. Synthetic
data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_tray.py`.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import browser_helpers
import pytest
from browser_helpers import (  # skips without playwright
    ORIGIN,
    SOCKET_DOWN,
    Page,
    Pages,
    open_fake,
    row,
    until,
)

from miniteams import web as W

pytestmark = [pytest.mark.browser, pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)]
pages = browser_helpers.pages

PAGE = W._page("widget.html")
A, B, C, D, E = (f"19:t{x}@thread.v2" for x in "abcde")
MENTION = {"kind": "me", "by": "Ann Lee", "text": "hi", "at": "2026-10-09T10:00:00.000Z", "edited": False}
ROWS = [
    row(A, "Fictional A", unread=True, last_id="1"),
    row(B, "Fictional B", unread=True, mention=MENTION, last_id="1"),
    row(C, "Fictional C", unread=True, muted=True, last_id="1"),
    row(D, "Fictional D", seen_at="2026-10-09T11:00:00.000Z", last_id="1"),
    row(E, "Fictional E", unread=True, muted=True, mention=MENTION, last_id="1"),
]
# What the native window injects (widget_qt.BOOT), stubbed: every badge() call lands in window.__badges,
# every notify() in window.__notes; window.__opened is what a notification's click calls.
BRIDGE = """window.__badges = []; window.__notes = [];
window.miniteamsTray = Promise.resolve({ badge: (pref, n) => window.__badges.push([pref, n]),
  notify: (...a) => window.__notes.push(a),
  settings: { connect: (f) => { window.__settings = f; } },
  opened: { connect: (f) => { window.__opened = f; } } });"""
LAST = "JSON.stringify(window.__badges.at(-1))"
TABS = """Object.fromEntries([...document.querySelectorAll('#tabs button')]
  .map(b => [b.firstChild.textContent, +b.querySelector('b').textContent]))"""
MENU = "[...document.querySelectorAll('.menu h3')].map(h => h.textContent)"


class Fake(browser_helpers.Fake):
    quiet: bool = False  # a reconnection gets no board frame until the test sends one
    sockets: int = 0
    first: dict[str, Any] | None = None  # merged into the first frame of every connection

    def attach(self, ws: Any) -> None:
        super().attach(ws)
        self.sockets += 1
        if not self.quiet:
            self.send(
                {"rows": ROWS, "page": "1", "opener": False, "browser": False, "me": "Me"}
                | (self.first or {})
            )


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
    assert await page.evaluate(MENU) == [
        "Theme",
        "Display",
        "Click opens",
        "Middle click opens",
        "Chat navigation",
    ]
    assert await page.locator("#tray").count() == 0
    assert await page.evaluate("'__badges' in window") is False
    await pages.close()


async def test_tray_section_shows_in_the_native_window(native: Fake) -> None:
    """024-AC5: with the bridge, ⚙ gains its Tray section, Unread chosen by default."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    assert await page.evaluate(MENU) == [
        "Theme",
        "Display",
        "Click opens",
        "Middle click opens",
        "Chat navigation",
        "Tray",
        "Notify",
    ]
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


# --- notifications (024-AC10, AC11) ---

NOTES = "JSON.stringify(window.__notes)"
LATER = "2026-10-09T12:00:00.000Z"


class Board:
    """The fake board's rows across broadcasts: each change returns the whole list, as the server sends it."""

    def __init__(self) -> None:
        self.rows = {r["id"]: dict(r) for r in ROWS}

    def change(self, thread_id: str, **over: Any) -> list[dict[str, Any]]:
        self.rows[thread_id] = self.rows[thread_id] | over  # a new dict: frames built earlier keep theirs
        return list(self.rows.values())

    def said(self, thread_id: str, msg: str, sender: str = "Bob Roe", **over: Any) -> list[dict[str, Any]]:
        """`thread_id` got message `msg`."""
        said = {"last_id": msg, "last_activity": LATER, "sender": sender, "text": f"text {msg}"}
        return self.change(thread_id, **said | over)


def _at_me(msg: str, by: str = "Bob Roe") -> dict[str, Any]:
    return MENTION | {"by": by, "text": f"hey {msg}", "at": LATER, "msg_id": msg}


async def _notes(fake: Fake, *frames: list[dict[str, Any]]) -> list[list[str]]:
    """Every notification raised so far, once the page handled `frames`, each a board broadcast."""
    assert fake.page is not None
    for rows in frames:
        fake.send({"rows": rows})
    await fake.handled()
    notes: list[list[str]] = await fake.page.evaluate("window.__notes")
    return notes


async def _choose(page: Page, label: str) -> None:
    await page.locator("#gear").click()
    await page.locator("#notify button", has_text=re.compile(f"^{re.escape(label)}$")).click()
    await page.keyboard.press("Escape")


async def test_notify_section_defaults_to_mentions_and_is_kept(native: Fake) -> None:
    """024-AC10: ⚙ Tray has the notification choice, mentions by default; a choice survives a reload."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    assert await page.locator("#notify button").all_text_contents() == ["@", "Messages", "None"]
    assert await page.locator("#notify button.on").text_content() == "@"
    await page.locator("#notify button", has_text="Messages").click()
    assert await page.evaluate("localStorage.getItem('notify')") == "all"
    await page.reload()
    await page.wait_for_selector("a.name")
    await page.locator("#gear").click()
    assert await page.locator("#notify button.on").text_content() == "Messages"


async def test_mentions_notify_a_new_mention_once(native: Fake) -> None:
    """024-AC10: on mentions, a mention appearing raises one notification (sender, chat, text), also in a
    muted chat (the @ tab's rule); a plain message, my own mention and the same mention again do not."""
    b = Board()
    assert await _notes(native, b.said(A, "2")) == []
    assert await _notes(native, b.change(A, mention=_at_me("m1"))) == [[A, "Bob Roe · Fictional A", "hey m1"]]
    # Broadcast again, cleared, then back (an edit re-adding it): still one.
    again = (b.change(A, mention=_at_me("m1")), b.change(A, mention=None), b.change(A, mention=_at_me("m1")))
    assert len(await _notes(native, *again)) == 1
    assert len(await _notes(native, b.change(C, mention=_at_me("m2")))) == 2  # muted, mentioned
    assert len(await _notes(native, b.change(D, mention=_at_me("m3", by="You")))) == 2


async def test_every_message_notifies_others_messages_in_unmuted_chats(native: Fake) -> None:
    """024-AC10: on every message, a new message notifies; not in a muted chat, not my own, not an
    activity line (no sender), not one older than the chat's Seen mark; a mention with its message, once."""
    page = native.page
    assert page is not None
    await _choose(page, "Messages")
    b = Board()
    assert await _notes(native, b.said(A, "2")) == [[A, "Bob Roe · Fictional A", "text 2"]]
    assert len(await _notes(native, b.said(C, "3"))) == 1  # muted
    assert len(await _notes(native, b.said(A, "4", sender="You"))) == 1
    assert len(await _notes(native, b.said(A, "5", sender=""))) == 1
    early = b.change(D, last_id="6", last_activity="2026-10-09T10:30:00.000Z", sender="Bob Roe")
    assert len(await _notes(native, early)) == 1  # still Seen
    assert len(await _notes(native, b.said(D, "7"))) == 2  # newer than the mark: back in Inbox
    notes = await _notes(native, b.said(B, "8", mention=_at_me("8")), b.change(B, text="edited"))
    assert notes[2:] == [[B, "Bob Roe · Fictional B", "hey 8"]]


async def test_a_direct_chat_names_its_sender_once_and_text_is_clipped(native: Fake) -> None:
    """024-AC10: a sender who is the chat's name is not repeated; a long text is cut short."""
    page = native.page
    assert page is not None
    await _choose(page, "Messages")
    notes = await _notes(native, Board().said(A, "2", sender="Fictional A", text="x" * 500))
    assert notes == [[A, "Fictional A", "x" * 199 + "…"]]


async def test_none_notifies_nothing(native: Fake) -> None:
    """024-AC10: with none, nothing, mentions included."""
    page = native.page
    assert page is not None
    await _choose(page, "None")
    b = Board()
    assert await _notes(native, b.said(A, "2"), b.change(B, mention=_at_me("m1"))) == []


async def test_no_notification_for_a_snapshot_or_a_resync(native: Fake) -> None:
    """024-AC10: the first board of a connection only sets what the next one is compared with: a
    reconnection's board, newer messages and mentions in it, raises nothing; the change after it does."""
    page = native.page
    assert page is not None
    await _choose(page, "Messages")
    assert await page.evaluate(NOTES) == "[]"
    b = Board()
    native.first = {"rows": b.said(A, "2", mention=_at_me("m1"))}
    await native.ws.close()
    await browser_helpers.wait(lambda: native.sockets == 2, "reconnection")
    assert await _notes(native) == []
    assert await _notes(native, b.said(A, "3")) == [[A, "Bob Roe · Fictional A", "text 3"]]


async def test_a_seeding_board_publishes_no_count_and_raises_nothing(pages: Pages) -> None:
    """024-AC4, AC10: while the server still seeds (`busy`), no count is published, not even 0; the seeded
    board publishes it and raises nothing for what seeding brought; the change after it notifies."""
    fake = Fake()
    fake.first = {"rows": [], "busy": True}
    page = await open_fake(
        pages,
        fake,
        ORIGIN + "/",
        PAGE,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
        init=BRIDGE,
    )
    await until(page, "typeof window.__opened === 'function'")
    b = Board()
    fake.send({"rows": list(b.rows.values()), "busy": True})
    fake.send({"rows": b.said(A, "2", mention=_at_me("m1")), "busy": True})
    await fake.handled()
    assert await page.evaluate("window.__badges.every(([p, n]) => p === 'none' && n === 0)")
    fake.send({"rows": list(b.rows.values()), "busy": False})
    await _published(page, "unread", 2)
    assert await page.evaluate(NOTES) == "[]"
    fake.send({"rows": b.change(D, mention=_at_me("m9"))})
    await until(page, "window.__notes.length === 1")
    await pages.close()


async def test_a_click_opens_the_chat_as_its_row_does(pages: Pages) -> None:
    """024-AC11: the window's `opened(chat)` (a notification's Open) does what a plain click on the row
    does: the server's `open` with an opener, the row's link without one."""
    fake = Fake()
    fake.first = {"opener": True}
    page = await _open(
        pages, fake, BRIDGE + "window.__wins = []; window.open = (...a) => window.__wins.push(a);"
    )
    await until(page, "typeof window.__opened === 'function'")
    await page.locator(f"li[data-id='{A}'] a.name").click()
    await fake.settled()
    row_click = fake.of("open")
    assert row_click == [{"open": A, "web": False}]
    await page.evaluate(f"window.__opened({A!r})")
    await fake.settled()
    assert fake.of("open") == row_click * 2
    await page.evaluate("window.__opened('19:gone@thread.v2')")  # off the board since: nothing
    fake.send({"opener": False, "rows": Board().change(B, link="https://teams.microsoft.com/l/chat/b")})
    await fake.handled()
    await page.evaluate(f"window.__opened({B!r})")
    assert await page.evaluate("window.__wins") == [
        ["https://teams.microsoft.com/l/chat/b", "_blank", "noopener"]
    ]
    await fake.settled()
    assert fake.of("open") == row_click * 2
    await pages.close()
    assert pages.external == []


@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
@pytest.mark.parametrize("click", ["teams", "chat", "app"])
async def test_a_click_follows_the_click_setting(pages: Pages, click: str, browser: bool) -> None:
    """035-AC6: a notification's Open follows ⚙ Click opens, as a plain click on the row does: Teams web or
    the chat page, through --browser when set, else a new window; `app` without an opener is Teams web."""
    fake = Fake()
    fake.first = {"opener": click != "app", "browser": browser}
    stub = "window.__wins = []; window.open = (...a) => window.__wins.push(a);"
    init = f"localStorage.setItem('click', {click!r}); {stub}"
    page = await _open(pages, fake, BRIDGE + init)
    await until(page, "typeof window.__opened === 'function'")
    fake.send({"rows": Board().change(A, link="https://teams.microsoft.com/l/chat/a")})
    await fake.handled()
    await page.evaluate(f"window.__opened({A!r})")
    await fake.settled()
    if browser:
        assert fake.of("open") == [{"open": A, "chat": True} if click == "chat" else {"open": A, "web": True}]
        assert await page.evaluate("window.__wins") == []
    else:
        assert fake.of("open") == []
        url = "/chat/19%3Ata%40thread.v2" if click == "chat" else "https://teams.microsoft.com/l/chat/a"
        assert await page.evaluate("window.__wins") == [[url, "_blank", "noopener"]]
    await pages.close()
    assert pages.external == []


async def test_a_click_with_the_socket_down_opens_the_row_link(pages: Pages) -> None:
    """035-AC6: a notification's Open while the socket is down opens the row's link, not nothing."""
    fake = Fake()
    fake.first = {"opener": True, "browser": True}
    stub = "window.__wins = []; window.open = (...a) => window.__wins.push(a);"
    page = await _open(pages, fake, BRIDGE + stub)
    await until(page, "typeof window.__opened === 'function'")
    fake.send({"rows": Board().change(A, link="https://teams.microsoft.com/l/chat/a")})
    await fake.handled()
    await page.evaluate(SOCKET_DOWN)
    await page.evaluate(f"window.__opened({A!r})")
    assert await page.evaluate("window.__wins") == [
        ["https://teams.microsoft.com/l/chat/a", "_blank", "noopener"]
    ]
    assert fake.of("open") == []
    await pages.close()
    assert pages.external == []


async def test_remembered_notifications_are_capped(native: Fake) -> None:
    """The page runs for days: it remembers exactly the last 500 notified messages. Past that the oldest is
    forgotten (its mention back notifies again); the second oldest is still remembered."""
    many = [row(f"19:m{i}@thread.v2", f"Fictional M{i}", last_id="1") for i in range(501)]
    mentioned = [r | {"mention": _at_me(f"x{i}")} for i, r in enumerate(many)]
    assert len(await _notes(native, ROWS + many, ROWS + mentioned)) == 501

    def without(i: int) -> list[dict[str, Any]]:
        return ROWS + [r | {"mention": None} if j == i else r for j, r in enumerate(mentioned)]

    assert len(await _notes(native, without(1), ROWS + mentioned)) == 501  # 500 kept: m1 is one of them
    notes = await _notes(native, without(0), ROWS + mentioned)
    assert [n[0] for n in notes[501:]] == ["19:m0@thread.v2"]  # 501 would have kept m0
