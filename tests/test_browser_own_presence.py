"""My own status in a headless browser (spec 036 phase 3): the header dot, its menu, the set and its errors.

Run: `uv run --with playwright pytest tests/test_browser_own_presence.py`. Synthetic data, no network."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import browser_helpers
import httpx
import pytest
import test_web_chat
from browser_helpers import Page, Pages, rendered, until, wait  # skips without playwright
from test_web_chat import ANN, ME, T, _archive, _served

from miniteams import presence as P
from miniteams import web as W
from miniteams.config import Settings

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, data_dir, quiet_directory = test_web_chat.board, test_web_chat.data_dir, test_web_chat.quiet_directory

DM = f"19:{ME.removeprefix('8:orgid:')}_{ANN.removeprefix('8:orgid:')}@unq.gbl.spaces"
PATHS = ("/", f"/chat/{T}", f"/chat/{T}?chatnav=split")
DOT = "document.querySelector('#own .pres')"
BTN = "document.querySelector('#own .ownbtn')"
MENU = "document.querySelector('.ownmenu')"
ERR = "document.querySelector('.ownerr').textContent"
ENTRIES = [
    "Available",
    "Busy",
    "Do not disturb",
    "Be right back",
    "Appear away",
    "Appear offline",
    "Reset status",
]
VALUES = ["Available", "Busy", "DoNotDisturb", "BeRightBack", "Away", "AppearOffline", "Reset"]
BODIES = [
    {"availability": "Available"},
    {"availability": "Busy"},
    {"availability": "DoNotDisturb"},
    {"availability": "BeRightBack"},
    {"availability": "Away"},
    {"availability": "Offline", "activity": "OffWork"},
    {"availability": "Offline"},
]


@dataclass
class Service:
    """The presence service's forceavailability: records each call, answers `status`; while `held` is set
    and clear, the answers wait for it."""

    status: int = 200
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    answered: int = 0
    held: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), json.loads(request.content)))
        if self.held is not None:
            await self.held.wait()
        self.answered += 1
        return httpx.Response(self.status)


@pytest.fixture
async def served(
    board: W.Board, settings: Settings, pages: Pages
) -> AsyncIterator[tuple[Pages, P.Presence, Service]]:
    service = Service()
    board.rows[DM] = {
        "id": DM,
        "label": "Ann Lee",
        "last_activity": "2026-09-15T10:00:09.000Z",
        "last_id": "9",
    }
    board.rows[DM].update(typing=[], seen_at=None, muted=False, mention=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    presence = board.presence = P.Presence(
        settings, lambda: "tok", board.broadcast_presence, client=client, me=ME
    )
    board.broadcast()  # the follow list now holds me and Ann, my 1:1's peer
    assert {ME, ANN} <= presence.wanted
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield pages, presence, service
        await pages.close()
    await presence.close()
    assert pages.external == []


def _me(availability: str, activity: str = "", forced: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"availability": availability, "activity": activity or availability}
    if forced:
        entry["forcedAvailability"] = {
            "availability": forced,
            "activity": forced,
            "expiry": "2026-11-04T14:39Z",
        }
    return {"presence": [{"mri": ME, "presence": entry}]}


async def _open(pages: Pages, path: str, **ctx: Any) -> Page:
    page = await pages.open(path, **ctx)
    await until(page, "window.__ws?.readyState === 1 && window.__frames.some(f => 'presence' in f)")
    return page


async def _dot(page: Page) -> list[str]:
    return list(await page.evaluate(f"[{DOT}.className, {BTN}.title, {BTN}.getAttribute('aria-label')]"))


async def _open_menu(page: Page) -> None:
    await page.locator("#own .ownbtn").click()
    await until(page, f"{MENU} !== null")


async def test_own_dot_follows_my_frames_only_and_unknown_is_a_ring(served: Any) -> None:
    """036-AC6: my dot and label on both pages, list and split; a change repaints it without a reload; a frame
    about someone else leaves it; unknown is an empty dashed ring, never the Offline look."""
    pages, presence, _ = served
    opened = [await _open(pages, path) for path in PATHS]
    for page in opened:
        assert await _dot(page) == ["pres unknown", "Status unknown", "my status: Status unknown"]
        assert await page.evaluate(f"{BTN}.getAttribute('aria-haspopup')") == "menu"
        assert await page.evaluate(f"{BTN}.getAttribute('aria-expanded')") == "false"
        style = await page.evaluate(
            f"(s => [s.borderTopStyle, s.backgroundColor, s.boxShadow])(getComputedStyle({DOT}))"
        )
        assert style == ["dashed", "rgba(0, 0, 0, 0)", "none"]
    await presence.on_presence(_me("Busy", "InACall"))
    for page in opened:
        await until(page, f"{DOT}.className === 'pres busy'")
        assert await _dot(page) == ["pres busy", "Busy · In a call", "my status: Busy · In a call"]
    await presence.on_presence({"presence": [{"mri": ANN, "presence": {"availability": "Away"}}]})
    for page in opened:  # someone else's frame: my dot stays
        await until(page, f"window.__frames.some(f => f.presence?.[{ANN!r}]?.availability === 'Away')")
        assert await _dot(page) == ["pres busy", "Busy · In a call", "my status: Busy · In a call"]
    await presence.on_presence(_me("Available"))
    for page in opened:
        await until(page, f"{DOT}.className === 'pres ok'")
        assert await page.evaluate(f"{BTN}.title") == "Available"
    await presence.on_presence(_me("Offline", "OffWork", "Offline"))
    for page in opened:
        await until(page, f"{DOT}.className === 'pres off'")
        offline = await page.evaluate(f"(s => [s.borderTopStyle, s.boxShadow])(getComputedStyle({DOT}))")
        assert offline[0] != "dashed" and offline[1] != "none"  # the Offline ring is not the unknown one
    await presence.on_presence({"presence": [{"mri": ME, "presence": {"availability": "PresenceUnknown"}}]})
    for page in opened:
        await until(page, f"{DOT}.className === 'pres unknown'")


async def test_each_entry_sends_its_set_by_mouse_and_keyboard(served: Any) -> None:
    """036-AC7/AC8/AC10: the menu lists the current label then the 7 entries; each pick (click, or keys)
    sends its verb, web makes its call, the menu closes and the focus is back on the button."""
    pages, presence, service = served
    await presence.on_presence(_me("Busy", "InAMeeting"))
    widget = await _open(pages, "/")
    await until(widget, f"{DOT}.className === 'pres busy'")
    await _open_menu(widget)
    assert await widget.evaluate(f"{MENU}.querySelector('.cur').textContent") == "Busy · In a meeting"
    assert await widget.locator(".ownmenu button").all_text_contents() == ENTRIES
    assert await widget.evaluate(f"{MENU}.getAttribute('role')") == "menu"
    roles = await widget.evaluate(f"[...{MENU}.querySelectorAll('button')].map(b => b.getAttribute('role'))")
    assert roles == ["menuitemradio"] * 6 + ["menuitem"]
    assert await widget.evaluate(f"{BTN}.getAttribute('aria-expanded')") == "true"
    await widget.keyboard.press("Escape")
    for i, value in enumerate(VALUES):
        if i % 2:  # by keyboard: Enter opens on the first entry, arrows walk, Enter picks
            await widget.evaluate(f"{BTN}.focus()")
            await widget.keyboard.press("Enter")
            await until(widget, f"{MENU} !== null")
            for _ in range(i):
                await widget.keyboard.press("ArrowDown")
            await widget.keyboard.press("Enter")
        else:
            await _open_menu(widget)
            await widget.locator(".ownmenu button", has_text=ENTRIES[i]).click()
        await until(widget, f"window.__frames.some(f => f.setpresence === {value!r} && f.ok === true)")
        assert await widget.evaluate(f"{MENU} === null && document.activeElement === {BTN}")
        assert await widget.evaluate(f"{BTN}.getAttribute('aria-expanded')") == "false"
    sent = await widget.evaluate("window.__out.filter(v => 'setpresence' in v)")
    assert sent == [{"setpresence": v} for v in VALUES]
    url = "https://presence.teams.microsoft.com/v1/me/forceavailability/"
    assert service.calls == [("PUT", url, body) for body in BODIES]
    assert await widget.evaluate(f"{DOT}.className") == "pres busy"  # no optimistic change: frames only
    # ArrowUp on the button opens on the last entry; ArrowDown wraps to the first.
    await widget.evaluate(f"{BTN}.focus()")
    await widget.keyboard.press("ArrowUp")
    assert await widget.evaluate("document.activeElement.textContent") == "Reset status"
    await widget.keyboard.press("ArrowDown")
    assert await widget.evaluate("document.activeElement.textContent") == "Available"


async def test_the_forced_entry_is_marked(served: Any) -> None:
    """036-AC7/AC8: the forced entry is checked (✓ + aria-checked), the menu opens on it, a frame moves the
    mark while the menu is open; Reset (no forcedAvailability) marks none."""
    pages, presence, _ = served
    checked = f"[...{MENU}.querySelectorAll('[aria-checked=true]')].map(b => b.textContent)"
    for path in PATHS:
        await presence.on_presence(_me("Busy", "Busy", "Busy"))
        page = await _open(pages, path)
        await until(page, f"{DOT}.className === 'pres busy'")
        await _open_menu(page)
        assert await page.evaluate(checked) == ["Busy"]
        assert await page.evaluate("document.activeElement.textContent") == "Busy"
        mark = await page.evaluate(
            f"getComputedStyle({MENU}.querySelector('[aria-checked=true]'), '::after').content"
        )
        assert mark == '"✓"'
        await presence.on_presence(_me("Offline", "OffWork", "Offline"))
        await until(page, f"JSON.stringify({checked}) === '[\"Appear offline\"]'")
        assert await page.evaluate("document.activeElement.textContent") == "Busy"  # the focus stays put
        await presence.on_presence(_me("Busy", "InAMeeting"))
        await until(page, f"{checked}.length === 0")
        assert await page.evaluate(f"{MENU}.querySelector('.cur').textContent") == "Busy · In a meeting"
        await page.keyboard.press("Escape")


async def test_a_failed_set_shows_its_reason_and_the_dot_stays(served: Any) -> None:
    """036-AC9: a refused call says "status not changed: <reason>" by the button, the dot unchanged; the
    next success clears it."""
    pages, presence, service = served
    await presence.on_presence(_me("Busy", "InAMeeting"))
    service.status = 401
    for path in PATHS[:2]:
        page = await _open(pages, path)
        await _open_menu(page)
        await page.locator(".ownmenu button", has_text="Do not disturb").click()
        await until(page, f"{ERR} === 'status not changed: HTTP 401'")
        assert await page.evaluate(f"{DOT}.className") == "pres busy"
        assert await page.evaluate("document.querySelector('.ownerr').getAttribute('role')") == "status"
        err = await page.evaluate("document.querySelector('.ownerr').getBoundingClientRect().toJSON()")
        btn = await page.evaluate(f"{BTN}.getBoundingClientRect().toJSON()")
        width = await page.evaluate("document.documentElement.clientWidth")
        assert (
            err["top"] >= btn["bottom"] and 0 <= err["left"] and err["right"] <= width
        )  # under it, on screen
        service.status = 200
        await _open_menu(page)
        await page.locator(".ownmenu button", has_text="Available").click()
        await until(page, f"{ERR} === ''")
        service.status = 401


@pytest.mark.parametrize(("width", "height"), [(280, 500), (480, 500), (1000, 500), (280, 160)])
async def test_menu_stays_in_the_viewport_and_closes(served: Any, width: int, height: int) -> None:
    """036-AC10: under the button, inside the viewport (a short window pushes it up), above the sticky
    header and the banner; Escape and a click outside close it, Escape gives the focus back; a resize keeps
    it inside."""
    pages, _, _ = served
    for path in PATHS:
        page = await _open(pages, path, viewport={"width": width, "height": height})
        await _open_menu(page)
        inside = f"""(() => {{ const m = {MENU}.getBoundingClientRect(), b = {BTN}.getBoundingClientRect();
          const w = document.documentElement.clientWidth, h = document.documentElement.clientHeight;
          const top = document.elementFromPoint(m.left + m.width / 2, m.top + 8);
          return [m.left >= 0, m.right <= w, m.top >= 0, m.bottom <= h, m.top >= b.bottom,
            {MENU}.contains(top)]; }})()"""
        flags = await page.evaluate(inside)
        assert flags[:4] + flags[5:] == [True] * 5, (path, width)
        pushed = path == "/" and height < 300  # under the 2-line widget header it would overflow: moved up
        assert flags[4] is not pushed, (path, width)
        if path == "/" or width > 400:  # the chat header needs ~380 px, as before
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.set_viewport_size({"width": 280 if width > 280 else 600, "height": height})
        await rendered(page)
        flags = await page.evaluate(inside)
        assert flags[:4] == [True] * 4, (path, width)
        await page.keyboard.press("Escape")
        assert await page.evaluate(f"{MENU} === null && document.activeElement === {BTN}")
        await _open_menu(page)
        await page.locator("h1").click()
        await until(page, f"{MENU} === null")
        assert await page.evaluate(f"{BTN}.getAttribute('aria-expanded')") == "false"
        await page.close()


async def test_entries_are_disabled_while_the_socket_is_closed(board: W.Board, served: Any) -> None:
    """036 scope (Feedback): the page's socket down → entries disabled, a pick sends nothing, the menu and
    its focus stay; back up → enabled again, the menu still open."""
    pages, _, service = served
    page = await _open(pages, "/")
    await _open_menu(page)
    await page.keyboard.press("ArrowDown")
    disabled = f"[...{MENU}.querySelectorAll('button')].map(b => b.getAttribute('aria-disabled'))"
    assert set(await page.evaluate(disabled)) == {"false"}
    # One evaluate: the page redials 1 s after the close, picks must land while it is down.
    down = await page.evaluate(
        f"""new Promise((r) => {{ window.__ws.addEventListener("close", () => {{
          const state = [{disabled}, document.activeElement.textContent];
          document.activeElement.click();
          [...{MENU}.querySelectorAll("button")].find((b) => b.textContent === "Appear away").click();
          r([...state, {MENU} !== null, document.activeElement.textContent]); }}); window.__ws.close(); }})"""
    )
    assert down == [["true"] * 7, "Busy", True, "Busy"]
    await until(page, "window.__ws.readyState === 1", tries=1500)
    await until(page, f"{disabled}.every(d => d === 'false')")
    assert await page.evaluate(f"{MENU} !== null && document.activeElement.textContent === 'Busy'")
    assert await page.evaluate("window.__out.filter(v => 'setpresence' in v)") == []
    assert service.calls == []
    await wait(lambda: len(board.pages) == 1, "the old socket gone")


SETS = "window.__out.filter(v => 'setpresence' in v)"
DISABLED = f"[...{MENU}.querySelectorAll('button')].map(b => b.getAttribute('aria-disabled'))"


async def test_a_pick_waits_for_its_answer_before_the_next(served: Any) -> None:
    """036-AC9: while a pick waits for its answer the entries are disabled and a second pick sends nothing
    (no "busy" error from web); the answer frees them."""
    pages, _, service = served
    service.held = asyncio.Event()
    page = await _open(pages, "/")
    await _open_menu(page)
    await page.locator(".ownmenu button", has_text="Busy").click()
    await wait(lambda: len(service.calls) == 1, "the first set")
    await _open_menu(page)
    assert set(await page.evaluate(DISABLED)) == {"true"}
    await page.evaluate(
        f"[...{MENU}.querySelectorAll('button')].find(b => b.textContent === 'Appear away').click()"
    )
    await page.locator(".ownmenu button", has_text="Available").click(force=True)  # aria-disabled: forced
    assert await page.evaluate(SETS) == [{"setpresence": "Busy"}]
    service.held.set()
    await until(page, f"{DISABLED}.every(d => d === 'false')")
    assert await page.evaluate(ERR) == "" and len(service.calls) == 1


async def test_a_socket_drop_frees_a_pick_left_waiting(served: Any) -> None:
    """036 scope (Feedback): a pick whose socket dropped never gets its answer: the redialled page enables
    the entries again and its next pick reaches web."""
    pages, _, service = served
    service.held = asyncio.Event()
    page = await _open(pages, "/")
    await _open_menu(page)
    await page.locator(".ownmenu button", has_text="Busy").click()
    await wait(lambda: len(service.calls) == 1, "the first set")
    await page.evaluate("window.__ws.close()")
    await until(page, "window.__ws.readyState === 3")
    await until(page, "window.__ws.readyState === 1", tries=1500)
    service.held.set()
    await wait(lambda: service.answered == 1, "the old set answered, to the closed socket")
    await _open_menu(page)
    assert set(await page.evaluate(DISABLED)) == {"false"}
    await page.locator(".ownmenu button", has_text="Available").click()
    await until(page, "window.__frames.some(f => f.setpresence === 'Available' && f.ok === true)")
    assert [body for _, _, body in service.calls] == [{"availability": "Busy"}, {"availability": "Available"}]


async def test_a_chat_page_without_authors_paints_my_dot_on_connect(served: Any, presence_chat: str) -> None:
    """036-AC6: a chat page showing no author avatars (so asking no presence) gets my state on connect."""
    pages, presence, _ = served
    await presence.on_presence(_me("DoNotDisturb", "Presenting"))
    page = await _open(pages, f"/chat/{presence_chat}")
    await until(page, f"{DOT}.className === 'pres dnd'")
    assert await page.evaluate("document.querySelectorAll('.msg .av').length") == 0
    assert await page.evaluate("window.__out.filter(v => 'authors' in v)") == []
    assert await page.evaluate(f"{BTN}.title") == "Do not disturb · Presenting"
    await presence.on_presence(_me("Away", "Away"))  # a later change reaches it too: no frozen dot
    await until(page, f"{DOT}.className === 'pres away'")


async def test_a_longer_label_keeps_the_open_menu_on_the_button(served: Any) -> None:
    """036-AC10: a frame lengthening the menu's label while it is open (narrow widget) keeps it under the
    button and inside the viewport."""
    pages, presence, _ = served
    await presence.on_presence(_me("Busy", "Busy"))
    page = await _open(pages, "/", viewport={"width": 280, "height": 500})
    await until(page, f"{DOT}.className === 'pres busy'")
    await _open_menu(page)
    await presence.on_presence(_me("Busy", "PresentingTheFictionalQuarterlyBudgetToEveryone"))
    await until(page, f"{MENU}.querySelector('.cur').textContent.includes('quarterly')")
    placed = f"""(() => {{ const m = {MENU}.getBoundingClientRect(), b = {BTN}.getBoundingClientRect();
      const w = document.documentElement.clientWidth;
      return [m.left >= 0, m.right <= w, m.top >= b.bottom, Math.abs(m.right - b.right) < 1 || m.left === 0,
        m.width > 200]; }})()"""
    assert await page.evaluate(placed) == [True] * 5


@pytest.fixture
def presence_chat(data_dir: Any) -> str:
    """An archived chat with no message: its page has no author to ask presence for."""
    empty = "19:eeee@thread.v2"
    _archive(data_dir, empty, [])
    return empty
