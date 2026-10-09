"""Split mode UX (spec 011 phase 8): resizable list, filters and tabs, keyboard path, narrow header,
current row, empty states, contrast. A fake chat socket serves synthetic rows; no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_split_ux.py`.
"""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any
from urllib.parse import quote

import pytest
import test_browser
from test_browser import Page, Pages, Route  # its import skips without playwright
from test_browser_chat_compose import _until, _wait

from miniteams import web as W

pytestmark = pytest.mark.browser
pages = test_browser.pages

ORIGIN = "http://127.0.0.1:9"  # never dialled: every request on it is answered by a route
# A module global read by `_open`: a mutation run swaps it for an altered copy.
PAGE = files("miniteams").joinpath("chat.html").read_text(encoding="utf-8")
_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _id(i: int) -> str:
    return f"19:r{i:03d}@thread.v2"


def _row(i: int, **over: Any) -> dict[str, Any]:
    """Row `i` of the board, newest first: index order is list order."""
    at = (_NOW - timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
    return {
        "id": _id(i),
        "label": f"Fictional chat {i:03d}",
        "last_activity": at,
        "unread": False,
        "mention": None,
        "sender": "Ann Lee",
        "text": f"snippet {i}",
        "seen_at": None,
        "muted": False,
        "typing": [],
        **over,
    }


def _board(n: int) -> list[dict[str, Any]]:
    """`n` rows; every 7th from 5 is seen and every 11th from 3 muted, so Seen and Muted count too."""
    seen = {"seen_at": "2027-01-01T00:00:00.000Z"}
    return [_row(i, **(seen if i % 7 == 5 else {}), muted=i % 11 == 3) for i in range(n)]


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Tab counts as the page's filters make them (no unread, no mention on this board)."""
    seen = sum(1 for r in rows if r["seen_at"] and not r["muted"])
    muted = sum(1 for r in rows if r["muted"])
    return {"all": sum(1 for r in rows if not r["seen_at"] and not r["muted"]), "seen": seen, "muted": muted}


@dataclass
class Fake:
    """The server side of the chat socket: follow, history and the board's rows."""

    rows: list[dict[str, Any]]
    verbs: list[dict[str, Any]] = field(default_factory=list)
    ws: Any = None

    def attach(self, ws: Any) -> None:
        self.ws = ws
        ws.on_message(self._on_message)

    def broadcast(self) -> None:
        self.ws.send(json.dumps({"rows": self.rows, "browser": False, "me": "Me"}))

    def _on_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        self.verbs.append(msg)
        if "follow" in msg:
            label = next((r["label"] for r in self.rows if r["id"] == msg["follow"]), "?") + " · 3p"
            self.ws.send(json.dumps({"follow": msg["follow"], "label": label, "page": "1", "opener": False}))
        elif "history" in msg:
            hello = {"id": "1", "at": "2026-10-09T10:00:00.000Z", "from": "8:orgid:ann", "name": "Ann Lee"}
            messages = [{**hello, "me": False, "type": "text", "html": "<p>hello</p>", "reactions": []}]
            self.ws.send(json.dumps({"history": msg["history"], "messages": messages, "source": "archive"}))
        elif msg.get("board") is True:
            self.broadcast()

    def follows(self) -> list[str]:
        return [v["follow"] for v in self.verbs if "follow" in v]


@dataclass
class Split:
    pages: Pages
    fake: Fake

    async def open(self, at: int = 2, query: str = "?chatnav=split", **ctx: Any) -> Page:
        """The chat page of row `at`, its list loaded. A narrow window keeps the list folded, attached."""
        context = await self.pages.browser.new_context(**ctx)
        await context.route_web_socket(re.compile(r"^ws://127\.0\.0\.1:9/ws\?chat$"), self.fake.attach)
        await context.add_init_script(test_browser._FRAMES)

        async def other(route: Route) -> None:
            if not route.request.url.startswith(ORIGIN + "/"):
                self.pages.external.append(route.request.url)
            await route.abort()

        async def page_html(route: Route) -> None:
            headers = {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Security-Policy": W._chat_csp(PAGE),
            }
            await route.fulfill(body=PAGE, headers=headers)

        await context.route("**/*", other)  # last registered wins: the catch-all goes first
        await context.route(re.compile(r"^http://127\.0\.0\.1:9/chat/[^/]+$"), page_html)
        page = await context.new_page()
        page.on("pageerror", lambda exc: self.pages.errors.append(str(exc)))
        await page.goto(f"{ORIGIN}/chat/{quote(_id(at), safe='')}{query}")
        await self.ready(page)
        return page

    async def ready(self, page: Page) -> None:
        await _until(page, "window.__frames.some(f => 'history' in f)")
        if await page.evaluate("document.body.classList.contains('split')"):
            await page.wait_for_selector("#rows li", state="attached")

    async def reload(self, page: Page) -> None:
        self.fake.verbs.clear()
        await page.reload()
        await self.ready(page)


@pytest.fixture
async def split(pages: Pages) -> AsyncIterator[Split]:
    found = Split(pages, Fake(_board(12)))
    yield found
    assert pages.external == []


async def _width(page: Page) -> float:
    return float(await page.evaluate("document.getElementById('side').getBoundingClientRect().width"))


async def _stored(page: Page, key: str) -> str | None:
    value = await page.evaluate("k => localStorage.getItem(k)", key)
    return None if value is None else str(value)


async def _focused(page: Page) -> str:
    """The focused element as `#id`, `k:<data-k>` or its tag."""
    js = "(e => e.id ? '#' + e.id : e.dataset.k ? 'k:' + e.dataset.k : e.tagName)(document.activeElement)"
    return str(await page.evaluate(js))


async def _visible_tabs(page: Page) -> list[str]:
    return list(
        await page.locator("#tabs button").evaluate_all(
            "bs => bs.filter(b => b.getClientRects().length).map(b => b.textContent)"
        )
    )


# --- AC27: resizable column ---


async def test_dragging_the_separator_resizes_saves_on_release_and_survives_a_reload(split: Split) -> None:
    """011-AC27: a focusable separator between list and chat; +100 px of drag gives a 400 px list, saved
    as `chatnav_w` on release only, and kept after a reload."""
    page = await split.open(viewport={"width": 1300, "height": 800})
    grip = page.locator("#grip")
    assert await grip.get_attribute("role") == "separator"
    assert await grip.get_attribute("aria-orientation") == "vertical"
    assert await grip.get_attribute("aria-controls") == "side"
    assert await grip.get_attribute("tabindex") == "0"
    assert await grip.get_attribute("aria-valuenow") == "300"
    assert (await grip.get_attribute("aria-valuemin"), await grip.get_attribute("aria-valuemax")) == (
        "280",
        "560",
    )
    box = await grip.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    assert abs(x - await _width(page)) <= 1  # over the list's right border
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + 50, y)
    assert await _width(page) == 350
    assert await _stored(page, "chatnav_w") is None
    await page.mouse.move(x + 100, y)
    await page.mouse.up()
    assert await _width(page) == 400
    assert await _stored(page, "chatnav_w") == "400"
    assert await grip.get_attribute("aria-valuenow") == "400"
    await split.reload(page)
    assert await _width(page) == 400


async def test_separator_keys_step_bound_and_reset(split: Split) -> None:
    """011-AC27: → steps 16 px (Shift 64), each saved; Home = 280, End = min(560, window − 420); Enter
    and double-click reset to 300 and clear the key."""
    page = await split.open(viewport={"width": 900, "height": 700})
    grip = page.locator("#grip")
    await grip.focus()
    for _ in range(3):
        await page.keyboard.press("ArrowRight")
    assert await _width(page) == 348
    assert await _stored(page, "chatnav_w") == "348"
    await page.keyboard.press("Shift+ArrowLeft")
    assert await _width(page) == 284
    await page.keyboard.press("Home")
    assert await _width(page) == 280
    assert await _stored(page, "chatnav_w") == "280"
    await page.keyboard.press("End")
    assert await _width(page) == 480  # 900 − 420
    assert await _stored(page, "chatnav_w") == "480"
    await page.keyboard.press("Enter")
    assert await _width(page) == 300
    assert await _stored(page, "chatnav_w") is None
    await page.keyboard.press("ArrowLeft")
    assert await _stored(page, "chatnav_w") == "284"
    await grip.dblclick()
    assert await _width(page) == 300
    assert await _stored(page, "chatnav_w") is None


@pytest.mark.parametrize(("stored", "width"), [("9999", 560), ("100", 280), ("wide", 300)])
async def test_a_stored_width_out_of_range_is_clamped(split: Split, stored: str, width: int) -> None:
    """011-AC27: a stored value beyond the bounds (or not a number) shows clamped, at 1024 px wide; a
    narrower window clamps again without touching what was stored."""
    page = await split.open(viewport={"width": 1024, "height": 700})
    await page.evaluate("v => localStorage.setItem('chatnav_w', v)", stored)
    await split.reload(page)
    assert await _width(page) == width
    await page.set_viewport_size({"width": 800, "height": 700})
    await _until(page, f"document.getElementById('side').getBoundingClientRect().width === {min(width, 380)}")
    assert await _stored(page, "chatnav_w") == stored


async def test_no_separator_in_a_narrow_window_or_in_list_mode(split: Split) -> None:
    """011-AC27: no separator at 700 px and below, none in list mode (the URL param wins over the stored
    split), and the page never writes `chatnav`."""
    page = await split.open(viewport={"width": 700, "height": 700})
    assert not await page.locator("#grip").is_visible()
    await page.set_viewport_size({"width": 701, "height": 700})
    await page.locator("#grip").wait_for(state="visible")
    assert await _stored(page, "chatnav") is None
    listed = await split.open(query="?chatnav=list")
    await listed.evaluate("localStorage.setItem('chatnav', 'split')")
    await split.reload(listed)
    assert not await listed.locator("#grip").is_visible()
    assert not await listed.locator("#side").is_visible()


# --- AC28: filter row and tabs ---


async def test_filter_row_lines_up_with_the_chat_header(split: Split) -> None:
    """011-AC28: the filter row is as tall as the chat header, bottom borders within 1 px."""
    page = await split.open(viewport={"width": 1300, "height": 800})
    bottoms = await page.evaluate(
        "[...document.querySelectorAll('#side .bar, header')].map(e => e.getBoundingClientRect().bottom)"
    )
    assert abs(bottoms[0] - bottoms[1]) <= 1


async def _fits(page: Page) -> None:
    """Every shown tab lies inside the strip, unclipped, and the strip does not scroll."""
    bad = await page.evaluate(
        """(() => { const seg = document.getElementById('tabs'), s = seg.getBoundingClientRect(), out = [];
          if (seg.scrollWidth > seg.clientWidth) out.push('strip scrolls');
          for (const b of seg.querySelectorAll('button')) { const r = b.getBoundingClientRect();
            if (!r.width) continue;
            const out_of = r.left < s.left - 0.5 || r.right > s.right + 0.5;
            if (out_of || b.scrollWidth > b.clientWidth) out.push(b.textContent); }
          return out; })()"""
    )
    assert bad == []


async def test_tabs_fit_a_280px_column_with_the_active_tab_shown(split: Split) -> None:
    """011-AC28: at 280 px Inbox, @, Unread and ⋯ fit without scrolling, three-digit counts included;
    Seen and Muted sit in the ⋯ menu, and the active one takes the ⋯ place, selected."""
    split.fake.rows = _board(400)
    page = await split.open(viewport={"width": 1300, "height": 800})
    await page.locator("#grip").focus()
    await page.keyboard.press("Home")
    n = _counts(split.fake.rows)
    assert n["all"] >= 100  # three-digit counts
    assert await _visible_tabs(page) == [f"Inbox{n['all']}", "@0", "Unread0", "⋯"]
    await _fits(page)
    more = page.locator("#tabs .more")
    assert await more.get_attribute("aria-expanded") == "false"
    await more.click()
    assert await more.get_attribute("aria-expanded") == "true"
    assert await page.locator("#more button").all_text_contents() == [
        f"Seen {n['seen']}",
        f"Muted {n['muted']}",
    ]
    await page.locator("#more").get_by_role("menuitemradio", name="Muted").click()
    assert await page.locator("#more").is_hidden()
    assert await _visible_tabs(page) == [f"Inbox{n['all']}", "@0", "Unread0", f"Muted{n['muted']}"]
    assert await more.evaluate("b => b.classList.contains('on')")
    await _fits(page)
    assert await _focused(page) == "k:tab:more"
    await page.keyboard.press("Enter")
    await page.keyboard.press("Escape")
    assert await page.locator("#more").is_hidden()
    assert await _focused(page) == "k:tab:more"


async def test_five_tabs_show_inline_from_360px(split: Split) -> None:
    """011-AC28: a list of 360 px or more shows the five tabs, no ⋯."""
    page = await split.open(viewport={"width": 1300, "height": 800})
    await page.evaluate("localStorage.setItem('chatnav_w', '360')")
    await split.reload(page)
    assert [t.rstrip("0123456789") for t in await _visible_tabs(page)] == [
        "Inbox",
        "@",
        "Unread",
        "Seen",
        "Muted",
    ]
    await _fits(page)


async def test_filter_clears_with_its_button_and_escape(split: Split) -> None:
    """011-AC28: the filter shows its own × while it holds text; × and Escape both clear it, Escape the
    arrow keys' highlight too."""
    page = await split.open()
    flt, clear = page.locator("#filter"), page.locator("#clear")
    assert not await clear.is_visible()
    await flt.fill("chat 00")
    assert await clear.is_visible()
    assert len(await page.locator("#rows li").all()) == 8  # 000-009 less one seen and one muted
    await clear.click()
    assert await flt.input_value() == ""
    assert not await clear.is_visible()
    assert await _focused(page) == "#filter"
    await flt.fill("chat 00")
    await flt.press("Escape")
    assert await flt.input_value() == ""
    assert len(await page.locator("#rows li").all()) == _counts(split.fake.rows)["all"]
    await flt.press("ArrowDown")
    assert await page.locator("#rows li.sel").count() == 1
    await flt.press("Escape")
    assert await page.locator("#rows li.sel").count() == 0


async def test_filter_arrows_highlight_and_enter_switches(split: Split) -> None:
    """011-AC28: ↓/↑ highlight a row with the focus kept in the filter, Enter switches to it; Enter
    with nothing highlighted takes the first row."""
    page = await split.open()
    flt = page.locator("#filter")
    await flt.fill("chat 01")
    for key in ("ArrowDown", "ArrowDown", "ArrowDown", "ArrowUp", "ArrowDown"):  # 010, 011, 011, 010, 011
        await flt.press(key)
    assert await page.locator("#rows li.sel").get_attribute("data-id") == _id(11)
    assert await _focused(page) == "#filter"
    await flt.press("Enter")
    await _wait(lambda: split.fake.follows()[-1:] == [_id(11)])
    assert page.url.endswith(quote(_id(11), safe="") + "?chatnav=split")
    assert await _focused(page) == "#reply"
    await flt.fill("chat 00")
    await flt.press("Enter")
    await _wait(lambda: split.fake.follows()[-1:] == [_id(0)])


@pytest.mark.parametrize("how", ["docked", "folded", "drawer"])
async def test_ctrl_k_focuses_the_filter_from_anywhere(split: Split, how: str) -> None:
    """011-AC28: Ctrl+K from the composer focuses the filter, opening the list first when it is folded
    away or behind ☰ in a narrow window."""
    narrow = how == "drawer"
    page = await split.open(viewport={"width": 600 if narrow else 1300, "height": 700})
    if how == "folded":
        await page.click("#menu")
        assert not await page.locator("#side").is_visible()
    await page.locator("#reply").focus()
    await page.keyboard.press("Control+k")
    assert await _focused(page) == "#filter"
    assert await page.locator("#side").is_visible()
    assert await page.locator("#menu").get_attribute("aria-expanded") == "true"


# --- AC29: rows and focus ---


async def test_row_buttons_take_no_width_until_hover_or_focus(split: Split) -> None:
    """011-AC29: ✓ and 🔕 take no width off hover, show on hover and on keyboard focus in the row."""
    page = await split.open()
    row = page.locator(f'#rows li[data-id="{_id(4)}"]')
    widths = "li => [...li.querySelectorAll('.meta button')].map(b => b.getBoundingClientRect().width)"
    assert await row.evaluate(widths) == [0, 0]
    await row.hover()
    assert all(w > 0 for w in await row.evaluate(widths))
    await page.mouse.move(900, 400)
    assert await row.evaluate(widths) == [0, 0]
    await row.locator(".name").focus()
    assert all(w > 0 for w in await row.evaluate(widths))


async def test_the_chat_header_is_ten_tabs_from_the_filter_with_many_rows(split: Split) -> None:
    """011-AC29: one row name in the Tab order (roving tabindex): with 400 rows, Tab goes filter → tabs →
    current row (→ ✓ 🔕) → separator → ☰ → search within 10 presses."""
    split.fake.rows = _board(400)
    page = await split.open(viewport={"width": 1300, "height": 800})
    assert await page.locator('#rows .name[tabindex="0"]').count() == 1
    await page.locator("#filter").focus()
    path = []
    for _ in range(10):
        await page.keyboard.press("Tab")
        path.append(await _focused(page))
        if path[-1] == "#find":
            break
    assert path == [
        "k:tab:all",
        "k:tab:mention",
        "k:tab:unread",
        "k:tab:more",
        "k:name",
        "k:seen",
        "k:mute",
        "#grip",
        "#menu",
        "#find",
    ]
    cur = page.locator("#rows li.cur")
    assert await cur.locator(".name").get_attribute("tabindex") == "0"


async def test_row_keys_move_the_tab_stop_and_enter_switches(split: Split) -> None:
    """011-AC29: ↓/↑/Home/End move the focus and the one Tab stop along the rows; Enter switches."""
    page = await split.open()
    stops = (
        "[...document.querySelectorAll('#rows .name[tabindex=\"0\"]')].map(a => a.closest('li').dataset.id)"
    )
    await page.locator("#rows li.cur .name").focus()
    await page.keyboard.press("ArrowDown")  # 3 is muted, out of Inbox
    assert await page.evaluate(stops) == [_id(4)]
    assert await page.evaluate("document.activeElement.closest('li').dataset.id") == _id(4)
    await page.keyboard.press("End")
    last = await page.locator("#rows li").last.get_attribute("data-id")
    assert await page.evaluate(stops) == [last]
    await page.keyboard.press("Home")
    await page.keyboard.press("ArrowUp")  # stays on the first
    assert await page.evaluate(stops) == [_id(0)]
    split.fake.broadcast()  # a re-render keeps the stop and the focus
    await _until(page, "window.__frames.filter(f => 'rows' in f).length >= 2")
    assert await page.evaluate(stops) == [_id(0)]
    assert await page.evaluate("document.activeElement.closest('li')?.dataset.id") == _id(0)
    await page.keyboard.press("Enter")
    await _wait(lambda: split.fake.follows()[-1:] == [_id(0)])
    assert await _focused(page) == "#reply"


async def test_menu_folds_the_docked_list_and_remembers(split: Split) -> None:
    """011-AC29: above 700 px ☰ folds and unfolds the docked list (separator included), with
    aria-expanded, saved as `chatnav_fold`."""
    page = await split.open(viewport={"width": 1300, "height": 800})
    menu = page.locator("#menu")
    assert await menu.is_visible()
    assert await menu.get_attribute("aria-expanded") == "true"
    await menu.click()
    assert not await page.locator("#side").is_visible()
    assert not await page.locator("#grip").is_visible()
    assert await menu.get_attribute("aria-expanded") == "false"
    assert await _stored(page, "chatnav_fold") == "1"
    await split.reload(page)
    assert not await page.locator("#side").is_visible()
    await page.locator("#menu").click()
    assert await page.locator("#side").is_visible()
    assert await _stored(page, "chatnav_fold") is None


async def test_narrow_drawer_focus_escape_and_backdrop(split: Split) -> None:
    """011-AC29: at 700 px and below ☰ opens a drawer over a backdrop: aria-expanded, focus into the
    filter, Escape closes it back to ☰, a click on the backdrop closes it; folding is not saved."""
    page = await split.open(viewport={"width": 600, "height": 700})
    menu, side = page.locator("#menu"), page.locator("#side")
    assert await menu.get_attribute("aria-expanded") == "false"
    await menu.click()
    assert await side.is_visible()
    assert await menu.get_attribute("aria-expanded") == "true"
    assert await _focused(page) == "#filter"
    # Hit testing cannot tell (the chat under it is inert): the pseudo-element must be generated and filled.
    backdrop = await page.evaluate(
        "(s => [s.content, s.position, s.backgroundColor])"
        "(getComputedStyle(document.getElementById('main'), '::after'))"
    )
    assert backdrop[:2] == ['""', "fixed"]
    assert backdrop[2] not in ("", "rgba(0, 0, 0, 0)")
    await page.keyboard.type("chat")
    await page.keyboard.press("Escape")  # clears the filter first, the drawer stays
    assert await page.locator("#filter").input_value() == ""
    assert await side.is_visible()
    await page.keyboard.press("Escape")
    assert not await side.is_visible()
    assert await _focused(page) == "#menu"
    assert await menu.get_attribute("aria-expanded") == "false"
    await menu.click()
    await page.mouse.click(550, 400)
    assert not await side.is_visible()
    assert await _stored(page, "chatnav_fold") is None


# --- AC30: header, current row, empty states, focus, contrast, scroll ---


# Overflow as the reader sees it: a child past the header's content box. scrollWidth misses a child
# pushed into the end padding.
_HEADER = """(() => {
  const h = document.querySelector('header'), cs = getComputedStyle(h);
  const end = h.getBoundingClientRect().right - parseFloat(cs.paddingRight);
  const shown = (c) => c.getClientRects().length && getComputedStyle(c).position !== 'absolute';
  const right = Math.max(...[...h.children].filter(shown).map(c => c.getBoundingClientRect().right));
  const label = document.getElementById('label').getBoundingClientRect().width;
  return [Math.max(0, right - end, h.scrollWidth - h.clientWidth), label]; })()"""


async def test_header_fits_a_720px_window(split: Split) -> None:
    """011-AC30: at 720 px a long label leaves no overflow and keeps 80 px; Open in Teams is an icon below
    a 640 px chat column, its name kept, and the members count goes. No overflow either at other widths,
    ⚙ included (011-AC31), docked, in a drawer or in list mode."""
    split.fake.rows[2]["label"] = "A fictional chat with a very long name about budget planning " * 3
    listed = await split.open(query="?chatnav=list", viewport={"width": 1300, "height": 700})
    page = await split.open(viewport={"width": 1300, "height": 700})
    # Each side of both breakpoints (chat width 640 and 400), docked list 300 px wide above 700.
    for p, widths in (
        (page, (400, 600, 701, 720, 760, 939, 941, 1024, 1300)),
        (listed, (360, 399, 401, 580, 639, 641, 1024, 1300)),
    ):
        for width in widths:
            await p.set_viewport_size({"width": width, "height": 700})
            overflow, label = await p.evaluate(_HEADER)
            assert (width, overflow) == (width, 0)
            assert label >= 80, width
            assert await p.locator("#gear").is_visible(), width
    await page.set_viewport_size({"width": 720, "height": 700})
    teams = page.locator("#teams")
    assert (await teams.inner_text()).strip() == "↗"
    assert await teams.get_attribute("aria-label") == "Open in Teams"
    assert not await page.locator("#members").is_visible()
    await page.set_viewport_size({"width": 941, "height": 700})  # a 641 px chat column
    assert (await teams.inner_text()).strip() == "Open in Teams"
    assert await page.locator("#members").is_visible()


_IN_VIEW = """(() => { const l = document.getElementById('rows'), c = l.querySelector('li.cur');
  const a = l.getBoundingClientRect(), b = c.getBoundingClientRect();
  return b.top >= a.top && b.bottom <= a.bottom; })()"""


async def test_current_row_is_marked_and_shown_on_load_and_back_not_on_updates(split: Split) -> None:
    """011-AC30: the current row has a fill and a 3 px bar (no outline); far down the list it is
    scrolled into view on load and on Back, never by a live update."""
    split.fake.rows = _board(400)
    page = await split.open(at=200, viewport={"width": 1300, "height": 800})
    cur = page.locator("#rows li.cur")
    assert await cur.get_attribute("data-id") == _id(200)
    assert await page.evaluate(_IN_VIEW)
    style = await cur.evaluate(
        "li => { const s = getComputedStyle(li); return [s.backgroundColor, s.boxShadow, s.outlineStyle]; }"
    )
    assert style[0] != "rgba(0, 0, 0, 0)"
    assert "3px 0px 0px 0px inset" in style[1]
    assert style[2] == "none"
    await page.locator("#rows").evaluate("l => { l.scrollTop = 0; }")
    split.fake.broadcast()
    await _until(page, "window.__frames.filter(f => 'rows' in f).length >= 2")
    assert await page.locator("#rows").evaluate("l => l.scrollTop") == 0
    await page.locator(f'#rows li[data-id="{_id(1)}"] .name').click()
    await _wait(lambda: split.fake.follows()[-1:] == [_id(1)])
    await page.go_back()
    await _wait(lambda: split.fake.follows()[-1:] == [_id(200)])
    assert await page.evaluate(_IN_VIEW)


async def test_empty_tabs_and_filters_say_so_under_the_bar(split: Split) -> None:
    """011-AC30: an empty filter result names the query right under the bar; an empty tab says which."""
    page = await split.open()
    await page.locator("#filter").fill("<b>nope</b>")
    nochat = page.locator("#nochat")
    assert await nochat.inner_text() == "no chat matches «<b>nope</b>»"
    assert await nochat.evaluate("n => n.getBoundingClientRect().top") < 150
    await page.locator("#clear").click()
    await page.get_by_role("button", name="@").click()
    assert await nochat.inner_text() == "No mentions"
    await page.get_by_role("button", name="Unread").click()
    assert await nochat.inner_text() == "No unread chats"
    split.fake.rows = [r for r in split.fake.rows if r["muted"] or r["seen_at"]]
    split.fake.broadcast()
    await page.get_by_role("button", name="Inbox").click()
    await _until(page, "document.getElementById('nochat').textContent === 'Inbox zero'")
    split.fake.rows = []
    split.fake.broadcast()
    await page.locator("#tabs .more").click()
    await page.get_by_role("menuitemradio", name="Seen").click()
    await _until(page, "document.getElementById('nochat').textContent === 'Nothing seen yet'")
    await page.locator("#tabs .more").click()
    await page.get_by_role("menuitemradio", name="Muted").click()
    assert await nochat.inner_text() == "Nothing muted"


async def test_focus_rings_use_the_accent_colour(split: Split) -> None:
    """011-AC30: the filter, the tabs, the current row's name and the separator ring in the accent."""
    page = await split.open(viewport={"width": 1300, "height": 800})
    accent = await page.evaluate(
        "(() => { const p = document.createElement('i'); p.style.color = 'var(--accent)';"
        " document.body.append(p);"
        " const c = getComputedStyle(p).color; p.remove(); return c; })()"
    )
    await page.locator("#filter").focus()
    rings = {}
    for _ in range(9):
        rings[await _focused(page)] = await page.evaluate(
            "(s => [s.outlineStyle, s.outlineColor])(getComputedStyle(document.activeElement))"
        )
        await page.keyboard.press("Tab")
    for key in ("#filter", "k:tab:all", "k:tab:more", "k:name", "#grip"):
        assert rings[key] == ["solid", accent], key


# An element's text against what is really under it: every ancestor's fill composited (alpha
# included) over white, the text faded by every ancestor's opacity.
_CONTRAST = """e => {
  const rgba = (c) => { const n = c.match(/[\\d.]+/g).map(Number), s = c.startsWith('color(') ? 255 : 1;
    return [n[0] * s, n[1] * s, n[2] * s, n.length > 3 ? n[3] : 1]; };
  const chain = [];
  for (let n = e; n; n = n.parentElement) chain.unshift(n);
  let bg = [255, 255, 255], op = 1;
  for (const n of chain) {
    const [r, g, b, a] = rgba(getComputedStyle(n).backgroundColor);
    bg = [r, g, b].map((v, i) => v * a + bg[i] * (1 - a));
    op *= Number(getComputedStyle(n).opacity);
  }
  const [r, g, b, a] = rgba(getComputedStyle(e).color), k = a * op;
  const fg = [r, g, b].map((v, i) => v * k + bg[i] * (1 - k));
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const lum = (c) => c.map(lin)
    .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_row_text_and_tab_labels_reach_4_5_to_1(split: Split, theme: str) -> None:
    """011-AC30: times, previews and names of seen rows, the time of the current row and of a mention
    row, a plain row's time and preview, and the inactive tab labels all contrast at least 4.5:1."""
    mention = {"kind": "me", "by": "Ann Lee", "text": "a question for Me", "at": _row(1)["last_activity"]}
    split.fake.rows[1]["mention"] = mention
    page = await split.open(query=f"?chatnav=split&theme={theme}&tab=seen")
    seen = f'#rows li.seen[data-id="{_id(5)}"]'
    measured = {}
    for part in ("name", "ago", "prev"):
        measured[f"seen {part}"] = await page.locator(f"{seen} .{part}").evaluate(_CONTRAST)
    await page.get_by_role("button", name="Inbox").click()
    plain = page.locator(f'#rows li[data-id="{_id(4)}"]')
    assert await plain.get_attribute("class") in (None, "")
    measured["plain ago"] = await plain.locator(".ago").evaluate(_CONTRAST)
    measured["plain prev"] = await plain.locator(".prev").evaluate(_CONTRAST)
    measured["current ago"] = await page.locator("#rows li.cur .ago").evaluate(_CONTRAST)
    measured["mention ago"] = await page.locator(f'#rows li.mention[data-id="{_id(1)}"] .ago').evaluate(
        _CONTRAST
    )
    tabs = await page.locator("#tabs button:not(.on)").evaluate_all(
        f"bs => bs.filter(b => b.getClientRects().length).map(b => [b.textContent, ({_CONTRAST})(b)])"
    )
    assert len(tabs) == 3  # @, Unread, ⋯
    measured |= {f"tab {name}": ratio for name, ratio in tabs}
    assert {k: v for k, v in measured.items() if v < 4.5} == {}


async def test_switching_chats_keeps_the_list_scroll(split: Split) -> None:
    """011-AC30: a click on another row keeps the list where it was, follows the row (URL, current mark)
    and puts the focus in the composer."""
    split.fake.rows = _board(400)
    page = await split.open(viewport={"width": 1300, "height": 800})
    rows = page.locator("#rows")
    await rows.evaluate("l => { l.scrollTop = 1500; }")
    target = await page.evaluate(
        """(() => { const l = document.getElementById('rows').getBoundingClientRect();
          const below = (li) => li.getBoundingClientRect().top > l.top + 100;
          return [...document.querySelectorAll('#rows li')].find(below).dataset.id; })()"""
    )
    await page.locator(f'#rows li[data-id="{target}"] .name').click()
    await _wait(lambda: split.fake.follows()[-1:] == [target])
    assert await rows.evaluate("l => l.scrollTop") == 1500
    assert await page.locator("#rows li.cur").get_attribute("data-id") == target
    assert page.url.endswith(quote(target, safe="") + "?chatnav=split")
    assert await _focused(page) == "#reply"


# --- fix round: reveal while hidden, drawer focus, click without drag, ⋯ menu keys ---


async def test_the_current_row_shows_when_a_hidden_list_opens(split: Split) -> None:
    """011-AC30: loaded with the list hidden (drawer closed), opening ☰ shows the current row; widened
    to a docked list, the next render centres it."""
    split.fake.rows = _board(400)
    page = await split.open(at=200, viewport={"width": 700, "height": 700})
    await page.click("#menu")
    assert await page.evaluate(_IN_VIEW)
    other = await split.open(at=200, viewport={"width": 700, "height": 700})
    await other.set_viewport_size({"width": 1300, "height": 700})
    split.fake.broadcast()
    await _until(other, "window.__frames.filter(f => 'rows' in f).length >= 2")
    assert await other.evaluate(_IN_VIEW)


async def test_the_drawer_keeps_tab_out_of_the_chat_under_it(split: Split) -> None:
    """011-AC29: with the drawer open, Tab cycles through the drawer and the header, never into the
    chat under the backdrop; closed, the composer is reachable again."""
    page = await split.open(viewport={"width": 600, "height": 700})
    await page.click("#menu")
    under = "document.activeElement.closest('#pane, #typing, footer') !== null"
    for _ in range(30):
        await page.keyboard.press("Tab")
        assert not await page.evaluate(under), await _focused(page)
    await page.locator("#filter").focus()
    await page.keyboard.press("Escape")
    await page.locator("#reply").focus()
    assert await _focused(page) == "#reply"


async def test_a_click_on_the_separator_stores_nothing(split: Split) -> None:
    """011-AC27: a click without a drag leaves `chatnav_w` unwritten."""
    page = await split.open(viewport={"width": 1300, "height": 800})
    await page.locator("#grip").click()
    assert await _stored(page, "chatnav_w") is None
    assert await _width(page) == 300


async def test_the_more_menu_is_one_tab_stop_and_closes_when_left(split: Split) -> None:
    """011-AC28: the ⋯ menu's items sit out of the Tab order and move with ↑/↓/Home/End; Tab out of the
    menu closes it; a second click on ⋯ closes it too."""
    page = await split.open()
    more = page.locator("#tabs .more")
    await more.focus()
    await page.keyboard.press("Enter")
    assert await _focused(page) == "k:menu:seen"
    assert await page.locator("#more button").evaluate_all("bs => bs.map(b => b.tabIndex)") == [-1, -1]
    for key, want in (("ArrowDown", "muted"), ("ArrowDown", "seen"), ("End", "muted"), ("Home", "seen")):
        await page.keyboard.press(key)
        assert await _focused(page) == f"k:menu:{want}", key
    await page.keyboard.press("Tab")
    await _until(page, "document.getElementById('more').hidden")
    assert await _focused(page) == "k:name"
    assert await more.get_attribute("aria-expanded") == "false"
    await more.click()
    assert await page.locator("#more").is_visible()
    box = await more.bounding_box()
    assert box is not None
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await page.mouse.down()
    await page.wait_for_timeout(100)  # a human press: the focusout check runs before the click
    await page.mouse.up()
    assert await page.locator("#more").is_hidden()


# --- AC31: the chat page's own ⚙ ---


async def _settings(page: Page) -> None:
    await page.locator("#gear").click()
    await page.locator("#cog .menu").wait_for()


async def test_settings_theme_applies_at_once_and_persists(split: Split) -> None:
    """011-AC31: ⚙ (named, aria-expanded) picks the theme: applied without a reload, stored under the
    widget's key, kept after a reload, and it outranks a `theme` URL parameter, which it drops."""
    page = await split.open(query="?chatnav=split&theme=light&tab=all")
    gear = page.locator("#gear")
    assert await gear.get_attribute("aria-label") == "settings"
    assert await gear.get_attribute("aria-expanded") == "false"
    assert await gear.get_attribute("aria-haspopup") is None  # a group of settings, not a menu
    assert await gear.get_attribute("aria-controls") == "settings"
    await page.evaluate("window.__same = 1")
    await _settings(page)
    assert await gear.get_attribute("aria-expanded") == "true"
    assert await page.locator("#settings").get_attribute("role") == "group"
    assert await page.locator("#cog .menu .on").first.inner_text() == "☀ Light"
    await page.get_by_role("button", name="☾ Dark").click()
    assert await page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert await _stored(page, "theme") == "dark"
    assert page.url.endswith("?chatnav=split&tab=all")
    assert await page.evaluate("window.__same") == 1
    await split.reload(page)
    assert await page.evaluate("document.documentElement.dataset.theme") == "dark"
    await _settings(page)
    await page.get_by_role("button", name="◐ Auto").click()
    assert await page.evaluate("'theme' in document.documentElement.dataset") is False


async def test_settings_avatars_toggle_messages_and_rows(split: Split) -> None:
    """011-AC31: the avatars switch hides and shows the avatars of messages and rows at once, stored."""
    page = await split.open()
    await _settings(page)
    switch = page.get_by_role("checkbox", name="Show avatars")
    assert await switch.is_checked()
    await switch.uncheck()
    assert await page.evaluate("document.body.classList.contains('compact')")
    assert await page.locator("#rows .rav").count() == 0
    assert not await page.locator(".msg .av").first.is_visible()
    assert await _stored(page, "avatars") == "0"
    await switch.check()
    assert await page.locator("#rows .rav").count() > 0
    assert await page.locator(".msg .av").first.is_visible()
    assert await _stored(page, "avatars") == "1"


async def test_settings_chat_navigation_reloads_into_split(split: Split) -> None:
    """011-AC31: from list mode, Split stores `chatnav` and reloads the page as a split, the draft kept."""
    page = await split.open(query="")
    assert not await page.locator("#side").is_visible()
    assert await page.locator("#gear").is_visible()
    await page.locator("#reply").fill("half a thought")
    await _settings(page)
    async with page.expect_navigation():
        await page.locator("#cog .menu").get_by_role("button", name="Split").click()
    await split.ready(page)
    assert await page.locator("#side").is_visible()
    assert await _stored(page, "chatnav") == "split"
    assert await page.locator("#reply").input_value() == "half a thought"


async def test_settings_chat_navigation_drops_the_url_parameter(split: Split) -> None:
    """011-AC31: opened on `?chatnav=split`, choosing List reloads in list mode without the parameter,
    the rest of the query kept."""
    page = await split.open(query="?chatnav=split&theme=dark")
    await _settings(page)
    async with page.expect_navigation():
        await page.locator("#cog .menu").get_by_role("button", name="List").click()
    await split.ready(page)
    assert page.url.endswith(f"/chat/{quote(_id(2), safe='')}?theme=dark")
    assert not await page.locator("#side").is_visible()
    assert await page.locator("#back").is_visible()
    assert await _stored(page, "chatnav") == "list"


async def test_settings_menu_keyboard_escape_and_outside_click(split: Split) -> None:
    """011-AC31: Enter on ⚙ opens the menu on the current theme; Escape closes it back to ⚙; Tab out of
    it and a click outside close it too."""
    page = await split.open()
    gear, menu = page.locator("#gear"), page.locator("#cog .menu")
    await gear.focus()
    await page.keyboard.press("Enter")
    assert await page.evaluate("document.activeElement.textContent") == "◐ Auto"
    await page.keyboard.press("Escape")
    assert await menu.count() == 0
    assert await _focused(page) == "#gear"
    assert await gear.get_attribute("aria-expanded") == "false"
    narrow = await split.open(viewport={"width": 600, "height": 700})
    await narrow.click("#menu")
    await _settings(narrow)
    await narrow.keyboard.press("Escape")  # closes the menu only, the drawer stays open
    assert await _focused(narrow) == "#gear"
    assert await narrow.locator("#side").is_visible()
    await page.keyboard.press("Enter")
    for _ in range(6):  # Light, Dark, the switch, List, Split, then out
        await page.keyboard.press("Tab")
    await _until(page, "!document.querySelector('#cog .menu')")
    await _settings(page)
    await page.locator("#label").click()  # outside: the focus leaves the menu
    await _until(page, "!document.querySelector('#cog .menu')")


async def test_avatars_changed_in_another_tab_reach_the_rows(split: Split) -> None:
    """011-AC31: avatars switched off in another page of the origin (the widget) leave the list's rows and
    the messages without avatars at once, not at the next refresh."""
    page = await split.open()
    assert await page.locator("#rows .rav").count() > 0
    other = await page.context.new_page()
    await other.goto(page.url.split("?")[0] + "?chatnav=list")
    await other.evaluate("localStorage.setItem('avatars', '0')")
    await _until(page, "document.body.classList.contains('compact')")
    assert await page.locator("#rows .rav").count() == 0
