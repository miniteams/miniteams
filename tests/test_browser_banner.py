"""Connection banner in a headless browser (spec 036): page socket down, Teams link down, sign-in expired.

Run: `uv run --with playwright pytest tests/test_browser_banner.py`. Synthetic data, no network."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import browser_helpers
import pytest
import test_web_chat
from browser_helpers import Page, Pages, rendered, until  # skips without playwright
from playwright.async_api import BrowserContext
from test_web_chat import T, _free_port, _served

from miniteams import web as W
from miniteams.config import Settings

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, data_dir, quiet_directory = test_web_chat.board, test_web_chat.data_dir, test_web_chat.quiet_directory

PATHS = ("/", f"/chat/{T}", f"/chat/{T}?chatnav=split")
# Page clock and text of every banner change (__log, "" = emptied) and of every shown text (__shown), from
# the first paint (a flash on load counts); when the latest socket opened (__opened), closed (__closed),
# got its latest link frame (__linkAt).
SHOWN = """document.addEventListener("DOMContentLoaded", () => {
  const b = document.getElementById("banner"); window.__shown = []; window.__log = [];
  const note = () => { const t = [performance.now(), b.textContent];
    window.__log.push(t); if (t[1]) window.__shown.push(t); };
  if (b.textContent) note();
  new MutationObserver(note).observe(b, { childList: true, subtree: true });
});
window.WebSocket = class extends WebSocket { constructor(...a) { super(...a);
  this.addEventListener("open", () => { window.__opened = performance.now(); });
  this.addEventListener("close", () => { window.__closed ??= performance.now(); });  // the first drop
  this.addEventListener("message", (e) => {
    if ("link" in JSON.parse(e.data)) window.__linkAt = performance.now(); }); } };"""
TEXT = "document.getElementById('banner').textContent"
VISIBLE = f"{TEXT} !== ''"
HIDDEN = f"{TEXT} === ''"
DOWN = "Teams connection lost, reconnecting…"
LOGIN = "Teams sign-in expired: run miniteams login"
GONE = "Disconnected from miniteams, reconnecting…"


async def _watch(context: BrowserContext) -> None:
    await context.add_init_script(SHOWN)


async def _open(pages: Pages, path: str, **ctx: Any) -> Page:
    page = await pages.open(path, prepare=_watch, **ctx)
    await until(page, "window.__ws?.readyState === 1")
    return page


@pytest.fixture
async def up(board: W.Board, pages: Pages) -> AsyncIterator[W.Board]:
    assert board.link == "up"  # Teams connected: only the page socket can raise the banner
    yield board
    await pages.close()


async def test_no_status_dot_on_either_page(up: W.Board, settings: Settings, pages: Pages) -> None:
    """036-AC2: the `#status` dot is gone from the widget and the chat page, list and split."""
    async with _served(up, settings) as host:
        pages.serve(host, up)
        for path in PATHS:
            page = await _open(pages, path)
            assert await page.evaluate("document.getElementById('status') === null"), path
            assert await page.evaluate("document.getElementById('banner').getAttribute('role')") == "status"
            # Empty, yet rendered: a live region kept in the tree has its next text announced.
            assert await page.evaluate(
                "(b => !b.hidden && b.checkVisibility())(document.getElementById('banner'))"
            )


async def test_page_socket_down_shows_after_the_grace_and_a_restart_clears_it(
    up: W.Board, settings: Settings, pages: Pages
) -> None:
    """036-AC3: `web` stopped → the message within 3 s, not before 2 s; `web` back → hidden, no reload."""
    port = _free_port()
    async with _served(up, settings, port) as host:
        pages.serve(host, up)
        opened = [await _open(pages, path) for path in PATHS]
    for page in opened:
        await until(page, VISIBLE, tries=400)
        closed, shown = await page.evaluate("[__closed, __shown[0][0]]")
        assert 1900 <= shown - closed < 3000, shown - closed
        assert await page.evaluate(TEXT) == GONE
    async with _served(up, settings, port):
        for page in opened:
            await until(page, HIDDEN, tries=1500)
            assert await page.evaluate("performance.getEntriesByType('navigation').length") == 1
            assert await page.evaluate("__shown.every(([, text]) => text === " + repr(GONE) + ")")


async def test_a_reload_never_flashes_the_banner(up: W.Board, settings: Settings, pages: Pages) -> None:
    """036-AC3: a reload while `web` runs reconnects inside the grace: the banner never shows."""
    async with _served(up, settings) as host:
        pages.serve(host, up)
        for path in PATHS:
            page = await _open(pages, path)
            await page.reload()
            await until(page, "window.__ws?.readyState === 1")
            await page.evaluate("new Promise((r) => setTimeout(r, 2500))")
            assert await page.evaluate("__shown") == [], path


async def test_teams_link_states_show_on_every_page(up: W.Board, settings: Settings, pages: Pages) -> None:
    """036-AC4/AC5: link down → "connection lost" on every page, one opened meanwhile too; login → the
    sign-in line with the command; up → hidden at once."""
    async with _served(up, settings) as host:
        pages.serve(host, up)
        opened = [await _open(pages, path) for path in PATHS]
        up.set_link("down")
        narrow = await _open(pages, "/", viewport={"width": 280, "height": 400})  # opened while down
        opened.append(narrow)
        for page in opened:
            await until(page, VISIBLE, tries=400)
            assert await page.evaluate(TEXT) == DOWN
        # The Teams states get the same grace: the page opened while down waited 2 s since it loaded.
        opened_at, shown = await narrow.evaluate("[__opened, __shown[0][0]]")
        assert shown >= 2000 and shown - opened_at < 3000, (opened_at, shown)
        up.set_link("login")
        for page in opened:
            await until(page, f"{TEXT} === {LOGIN!r}", tries=400)
            # A swap is a new problem: it waits its own grace too.
            assert await page.evaluate("__shown.at(-1)[0] - __linkAt") >= 1900
            assert (
                await page.evaluate("document.querySelector('#banner code').textContent") == "miniteams login"
            )
        # The narrow widget window: the line wraps inside the banner, nothing scrolls sideways.
        assert await narrow.evaluate("document.documentElement.scrollWidth <= innerWidth")
        box = await narrow.locator("#banner").bounding_box()
        assert box is not None and box["width"] == 280 and box["height"] > 30
        up.set_link("up")
        for page in opened:
            await until(page, HIDDEN, tries=50)
            # Emptied, not `hidden`: the live region stays in the tree for the next problem's text.
            assert await page.evaluate(
                "(b => !b.hidden && b.checkVisibility() && !b.firstChild)(document.getElementById('banner'))"
            )


async def test_a_signed_out_page_shows_the_sign_in_alert_alone(
    up: W.Board, settings: Settings, pages: Pages
) -> None:
    """036-AC3: no cookie: the sign-in alert says what to do; the "Disconnected" banner stays out of it."""
    async with _served(up, settings) as host:
        pages.host = host  # no cookie
        for path in PATHS:
            page = await pages.open(path)
            await page.locator("#signin").wait_for()
            await page.evaluate("new Promise((r) => setTimeout(r, 2500))")
            assert await page.evaluate(TEXT) == GONE  # past the grace, yet not shown:
            assert await page.evaluate("document.getElementById('banner').checkVisibility()") is False, path


@pytest.mark.parametrize("width", [280, 480, 1000])
async def test_the_widget_keeps_rows_clear_of_the_banner(
    up: W.Board, settings: Settings, pages: Pages, width: int
) -> None:
    """036-AC4: the banner sticks under the header; a row scrolled into view lands below both (--hdr)."""
    for i in range(60):
        rid = f"19:row{i:02d}@thread.v2"
        up.rows[rid] = {"id": rid, "label": f"Row {i}", "last_activity": f"2026-09-15T09:{i:02d}:00Z"}
        up.rows[rid].update(last_id="1", seen_at=None, typing=[], muted=False, mention=None)
    sizes = """(() => { const s = getComputedStyle(document.documentElement);
      const px = (v) => parseFloat(s.getPropertyValue(v));
      const h = (s) => document.querySelector(s).getBoundingClientRect().height;
      return [px("--head"), px("--hdr"), h("header"), h("#banner")]; })()"""
    async with _served(up, settings) as host:
        pages.serve(host, up)
        widget = await _open(pages, "/", viewport={"width": width, "height": 400})
        await until(widget, "document.querySelectorAll('#rows li').length > 40")
        head, hdr, header, banner = await widget.evaluate(sizes)
        assert (head, hdr, banner) == (header, header, 0)
        up.set_link("login")
        await until(widget, VISIBLE, tries=400)
        await rendered(widget)
        head, hdr, header, banner = await widget.evaluate(sizes)
        assert banner > 0 and (head, hdr) == (header, header + banner)
        await widget.evaluate("scrollTo(0, 800)")
        top = await widget.evaluate("document.getElementById('banner').getBoundingClientRect().top")
        assert top == header  # stuck right under the header
        await widget.evaluate("document.querySelectorAll('#rows li')[30].scrollIntoView()")
        row = await widget.evaluate("document.querySelectorAll('#rows li')[30].getBoundingClientRect().top")
        assert row >= header + banner - 1
        # The window resized with the banner up: the header rewraps, the banner follows it at once.
        await widget.set_viewport_size({"width": 1000 if width < 640 else 280, "height": 400})
        await rendered(widget)
        await widget.evaluate("scrollTo(0, 900)")
        head, hdr, header, banner = await widget.evaluate(sizes)
        assert (head, hdr) == (header, header + banner)
        rect = "document.querySelector({!r}).getBoundingClientRect().{}"
        bottom, top = await widget.evaluate(
            f"[{rect.format('header', 'bottom')}, {rect.format('#banner', 'top')}]"
        )
        assert top == bottom
        up.set_link("up")
        await until(widget, HIDDEN, tries=50)
        await rendered(widget)  # resize observations land before the next frame
        head, hdr, header, banner = await widget.evaluate(sizes)
        assert (head, hdr, banner) == (header, header, 0)


async def test_a_quick_restart_keeps_the_teams_banner(up: W.Board, settings: Settings, pages: Pages) -> None:
    """036-AC3/AC5: sign-in banner up, `web` stopped 300 ms then back: the page redials inside the grace,
    so the banner neither empties nor swaps to "Disconnected"."""
    port = _free_port()
    up.set_link("login")
    async with _served(up, settings, port) as host:
        pages.serve(host, up)
        opened = [await _open(pages, path) for path in PATHS]
        for page in opened:
            await until(page, f"{TEXT} === {LOGIN!r}", tries=400)
    await asyncio.sleep(0.3)
    async with _served(up, settings, port):
        for page in opened:
            await until(page, "__opened > __closed && __linkAt > __closed", tries=1500)
            await page.evaluate("new Promise((r) => setTimeout(r, 2500))")  # any pending grace has run out
            assert await page.evaluate("__log.map(([, text]) => text)") == [LOGIN]


async def test_a_long_drop_swaps_the_teams_banner_after_the_grace(
    up: W.Board, settings: Settings, pages: Pages
) -> None:
    """036-AC3/AC4: "connection lost" up, the socket closed for good: "Disconnected" 2 s after the close."""
    up.set_link("down")
    async with _served(up, settings) as host:
        pages.serve(host, up)
        opened = [await _open(pages, path) for path in PATHS]
        for page in opened:
            await until(page, f"{TEXT} === {DOWN!r}", tries=400)
    for page in opened:
        await until(page, f"{TEXT} === {GONE!r}", tries=400)
        closed, swapped = await page.evaluate("[__closed, __shown.at(-1)[0]]")
        assert 1900 <= swapped - closed < 3000, swapped - closed
        assert await page.evaluate("__log.map(([, text]) => text)") == [DOWN, GONE]


@pytest.mark.parametrize("width", [280, 480, 1000])
async def test_the_options_menu_opens_over_the_banner(
    up: W.Board, settings: Settings, pages: Pages, width: int
) -> None:
    """036-AC5: with a banner up, the widget's ⚙ menu still takes the clicks where the two overlap."""
    up.set_link("login")
    async with _served(up, settings) as host:
        pages.serve(host, up)
        widget = await _open(pages, "/", viewport={"width": width, "height": 600})
        await until(widget, VISIBLE, tries=400)
        await widget.click("#gear")
        hit = """(() => { const m = document.querySelector('.menu').getBoundingClientRect(),
          b = document.getElementById('banner').getBoundingClientRect();
          const top = Math.max(m.top, b.top), bottom = Math.min(m.bottom, b.bottom),
            left = Math.max(m.left, b.left), right = Math.min(m.right, b.right);
          if (top >= bottom || left >= right) return "no overlap";
          return !!document.elementFromPoint((left + right) / 2, (top + bottom) / 2).closest('.menu'); })()"""
        assert await widget.evaluate(hit) is True
