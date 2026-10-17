"""Chat sections in the list (spec 026): the widget and the chat page's split list against a real
board and its sections store. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_sections.py`.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import browser_helpers
import pytest
import test_web_chat
from browser_helpers import BrowserContext, Page, Pages, row, until, wait  # skips without playwright
from test_web_chat import ME, _archive, _at, _m, _served

from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.config import Settings
from miniteams.sections import Sections

pytestmark = [pytest.mark.browser, pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)]
pages = browser_helpers.pages
quiet_directory = test_web_chat.quiet_directory
VIEWS = pytest.mark.parametrize("view", ["widget", "split"])

A, B, C = "19:sa@thread.v2", "19:sb@thread.v2", "19:sc@thread.v2"
NAME = {A: "Fictional A", B: "Fictional B", C: "Fictional C"}
# The list as shown: a header is `#<name>`, ` ▸` when folded; a chat its title.
ITEMS = """[...document.querySelectorAll('#rows > li')]
  .filter(li => li.dataset.id || li.dataset.sid !== undefined)
  .map(li => li.dataset.sid === undefined ? li.querySelector('.name').textContent
    : '#' + li.querySelector('.sname').textContent
      + (li.querySelector('.shead').getAttribute('aria-expanded') === 'false' ? ' ▸' : ''))"""
KEY = "(li => li && (li.dataset.id ?? '#' + li.dataset.sid))"  # an item's key, `#<sid>` for a header
SELECTED = f"{KEY}(document.querySelector('#rows > li.sel'))"
FOCUSED = f"{KEY}(document.activeElement.closest('#rows > li'))"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    for t in (A, B, C):
        _archive(path, t, [_m("1", _at(1))])
    return path


@pytest.fixture
def board(quiet_directory: Any, data_dir: Path, tmp_path: Path) -> W.Board:
    """A newest, then B, then C."""
    rows = {t: row(t, NAME[t], _at(i), last_id=str(i)) for t, i in ((A, 30), (B, 20), (C, 10))}
    return W.Board(rows, quiet_directory, me=ME, data_dir=data_dir, sections_path=tmp_path / "sections.json")


@dataclass
class Views:
    board: W.Board
    pages: Pages

    async def open(self, view: str, context: BrowserContext | None = None, tab: str = "") -> Page:
        """The widget or the split chat page (on A); `context` shares an earlier page's storage."""
        path = "/" if view == "widget" else f"/chat/{quote(A, safe='')}?chatnav=split"
        if tab:
            path += ("&" if "?" in path else "?") + f"tab={tab}"
        if context is None:
            page = await self.pages.open(path)
        else:
            page = await context.new_page()
            self.pages.watch(page)
            await page.goto(f"http://{self.pages.host}{path}")
        await self.ready(page)
        return page

    async def ready(self, page: Page) -> None:
        if "/chat/" in page.url:
            await until(page, "window.__frames.some(f => 'history' in f)")
        await until(page, "window.__frames.some(f => 'rows' in f)")

    async def reload(self, page: Page) -> None:
        await page.reload()
        await self.ready(page)

    def change(self, verb: str, *args: Any) -> Any:
        """A sections change made by the server, broadcast to every page."""
        result = getattr(self.board.sections, verb)(*args)
        self.board.sections.save()
        self.board.broadcast()
        return result

    def sid(self, name: str) -> str:
        return next(s["id"] for s in self.board.sections.items if s["name"] == name)

    def names(self) -> list[str]:
        return [s["name"] for s in self.board.sections.items]

    def chats(self, name: str) -> list[str]:
        return next(s["chats"] for s in self.board.sections.items if s["name"] == name)


@pytest.fixture
async def views(
    board: W.Board, settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Views]:
    monkeypatch.setattr(CV, "fetch_history", lambda *a: [])
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield Views(board, pages)
        await pages.close()
    assert pages.external == []


async def _items(page: Page) -> list[str]:
    return list(await page.evaluate(ITEMS))


async def _list_is(page: Page, expected: list[str]) -> None:
    seen: list[str] = []
    for _ in range(500):
        seen = await _items(page)
        if seen == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"list shows {seen}, expected {expected}")


async def _selected_is(page: Page, key: str) -> None:
    await until(page, f"{SELECTED} === {json.dumps(key)}")


async def _sections_mode(page: Page) -> None:
    await page.locator('#mode [data-k="mode:sections"]').click()
    await until(page, "!!document.querySelector('#rows > li.sect')")


def _row(page: Page, thread_id: str) -> Any:
    return page.locator(f'#rows > li[data-id="{thread_id}"]')


def _head(page: Page, sid: str) -> Any:
    return page.locator(f'#rows > li.sect[data-sid="{sid}"]')


async def _menu(li: Any) -> None:
    """Opens the ⋯ menu of a row or a header (its button shows on hover)."""
    await li.hover()
    await li.locator("[data-k=more]").click()
    await li.locator(".rowmenu").wait_for()


async def _filter(page: Page) -> Any:
    return page.locator("#q" if await page.locator("#q").count() else "#filter")


# --- AC1: the switch appears with the first section, made from a chat's ⋯ ---


@VIEWS
async def test_no_section_looks_as_today_and_new_section_files_the_chat(views: Views, view: str) -> None:
    """026-AC1: no section, no switch, the flat list; ⋯ → Section ▸ → New section… asks inline, creates the
    section with the chat in it and shows the switch; the list stays Recent until switched."""
    page = await views.open(view)
    assert await page.locator("#mode").is_hidden()
    assert await _items(page) == [NAME[A], NAME[B], NAME[C]]
    await _menu(_row(page, B))
    await page.locator(".rowmenu [data-k=file]").click()
    await page.locator(".rowmenu [data-k=new]").click()
    box = page.locator(".rowmenu input")
    assert await box.evaluate("e => e === document.activeElement")
    await box.fill("Work")
    await box.press("Enter")
    await wait(lambda: views.names() == ["Work"], "section created")
    assert views.chats("Work") == [B]
    await page.locator("#mode").wait_for(state="visible")
    assert await page.locator(".rowmenu").count() == 0
    await _list_is(page, [NAME[A], NAME[B], NAME[C]])
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[B], "#Other", NAME[A], NAME[C]])


# --- AC2: grouped, in section order, recency inside, Other last ---


@VIEWS
async def test_sections_mode_groups_in_section_order_by_recency(views: Views, view: str) -> None:
    """026-AC2: headers in the saved order, chats by recency under each, the rest under Other, last."""
    views.change("create", "Home")
    views.change("create", "Work", C)
    views.change("file", A, views.sid("Work"))
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Home", "#Work", NAME[A], NAME[C], "#Other", NAME[B]])
    views.change("move", views.sid("Work"), -1)
    await _list_is(page, ["#Work", NAME[A], NAME[C], "#Home", "#Other", NAME[B]])


# --- AC3: the mode, per view ---


async def test_each_view_remembers_its_own_mode(views: Views) -> None:
    """026-AC3: switching the widget to Sections leaves the chat page in Recent; both survive a reload."""
    views.change("create", "Work", B)
    widget = await views.open("widget")
    await _sections_mode(widget)
    split = await views.open("split", widget.context)
    await _list_is(split, [NAME[A], NAME[B], NAME[C]])
    assert await split.locator('#mode [data-k="mode:recent"]').get_attribute("aria-pressed") == "true"
    await views.reload(widget)
    await _list_is(widget, ["#Work", NAME[B], "#Other", NAME[A], NAME[C]])
    await split.locator('#mode [data-k="mode:sections"]').click()
    await split.locator('#mode [data-k="mode:recent"]').click()
    await views.reload(split)
    await _list_is(split, [NAME[A], NAME[B], NAME[C]])
    await _list_is(widget, ["#Work", NAME[B], "#Other", NAME[A], NAME[C]])


# --- AC4: tabs filter in both modes ---


@VIEWS
async def test_tabs_filter_in_both_modes_and_a_seen_chat_leaves_its_section(views: Views, view: str) -> None:
    """026-AC4: a chat marked seen leaves its section in Inbox, in both modes, and shows under it in Seen."""
    views.change("create", "Work", B)
    views.change("file", A, views.sid("Work"))
    views.board.rows[A]["seen_at"] = "2027-01-01T00:00:00.000Z"
    page = await views.open(view)
    await _list_is(page, [NAME[B], NAME[C]])
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[B], "#Other", NAME[C]])
    seen = await views.open(view, page.context, tab="seen")
    await _list_is(seen, ["#Work", NAME[A], "#Other"])
    recent = await views.open(view, tab="seen")  # a fresh browser: Recent
    await _list_is(recent, [NAME[A]])


# --- AC5: empty sections, and a search ---


@VIEWS
async def test_empty_sections_stay_and_a_search_shows_only_what_it_reaches(views: Views, view: str) -> None:
    """026-AC5: an empty section shows in every tab; a search (quick filter, Ctrl+K on the chat page) shows
    only the sections holding a result, unfolded; a fold made meanwhile is forgotten with it."""
    views.change("create", "Empty")
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    await _head(page, views.sid("Work")).locator(".shead").click()
    await _list_is(page, ["#Empty", "#Work ▸", "#Other", NAME[A], NAME[C]])
    muted = await views.open(view, page.context, tab="muted")
    await _list_is(muted, ["#Empty", "#Work ▸", "#Other"])
    box = await _filter(page)
    if view == "split":
        await page.keyboard.press("Control+k")
        assert await box.evaluate("e => e === document.activeElement")
    await box.fill("fictional b")
    await _list_is(page, ["#Work", NAME[B]])
    await _head(page, views.sid("Work")).locator(".shead").click()
    await _list_is(page, ["#Work ▸"])
    await box.fill("")
    await _list_is(page, ["#Empty", "#Work ▸", "#Other", NAME[A], NAME[C]])
    await _head(page, views.sid("Work")).locator(".shead").click()
    await box.fill("fictional")
    await _head(page, views.sid("Work")).locator(".shead").click()
    await _list_is(page, ["#Work ▸", "#Other", NAME[A], NAME[C]])
    await box.fill("")
    await _list_is(page, ["#Empty", "#Work", NAME[B], "#Other", NAME[A], NAME[C]])


# --- AC6: the accordion ---


@VIEWS
async def test_a_folded_section_counts_unread_and_mentions_and_stays_folded(views: Views, view: str) -> None:
    """026-AC6: folded, a header shows its unread count and `@` by the tabs' rules, with text alternatives;
    an arrival keeps it folded; the fold survives a reload and is this view's only."""
    views.board.rows[B]["unread"] = True
    views.board.rows[C]["mention"] = {"kind": "me", "by": "Ann Lee", "text": "hi", "at": _at(10)}
    views.change("create", "Work", B)
    views.change("file", C, views.sid("Work"))
    page = await views.open(view)
    await _sections_mode(page)
    head = _head(page, views.sid("Work"))
    assert await head.locator(".shead b").count() == 0  # unfolded: the chats show for themselves
    await head.locator(".shead").click()
    await _list_is(page, ["#Work ▸", "#Other", NAME[A]])
    assert await head.locator(".shead b").all_inner_texts() == ["1", "@"]
    assert await head.locator(".shead").evaluate("b => b.textContent") == "▸Work1 unread@ mentioned"
    assert await head.locator(".fold").get_attribute("aria-hidden") == "true"  # aria-expanded says it
    views.board.rows[C].update(unread=True, last_activity=_at(50))
    views.board.broadcast()
    await until(page, "document.querySelector('li.sect b.n')?.textContent === '2'")
    await _list_is(page, ["#Work ▸", "#Other", NAME[A]])
    await views.reload(page)
    await _list_is(page, ["#Work ▸", "#Other", NAME[A]])
    other = await views.open("split" if view == "widget" else "widget", page.context)
    await _sections_mode(other)
    await _list_is(other, ["#Work", NAME[C], NAME[B], "#Other", NAME[A]])


# --- AC7: managing by menu ---


@VIEWS
async def test_menus_file_create_rename_move_and_delete(views: Views, view: str) -> None:
    """026-AC7: Section ▸ files a chat in a section or None; a header's ⋯ renames inline, moves up and
    down (not past an end) and deletes after an inline confirmation, its chats going to Other."""
    views.change("create", "Home")
    views.change("create", "Work", B)
    home, work = views.sid("Home"), views.sid("Work")
    page = await views.open(view)
    await _sections_mode(page)
    await _menu(_row(page, A))
    await page.locator(".rowmenu [data-k=file]").click()
    assert await page.locator(".rowmenu [data-k=sec]").all_inner_texts() == ["Home", "Work"]
    assert await page.locator(".rowmenu [data-k=none]").inner_text() == "✓ None"
    await page.locator(".rowmenu [data-k=sec]", has_text="Home").click()
    await wait(lambda: views.chats("Home") == [A], "filed")
    await _list_is(page, ["#Home", NAME[A], "#Work", NAME[B], "#Other", NAME[C]])
    await _menu(_row(page, B))
    await page.locator(".rowmenu [data-k=file]").click()
    assert await page.locator(".rowmenu [data-k=sec]", has_text="Work").inner_text() == "✓ Work"
    await page.locator(".rowmenu [data-k=none]").click()
    await _list_is(page, ["#Home", NAME[A], "#Work", "#Other", NAME[B], NAME[C]])
    await _menu(_head(page, work))
    await page.locator(".rowmenu [data-k=rename]").click()
    box = page.locator(".rowmenu input")
    assert await box.input_value() == "Work"
    await box.fill("Jobs")
    await box.press("Enter")
    await _list_is(page, ["#Home", NAME[A], "#Jobs", "#Other", NAME[B], NAME[C]])
    await _menu(_head(page, home))
    assert await page.locator(".rowmenu [data-k=up]").count() == 0
    await page.locator(".rowmenu [data-k=down]").click()
    await wait(lambda: views.names() == ["Jobs", "Home"], "moved down")
    await _menu(_head(page, home))
    assert await page.locator(".rowmenu [data-k=down]").count() == 0
    await page.locator(".rowmenu [data-k=up]").click()
    await wait(lambda: views.names() == ["Home", "Jobs"], "moved up")
    await _menu(_head(page, home))
    await page.locator(".rowmenu [data-k=delete]").click()
    assert await page.locator(".rowmenu p").inner_text() == "Delete Home? Its chats go to Other."
    await page.locator(".rowmenu [data-k=cancel]").click()
    assert views.names() == ["Home", "Jobs"]
    await page.locator(".rowmenu [data-k=delete]").click()
    await page.locator(".rowmenu [data-k=ok]").click()
    await wait(lambda: views.names() == ["Jobs"], "deleted")
    await _list_is(page, ["#Jobs", "#Other", NAME[A], NAME[B], NAME[C]])


@VIEWS
async def test_escape_steps_back_out_of_a_menu_page(views: Views, view: str) -> None:
    """026-AC7: Escape in an inner page (the name box) goes back to the menu, a second one closes it."""
    views.change("create", "Work")
    page = await views.open(view)
    await _menu(_row(page, A))
    await page.locator(".rowmenu [data-k=file]").click()
    await page.locator(".rowmenu [data-k=new]").click()
    await page.locator(".rowmenu input").press("Escape")
    await page.locator(".rowmenu [data-k=file]").wait_for()
    await page.keyboard.press("Escape")
    await until(page, "!document.querySelector('.rowmenu')")
    assert views.names() == ["Work"]


# --- AC9: keyboard ---


@VIEWS
async def test_arrows_walk_headers_and_chats_and_fold_from_the_filter_box(views: Views, view: str) -> None:
    """026-AC9: from the filter box, ↑/↓ walk headers and chats, a folded section one step; on a header
    Enter, ← and → fold and unfold; ← on a chat reaches its header."""
    views.change("create", "Work", B)
    views.change("create", "Home", C)
    work, home = views.sid("Work"), views.sid("Home")
    page = await views.open(view)
    await _sections_mode(page)
    box = await _filter(page)
    await box.focus()
    await box.press("ArrowDown")
    await _selected_is(page, "#" + work)
    await box.press("ArrowDown")
    await _selected_is(page, B)
    await box.press("ArrowLeft")
    await _selected_is(page, "#" + work)
    await box.press("ArrowLeft")
    await _list_is(page, ["#Work ▸", "#Home", NAME[C], "#Other", NAME[A]])
    await box.press("ArrowDown")
    await _selected_is(page, "#" + home)
    await box.press("Enter")
    await _list_is(page, ["#Work ▸", "#Home ▸", "#Other", NAME[A]])
    await box.press("ArrowRight")
    await _list_is(page, ["#Work ▸", "#Home", NAME[C], "#Other", NAME[A]])
    await box.press("ArrowUp")
    await box.press("ArrowRight")
    await _list_is(page, ["#Work", NAME[B], "#Home", NAME[C], "#Other", NAME[A]])
    assert await box.evaluate("e => e === document.activeElement")
    await box.fill("fic")  # a typed text keeps ←/→ for its caret
    await box.press("ArrowLeft")
    await _list_is(page, ["#Work", NAME[B], "#Home", NAME[C], "#Other", NAME[A]])


async def test_split_list_keys_move_the_focus_over_headers(views: Views) -> None:
    """026-AC9: in the split list the focus walks headers and chats; ← / → / Enter fold a header with the
    focus kept on it; ← on a chat focuses its header; Ctrl+K still opens the filter."""
    views.change("create", "Work", B)
    views.change("create", "Home", C)
    work = views.sid("Work")
    page = await views.open("split")
    await _sections_mode(page)
    await _head(page, work).locator(".shead").focus()
    await page.keyboard.press("ArrowDown")
    await until(page, f"{FOCUSED} === {json.dumps(B)}")
    await page.keyboard.press("ArrowLeft")
    await until(page, f"{FOCUSED} === {json.dumps('#' + work)}")
    await page.keyboard.press("ArrowLeft")
    await _list_is(page, ["#Work ▸", "#Home", NAME[C], "#Other", NAME[A]])
    await until(page, f"{FOCUSED} === {json.dumps('#' + work)}")
    assert await _head(page, work).locator(".shead").get_attribute("tabindex") == "0"
    await page.keyboard.press("Enter")
    await _list_is(page, ["#Work", NAME[B], "#Home", NAME[C], "#Other", NAME[A]])
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("ArrowDown")
    await until(page, f"{FOCUSED} === {json.dumps('#' + views.sid('Home'))}")
    await page.keyboard.press("Control+k")
    await until(page, "document.activeElement.id === 'filter'")


# --- AC10: sync ---


async def test_a_change_in_one_view_shows_in_the_other_and_survives_a_restart(views: Views) -> None:
    """026-AC10: a section made in the widget shows in the split list, a rename there shows in the widget,
    without a reload; the saved file reads back the same."""
    widget = await views.open("widget")
    split = await views.open("split")
    await _menu(_row(widget, C))
    await widget.locator(".rowmenu [data-k=file]").click()
    await widget.locator(".rowmenu [data-k=new]").click()
    await widget.locator(".rowmenu input").fill("Work")
    await widget.locator(".rowmenu [data-k=ok]").click()
    await split.locator("#mode").wait_for(state="visible")
    await _sections_mode(split)
    await _sections_mode(widget)
    await _list_is(split, ["#Work", NAME[C], "#Other", NAME[A], NAME[B]])
    await _menu(_head(split, views.sid("Work")))
    await split.locator(".rowmenu [data-k=rename]").click()
    await split.locator(".rowmenu input").fill("Jobs")
    await split.locator(".rowmenu [data-k=ok]").click()
    await _list_is(widget, ["#Jobs", NAME[C], "#Other", NAME[A], NAME[B]])
    assert views.board.sections.path is not None
    assert Sections(views.board.sections.path, lambda: views.board.rows).items == views.board.sections.items


# --- AC11: names are plain text, bounded; unknown chats refused ---


@VIEWS
async def test_a_section_name_is_plain_text_non_empty_and_bounded(views: Views, view: str) -> None:
    """026-AC11: markup in a name shows as text; an empty name sends nothing; the box takes 60 characters;
    a chat unknown to the board is refused."""
    page = await views.open(view)
    await _menu(_row(page, A))
    await page.locator(".rowmenu [data-k=file]").click()
    await page.locator(".rowmenu [data-k=new]").click()
    box = page.locator(".rowmenu input")
    await box.fill("   ")
    await box.press("Enter")
    assert await box.is_visible()
    await box.fill("x" * 70)
    assert await box.input_value() == "x" * 60
    evil = '<b>x</b><img src=x onerror="window.pwned=1">'
    await box.fill(evil)
    await box.press("Enter")
    await wait(lambda: views.names() == [evil], "created")
    await _sections_mode(page)
    head = _head(page, views.sid(evil))
    assert await head.locator(".sname").evaluate("e => e.textContent") == evil
    assert await head.locator(".sname *").count() == 0
    await page.evaluate(
        "window.__ws.send(JSON.stringify({section_new: 'Ghost', chat: '19:unknown@thread.v2'}))"
    )
    await page.evaluate("window.__ws.send(JSON.stringify({section_new: 'Real'}))")
    await wait(lambda: "Real" in views.names(), "the later verb")
    assert views.names() == [evil, "Real"]
    assert not await page.evaluate("window.pwned === 1")


# --- AC12: manual order ---


@VIEWS
async def test_a_manual_section_keeps_its_order_and_moves_with_alt_arrows(views: Views, view: str) -> None:
    """026-AC12: Manual keeps the order on an arrival; a newly filed chat lands on top; Alt+↑/↓ moves a
    chat; the order survives a reload and shows in the other view; Recent orders by activity again."""
    views.change("create", "Work", B)
    views.change("file", A, views.sid("Work"))
    work = views.sid("Work")
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[A], NAME[B], "#Other", NAME[C]])
    await _menu(_head(page, work))
    await page.locator(".rowmenu [data-k='order:manual']").click()
    await wait(lambda: views.board.sections.items[0]["order"] == "manual", "manual")
    views.board.rows[B]["last_activity"] = _at(40)
    views.board.broadcast()
    await until(page, f"window.__frames.filter(f => 'rows' in f).at(-1).rows[0].id === {json.dumps(B)}")
    await _list_is(page, ["#Work", NAME[A], NAME[B], "#Other", NAME[C]])
    await _menu(_row(page, C))
    await page.locator(".rowmenu [data-k=file]").click()
    await page.locator(".rowmenu [data-k=sec]").click()
    await _list_is(page, ["#Work", NAME[C], NAME[A], NAME[B], "#Other"])
    if view == "widget":
        box = page.locator("#q")
        await box.focus()
        for _ in range(2):
            await box.press("ArrowDown")
        await _selected_is(page, C)
        await box.press("Alt+ArrowDown")
    else:
        await _row(page, C).locator(".name").focus()
        await page.keyboard.press("Alt+ArrowDown")
    await wait(lambda: views.chats("Work") == [A, C, B], "moved")
    await _list_is(page, ["#Work", NAME[A], NAME[C], NAME[B], "#Other"])
    if view == "split":
        await until(page, f"document.activeElement.closest('li')?.dataset.id === {json.dumps(C)}")
    await views.reload(page)
    await _list_is(page, ["#Work", NAME[A], NAME[C], NAME[B], "#Other"])
    other = await views.open("split" if view == "widget" else "widget")
    await _sections_mode(other)
    await _list_is(other, ["#Work", NAME[A], NAME[C], NAME[B], "#Other"])
    await _menu(_head(page, work))
    await page.locator(".rowmenu [data-k='order:recent']").click()
    await _list_is(page, ["#Work", NAME[B], NAME[A], NAME[C], "#Other"])


# --- review follow-ups ---


async def test_a_menu_opened_upwards_leaves_the_next_one_downwards(views: Views) -> None:
    """A low row's ⋯ opens upwards; after it, the first row's ⋯ opens downwards again."""
    for i in range(12):
        tid = f"19:r{i:02d}@thread.v2"
        views.board.rows[tid] = row(tid, f"Fictional r{i:02d}", _at(9 - i % 9))
    page = await views.open("widget")
    await page.set_viewport_size({"width": 600, "height": 260})
    low = page.locator("#rows > li").nth(4)
    await low.evaluate("li => li.scrollIntoView({ block: 'end' })")
    await _menu(low)
    assert await low.locator(".rowmenu.up").count() == 1
    await page.keyboard.press("Escape")
    await page.evaluate("scrollTo(0, 0)")
    first = page.locator("#rows > li").first
    await _menu(first)
    assert await first.locator(".rowmenu.up").count() == 0


# Records whether the page took a modified ←, then keeps the browser from acting on it (Alt+← is Back).
MOD_LEFT = """addEventListener("keydown", (e) => {
  if ((e.altKey || e.ctrlKey) && e.key === "ArrowLeft") {
    window.__taken = e.defaultPrevented; e.preventDefault(); } })"""


@VIEWS
@pytest.mark.parametrize("mod", ["Alt", "Control"])
async def test_a_modified_left_is_left_to_the_browser(views: Views, view: str, mod: str) -> None:
    """026-AC9: Alt+← (browser Back) or Ctrl+← on a header or a chat is not taken by the list, nor folds."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    await page.evaluate(MOD_LEFT)
    before = await _items(page)
    if view == "widget":
        box = page.locator("#q")
        targets = [box, box]
        await box.focus()
        await box.press("ArrowDown")
    else:
        targets = [_head(page, views.sid("Work")).locator(".shead"), _row(page, B).locator(".name")]
    for i, target in enumerate(targets):
        if view == "widget" and i:
            await box.press("ArrowDown")
        else:
            await target.focus()
        await page.evaluate("window.__taken = null")
        await target.press(f"{mod}+ArrowLeft")
        assert await page.evaluate("window.__taken") is False
        assert await _items(page) == before


@VIEWS
async def test_alt_arrows_move_over_the_chats_shown_only(views: Views, view: str) -> None:
    """026-AC12: in Manual [A, B, C] with B seen (out of Inbox), Alt+↓ on A shows C, A; Alt+↑ back over the
    hidden B shows A, C."""
    views.change("create", "Work", C)
    views.change("file", B, views.sid("Work"))
    views.change("file", A, views.sid("Work"))
    views.change("set_order", views.sid("Work"), "manual")
    assert views.chats("Work") == [A, B, C]
    views.board.rows[B]["seen_at"] = "2027-01-01T00:00:00.000Z"
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[A], NAME[C], "#Other"])
    if view == "widget":
        box = page.locator("#q")
        await box.focus()
        await box.press("ArrowDown")
        await box.press("ArrowDown")
        await _selected_is(page, A)
        await box.press("Alt+ArrowDown")
    else:
        await _row(page, A).locator(".name").focus()
        await page.keyboard.press("Alt+ArrowDown")
    await _list_is(page, ["#Work", NAME[C], NAME[A], "#Other"])
    assert views.chats("Work") == [B, C, A]
    await (page.locator("#q") if view == "widget" else page.locator(":focus")).press("Alt+ArrowUp")
    await _list_is(page, ["#Work", NAME[A], NAME[C], "#Other"])
    assert views.chats("Work") == [B, A, C]


@VIEWS
@pytest.mark.parametrize("key", ["Enter", " "])
async def test_the_mode_switch_keeps_the_focus(views: Views, view: str, key: str) -> None:
    """026-AC3: Enter or Space on Recent / Sections switches and leaves the focus on the switch."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await page.locator('#mode [data-k="mode:sections"]').focus()
    await page.keyboard.press(key)
    await until(page, "!!document.querySelector('#rows > li.sect')")
    assert await page.evaluate("document.activeElement.dataset.k") == "mode:sections"


async def test_home_and_end_reach_headers_in_the_split_list(views: Views) -> None:
    """026-AC9: Home and End reach the first and last items, headers included."""
    views.change("create", "Work", B)
    page = await views.open("split")
    await _sections_mode(page)
    await _row(page, B).locator(".name").focus()
    await page.keyboard.press("End")
    await until(page, f"{FOCUSED} === {json.dumps(C)}")
    await page.keyboard.press("Home")
    await until(page, f"{FOCUSED} === {json.dumps('#' + views.sid('Work'))}")


@VIEWS
async def test_a_broadcast_during_a_rename_keeps_the_text_and_the_caret(views: Views, view: str) -> None:
    """026-AC10: a broadcast landing while a name is typed keeps its text, focus and caret."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    await _menu(_head(page, views.sid("Work")))
    await page.locator(".rowmenu [data-k=rename]").click()
    box = page.locator(".rowmenu input")
    await box.fill("Jobs")
    await box.evaluate("e => e.setSelectionRange(2, 2)")
    n = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    views.board.broadcast()
    await until(page, f"window.__frames.filter(f => 'rows' in f).length > {n}")
    state = await box.evaluate("e => [e.value, e === document.activeElement, e.selectionStart]")
    assert state == ["Jobs", True, 2]
    await box.press("X")
    assert await box.input_value() == "JoXbs"


# --- round 2 ---

MOVES = "window.__out.filter(v => 'section_chat_move' in v).length"


@VIEWS
async def test_folding_moves_a_selected_chat_to_its_header(views: Views, view: str) -> None:
    """026-AC6, 026-AC9: a chat selected in a section folded by click hands the selection to the header:
    Tab opens no reply for it, Alt+↓ moves nothing."""
    views.change("create", "Work", C)
    views.change("file", B, views.sid("Work"))
    views.change("set_order", views.sid("Work"), "manual")
    work = views.sid("Work")
    page = await views.open(view)
    await _sections_mode(page)
    box = await _filter(page)
    await box.focus()
    await box.press("ArrowDown")
    await box.press("ArrowDown")
    await _selected_is(page, B)
    await _head(page, work).locator(".shead").click()
    await _selected_is(page, "#" + work)
    await box.focus()
    await box.press("Alt+ArrowDown")
    if view == "widget":
        await box.press("Tab")
        assert not await page.evaluate("!!document.getElementById('reply')?.isConnected")
    assert await page.evaluate(MOVES) == 0
    assert views.chats("Work") == [B, C]


@VIEWS
async def test_switching_to_sections_never_selects_a_folded_chat(views: Views, view: str) -> None:
    """026-AC6: a chat selected in Recent that lands in a folded section hands the selection to its header."""
    views.change("create", "Work", B)
    work = views.sid("Work")
    page = await views.open(view)
    await _sections_mode(page)
    await _head(page, work).locator(".shead").click()
    await page.locator('#mode [data-k="mode:recent"]').click()
    box = await _filter(page)
    await box.focus()
    await box.press("ArrowDown")
    await box.press("ArrowDown")
    await _selected_is(page, B)
    await page.locator('#mode [data-k="mode:sections"]').click()
    await _selected_is(page, "#" + work)


@VIEWS
async def test_a_composition_in_the_name_box_survives_a_broadcast(views: Views, view: str) -> None:
    """026-AC10: a broadcast landing during an IME composition in the name box leaves the box in place (a
    move would cancel it); the list catches up once the composition ends."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    await _menu(_head(page, views.sid("Work")))
    await page.locator(".rowmenu [data-k=rename]").click()
    box = page.locator(".rowmenu input")
    await box.evaluate("""e => { window.__li = e.closest('li'); window.__blurs = 0;
      e.addEventListener('blur', () => window.__blurs++);
      e.dispatchEvent(new CompositionEvent('compositionstart')); }""")
    views.board.rows[C]["label"] = "Fictional C renamed"
    n = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    views.board.broadcast()
    await until(page, f"window.__frames.filter(f => 'rows' in f).length > {n}")
    js = "e => [e.closest('li') === window.__li, e.isConnected, e === document.activeElement, window.__blurs]"
    assert await box.evaluate(js) == [True, True, True, 0]
    assert NAME[C] in await _items(page)
    await box.evaluate("e => e.dispatchEvent(new CompositionEvent('compositionend'))")
    await until(
        page, "[...document.querySelectorAll('#rows .name')].some(n => n.textContent.endsWith('renamed'))"
    )
    assert await box.evaluate("e => e === document.activeElement")


async def _focus_is(page: Page, key: str) -> None:
    await until(page, f"{FOCUSED} === {json.dumps(key)}")


@VIEWS
async def test_deleting_a_section_moves_the_focus_to_the_next_item(views: Views, view: str) -> None:
    """026-AC7: a delete confirmed from the keyboard leaves the focus on the next item (its first chat,
    now under Other), on the filter box when the list is left empty, and on the filter box when the
    last section goes while the mode switch holds it."""
    views.change("create", "Home")
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    await _menu(_head(page, views.sid("Work")))
    await page.locator(".rowmenu [data-k=delete]").click()
    await page.locator(".rowmenu [data-k=ok]").press("Enter")
    await wait(lambda: views.names() == ["Home"], "deleted")
    await _list_is(page, ["#Home", "#Other", NAME[A], NAME[B], NAME[C]])
    await _focus_is(page, B)
    muted = await views.open(view, page.context, tab="muted")
    await _list_is(muted, ["#Home", "#Other"])
    await _menu(_head(muted, views.sid("Home")))
    await muted.locator(".rowmenu [data-k=delete]").click()
    await muted.locator(".rowmenu [data-k=ok]").press("Enter")
    await wait(lambda: views.names() == [], "deleted")
    filter_id = "q" if view == "widget" else "filter"
    await until(muted, f"document.activeElement.id === {json.dumps(filter_id)}")
    views.change("create", "Last")
    await page.locator("#mode").wait_for(state="visible")
    await page.locator('#mode [data-k="mode:sections"]').focus()
    views.change("delete", views.sid("Last"))
    await page.locator("#mode").wait_for(state="hidden")
    await until(page, f"document.activeElement.id === {json.dumps(filter_id)}")


# --- round 3 ---


@VIEWS
async def test_filing_the_selected_chat_in_a_folded_section_focuses_the_header(
    views: Views, view: str
) -> None:
    """026-AC6: the selected chat filed from its menu into a folded section: the focus lands on that
    section's header, not on the page."""
    views.change("create", "Work")
    work = views.sid("Work")
    page = await views.open(view)
    await _sections_mode(page)
    await _head(page, work).locator(".shead").click()
    box = await _filter(page)
    await box.focus()
    for _ in range(3):  # Work, Other, A
        await box.press("ArrowDown")
    await _selected_is(page, A)
    await _menu(_row(page, A))
    await page.locator(".rowmenu [data-k=file]").press("Enter")
    await page.locator(".rowmenu [data-k=sec]").press("Enter")
    await wait(lambda: views.chats("Work") == [A], "filed")
    await _list_is(page, ["#Work ▸", "#Other", NAME[B], NAME[C]])
    await _focus_is(page, "#" + work)


@pytest.mark.parametrize("mods", [[], ["Control"]])
async def test_a_pointer_open_leaves_no_pinned_row_actions(
    views: Views, monkeypatch: pytest.MonkeyPatch, mods: list[str]
) -> None:
    """A row's title clicked (plain: opened by the server; Ctrl: a new tab) keeps no focus: after a
    broadcast its ✎ 👁 ⋯ hide again once the mouse leaves."""
    opened: list[str] = []

    async def spy(thread_id: str, target: str = "app", ws: Any = None, msg_id: str = "") -> bool:
        opened.append(thread_id)
        return True

    monkeypatch.setattr(views.board, "open_row", spy)
    views.board.opener = ["true"]
    # The Teams link becomes a page of our own server: a Ctrl click's tab stays local.
    monkeypatch.setattr(W, "deep_link", lambda thread_id, msg_id="", scheme="https": f"/teams/{msg_id}")
    page = await views.open("widget")
    if mods:
        async with page.context.expect_page() as tab:
            await _row(page, B).locator(".name").click(modifiers=mods)  # type: ignore[arg-type]
        await (await tab.value).close()
    else:
        await _row(page, B).locator(".name").click()
        await wait(lambda: opened == [B], "opened")
    await page.mouse.move(0, 0)
    n = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    views.board.broadcast()
    await until(page, f"window.__frames.filter(f => 'rows' in f).length > {n}")
    assert not await _row(page, B).locator("[data-k=more]").is_visible()


@VIEWS
async def test_two_pages_of_a_view_keep_each_others_folds(views: Views, view: str) -> None:
    """026-AC6: two pages of one view fold different sections: both folds are kept; unfolding one in a page
    leaves the other page's fold stored."""
    views.change("create", "Work", B)
    views.change("create", "Home", C)
    work, home = views.sid("Work"), views.sid("Home")
    one = await views.open(view)
    two = await views.open(view, one.context)
    for page in (one, two):
        await _sections_mode(page)
    await _head(one, work).locator(".shead").click()
    await _head(two, home).locator(".shead").click()
    await views.reload(one)
    await _list_is(one, ["#Work ▸", "#Home ▸", "#Other", NAME[A]])
    await _head(two, home).locator(".shead").click()
    await views.reload(one)
    await _list_is(one, ["#Work ▸", "#Home", NAME[C], "#Other", NAME[A]])


# --- AC8, AC13: drag and drop (phase 2) ---

SENT = "window.__out.filter(f => Object.keys(f).some(k => k.startsWith('section'))).length"


async def _drag(page: Page, item: Any, onto: Any, at: str = "middle") -> None:
    """Drags `item` with the mouse onto `onto`, near its top, middle or bottom edge."""
    box = await onto.bounding_box()
    assert box
    y = {"top": 4, "middle": box["height"] / 2, "bottom": box["height"] - 4}[at]
    await item.drag_to(onto, target_position={"x": box["width"] / 2, "y": y})


async def _drops_nothing(page: Page, item: Any, onto: Any, at: str = "middle") -> None:
    """The drop sends no sections verb; the list is drawn again once the drag ended."""
    n = await page.evaluate(SENT)
    await _drag(page, item, onto, at)
    await page.evaluate("new Promise(r => setTimeout(r, 100))")
    assert await page.evaluate(SENT) == n
    assert await page.locator("#rows > li:is(.dropin, .dropup, .dropdown)").count() == 0


@VIEWS
async def test_a_chat_dropped_on_a_header_files_it_there_folded_too(views: Views, view: str) -> None:
    """026-AC8: a chat dropped on a section header goes into it, on top of a Manual one, even folded."""
    views.change("create", "Work", B)
    views.change("create", "Home", C)
    work, home = views.sid("Work"), views.sid("Home")
    views.change("set_order", home, "manual")
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[B], "#Home", NAME[C], "#Other", NAME[A]])
    await _drag(page, _row(page, A), _head(page, work))
    await wait(lambda: views.chats("Work") == [A, B], "filed in Work")
    await _list_is(page, ["#Work", NAME[A], NAME[B], "#Home", NAME[C], "#Other"])
    await _head(page, home).locator(".shead").click()
    await _list_is(page, ["#Work", NAME[A], NAME[B], "#Home ▸", "#Other"])
    await _drag(page, _row(page, B), _head(page, home))
    await wait(lambda: views.chats("Home") == [B, C], "on top of Home")
    await _list_is(page, ["#Work", NAME[A], "#Home ▸", "#Other"])


@VIEWS
async def test_a_chat_dropped_on_a_chat_of_another_section_files_it_there(views: Views, view: str) -> None:
    """026-AC8, 026-AC13: dropped on a chat of a Recent section it goes in by recency; of a Manual one, at
    the drop position, above or below that chat."""
    views.change("create", "Work", B)
    views.change("create", "Home", C)
    home = views.sid("Home")
    views.change("set_order", home, "manual")
    page = await views.open(view)
    await _sections_mode(page)
    await _drag(page, _row(page, A), _row(page, B), "bottom")
    await wait(lambda: views.chats("Work") == [A, B], "filed in Work")
    await _list_is(page, ["#Work", NAME[A], NAME[B], "#Home", NAME[C], "#Other"])
    await _drag(page, _row(page, A), _row(page, C), "bottom")
    await wait(lambda: views.chats("Home") == [C, A], "below C")
    await _list_is(page, ["#Work", NAME[B], "#Home", NAME[C], NAME[A], "#Other"])
    await _drag(page, _row(page, B), _row(page, C), "top")
    await wait(lambda: views.chats("Home") == [B, C, A], "above C")
    await _list_is(page, ["#Work", "#Home", NAME[B], NAME[C], NAME[A], "#Other"])


@VIEWS
async def test_a_chat_dragged_in_its_manual_section_lands_at_the_drop(views: Views, view: str) -> None:
    """026-AC13: in a Manual section a chat lands where dropped, above or below the chat under the mouse;
    a Recent section ignores the position: recency orders it."""
    views.change("create", "Work", C)
    views.change("file", B, views.sid("Work"))
    views.change("file", A, views.sid("Work"))
    work = views.sid("Work")
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[A], NAME[B], NAME[C], "#Other"])
    await _drops_nothing(page, _row(page, C), _row(page, A), "top")
    await _list_is(page, ["#Work", NAME[A], NAME[B], NAME[C], "#Other"])
    views.change("set_order", work, "manual")
    await _drag(page, _row(page, C), _row(page, A), "top")
    await wait(lambda: views.chats("Work") == [C, A, B], "C on top")
    await _list_is(page, ["#Work", NAME[C], NAME[A], NAME[B], "#Other"])
    await _drag(page, _row(page, C), _row(page, A), "bottom")
    await wait(lambda: views.chats("Work") == [A, C, B], "C below A")
    await _drag(page, _row(page, A), _row(page, B), "bottom")
    await wait(lambda: views.chats("Work") == [C, B, A], "A last")
    await _list_is(page, ["#Work", NAME[C], NAME[B], NAME[A], "#Other"])
    await _drops_nothing(page, _row(page, A), _row(page, A), "top")
    await _drops_nothing(page, _row(page, A), _row(page, B), "bottom")  # its own slot
    await _drops_nothing(page, _row(page, B), _row(page, C), "bottom")
    assert views.chats("Work") == [C, B, A]


@VIEWS
async def test_a_chat_dropped_on_other_leaves_its_section(views: Views, view: str) -> None:
    """026-AC8: dropped on Other's header (shown empty too) or on one of its chats, a chat leaves its
    section; a chat already in Other dropped there changes nothing."""
    views.change("create", "Work", B)
    views.change("file", A, views.sid("Work"))
    views.change("file", C, views.sid("Work"))
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[A], NAME[B], NAME[C], "#Other"])
    await _drag(page, _row(page, B), _head(page, ""))
    await wait(lambda: views.chats("Work") == [C, A], "B out")
    await _list_is(page, ["#Work", NAME[A], NAME[C], "#Other", NAME[B]])
    await _drag(page, _row(page, C), _row(page, B))
    await wait(lambda: views.chats("Work") == [A], "C out")
    await _list_is(page, ["#Work", NAME[A], "#Other", NAME[B], NAME[C]])
    await _drops_nothing(page, _row(page, C), _head(page, ""))


@VIEWS
async def test_a_header_dragged_reorders_the_sections_and_other_stays_last(views: Views, view: str) -> None:
    """026-AC8: a header dropped on another lands before it, dropped on Other lands last; Other does not
    drag and nothing lands after it."""
    for name in ("Home", "Work", "Jobs"):
        views.change("create", name)
    home, jobs = views.sid("Home"), views.sid("Jobs")
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Home", "#Work", "#Jobs", "#Other", NAME[A], NAME[B], NAME[C]])
    await _drag(page, _head(page, jobs), _head(page, home))
    await wait(lambda: views.names() == ["Jobs", "Home", "Work"], "Jobs first")
    await _list_is(page, ["#Jobs", "#Home", "#Work", "#Other", NAME[A], NAME[B], NAME[C]])
    await _drag(page, _head(page, jobs), _head(page, ""))
    await wait(lambda: views.names() == ["Home", "Work", "Jobs"], "Jobs last")
    await _list_is(page, ["#Home", "#Work", "#Jobs", "#Other", NAME[A], NAME[B], NAME[C]])
    assert await _head(page, "").get_attribute("draggable") == "false"
    await _drops_nothing(page, _head(page, ""), _head(page, home))
    await _drops_nothing(page, _head(page, home), _row(page, A))
    await _drops_nothing(page, _head(page, home), _head(page, views.sid("Work")))  # where it already is
    await _drops_nothing(page, _head(page, jobs), _head(page, ""))
    assert views.names() == ["Home", "Work", "Jobs"]


@VIEWS
async def test_a_click_without_a_drag_opens_and_folds_as_before(
    views: Views, view: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """026-AC8: rows and headers drag only in Sections mode; there a plain click still opens the chat and
    folds a header, and sends no sections verb."""
    opened: list[str] = []

    async def spy(thread_id: str, target: str = "app", ws: Any = None, msg_id: str = "") -> bool:
        opened.append(thread_id)
        return True

    monkeypatch.setattr(views.board, "open_row", spy)
    views.board.opener = ["true"]
    views.change("create", "Work", B)
    work = views.sid("Work")
    page = await views.open(view)
    assert await _row(page, B).get_attribute("draggable") is None
    await _sections_mode(page)
    assert await _row(page, B).get_attribute("draggable") == "true"
    assert await _head(page, work).get_attribute("draggable") == "true"
    await _row(page, B).locator(".name").click()
    if view == "widget":
        await wait(lambda: opened == [B], "opened")
    else:
        await until(page, f"location.pathname === {json.dumps('/chat/' + quote(B, safe=''))}")
    await _head(page, work).locator(".shead").click()
    await _list_is(page, ["#Work ▸", "#Other", NAME[A], NAME[C]])
    assert await page.evaluate(SENT) == 0


@VIEWS
async def test_a_broadcast_during_a_drag_waits_for_its_end(views: Views, view: str) -> None:
    """026-AC8: the list is not rebuilt under a drag (its source would go); the change shows once it ends."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    drag = f"""t => {{ const li = document.querySelector('#rows > li[data-id="{A}"]'); window.__src = li;
      li.dispatchEvent(new DragEvent(t, {{ bubbles: true, dataTransfer: new DataTransfer() }})); }}"""
    await page.evaluate(drag, "dragstart")
    n = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    views.change("rename", views.sid("Work"), "Jobs")
    await until(page, f"window.__frames.filter(f => 'rows' in f).length > {n}")
    assert await page.evaluate("window.__src.isConnected")
    await _list_is(page, ["#Work", NAME[B], "#Other", NAME[A], NAME[C]])
    await page.evaluate(drag, "dragend")
    await _list_is(page, ["#Jobs", NAME[B], "#Other", NAME[A], NAME[C]])


# A drag step dispatched on an item (`#<sid>` for a header) at `y` (0 top … 1 bottom); true when the page
# took it (a drop allowed). A drop or dragend whose item is gone goes to the list; any other step needs its
# item. One DataTransfer per drag, as in a real one.
DND = """([type, key, y]) => {
  const li = document.querySelector(key[0] === '#'
    ? `#rows > li[data-sid="${key.slice(1)}"]` : `#rows > li[data-id="${key}"]`)
    || (['drop', 'dragend'].includes(type) ? document.querySelector('#rows') : null);
  if (!li) throw new Error(`no item ${key} for ${type}`);
  const r = li.getBoundingClientRect();
  if (type === 'dragstart') window.__dt = new DataTransfer();
  const e = new DragEvent(type, { bubbles: true, cancelable: true, clientX: r.left + r.width / 2,
    clientY: r.top + (y ?? 0.5) * r.height, dataTransfer: window.__dt });
  li.dispatchEvent(e);
  return e.defaultPrevented;
}"""
MARKS = """[...document.querySelectorAll('#rows > li:is(.dropin, .dropup, .dropdown)')]
  .map(li => (li.dataset.id ?? '#' + li.dataset.sid) + ' '
    + [...li.classList].filter(c => c.startsWith('drop')).join(' '))"""


async def _dnd(page: Page, kind: str, key: str, y: float | None = None) -> bool:
    return bool(await page.evaluate(DND, [kind, key, y]))


@VIEWS
@pytest.mark.parametrize("case", ["header-on-gone", "chat-on-gone", "gone-header"])
async def test_a_drop_on_a_section_deleted_mid_drag_sends_nothing(views: Views, view: str, case: str) -> None:
    """026-AC8: renders wait for a drag, so a section deleted by another view stays on screen: a header or
    a chat dropped on it, or the dragged header deleted itself, sends no verb (not a move to the top, not
    a filing into Other)."""
    for name in ("Home", "Spare", "Jobs"):
        views.change("create", name)
    views.change("create", "Work", A)
    home, spare, jobs = views.sid("Home"), views.sid("Spare"), views.sid("Jobs")
    item, gone, onto = {
        "header-on-gone": ("#" + jobs, home, home),
        "chat-on-gone": (A, spare, spare),
        "gone-header": ("#" + jobs, jobs, home),
    }[case]
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Home", "#Spare", "#Jobs", "#Work", NAME[A], "#Other", NAME[B], NAME[C]])
    await _dnd(page, "dragstart", item)
    n = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    views.change("delete", gone)
    await until(page, f"window.__frames.filter(f => 'rows' in f).length > {n}")
    assert not await _dnd(page, "dragover", "#" + onto)
    assert await page.evaluate(MARKS) == []
    await _dnd(page, "drop", "#" + onto)
    await _dnd(page, "dragend", item)
    await page.evaluate("new Promise(r => setTimeout(r, 100))")
    assert await page.evaluate(SENT) == 0
    left = {home: "Home", spare: "Spare", jobs: "Jobs"}[gone]
    assert views.names() == [x for x in ("Home", "Spare", "Jobs", "Work") if x != left]
    assert views.chats("Work") == [A]


@VIEWS
async def test_drop_markers_show_where_a_chat_would_land(views: Views, view: str) -> None:
    """026-AC8, 026-AC13: while a chat is dragged, a header it would go into is outlined; a Manual
    section's chat shows a line above or below, by the half under the mouse; an invalid target none."""
    views.change("create", "Work", B)
    views.change("create", "Home", C)
    views.change("file", A, views.sid("Home"))
    work, home = views.sid("Work"), views.sid("Home")
    views.change("set_order", home, "manual")
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[B], "#Home", NAME[A], NAME[C], "#Other"])
    await _dnd(page, "dragstart", B)
    for key, y, marks in (
        ("#" + home, None, ["#" + home + " dropin"]),
        (C, 0.2, [C + " dropup"]),
        (C, 0.8, [C + " dropdown"]),
        (A, 0.8, [A + " dropdown"]),
        ("#", None, ["# dropin"]),
        (B, None, []),
        ("#" + work, None, []),
    ):
        assert await _dnd(page, "dragover", key, y) == bool(marks), key
        assert await page.evaluate(MARKS) == marks, (key, y)
    # A selected header shows the drop's dashed outline, not its own solid one.
    head = f"document.querySelector('#rows > li[data-sid=\"{home}\"]')"
    await page.evaluate(f"{head}.classList.add('sel')")
    assert await _dnd(page, "dragover", "#" + home)
    assert await page.evaluate(f"getComputedStyle({head}).outlineStyle") == "dashed"
    await _dnd(page, "dragend", B)
    assert await page.evaluate(MARKS) == []


@VIEWS
async def test_a_chat_dropped_on_its_manual_header_goes_on_top(views: Views, view: str) -> None:
    """026-AC13: dropped on its own Manual section's header a chat goes first; already first, no verb."""
    views.change("create", "Work", C)
    views.change("file", B, views.sid("Work"))
    work = views.sid("Work")
    views.change("set_order", work, "manual")
    page = await views.open(view)
    await _sections_mode(page)
    await _list_is(page, ["#Work", NAME[B], NAME[C], "#Other", NAME[A]])
    await _drag(page, _row(page, C), _head(page, work))
    await wait(lambda: views.chats("Work") == [C, B], "C first")
    await _list_is(page, ["#Work", NAME[C], NAME[B], "#Other", NAME[A]])
    await _drops_nothing(page, _row(page, C), _head(page, work))


@VIEWS
async def test_a_row_drag_carries_only_its_own_type(views: Views, view: str) -> None:
    """026-AC8: a row's drag carries no link or text: its title dropped on the composer types nothing."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    types = """id => {
      const dt = new DataTransfer(), li = document.querySelector(`#rows > li[data-id="${id}"]`);
      dt.setData('text/uri-list', 'https://example.invalid/');
      dt.setData('text/plain', 'https://example.invalid/');
      const start = new DragEvent('dragstart', { bubbles: true, dataTransfer: dt });
      li.querySelector('.name').dispatchEvent(start);
      const t = [...dt.types];
      li.dispatchEvent(new DragEvent('dragend', { bubbles: true }));
      return t; }"""
    assert await page.evaluate(types, A) == ["application/x-miniteams-item"]
    if view == "widget":
        await _row(page, A).hover()
        await _row(page, A).locator("[data-k=reply]").click()
    box = page.locator("#reply")
    await box.wait_for()
    await _row(page, B).locator(".name").drag_to(box)
    await page.evaluate("new Promise(r => setTimeout(r, 100))")
    assert await box.input_value() == ""


@VIEWS
@pytest.mark.parametrize("end", ["move", "drop"])
async def test_a_drag_with_no_dragend_is_over_at_a_drop_or_a_buttonless_move(
    views: Views, view: str, end: str
) -> None:
    """026-AC8: a drag whose dragend never reached the list (its source gone) stops holding the list at a
    drop on it or at the first pointer move with no button pressed: a change made meanwhile shows."""
    views.change("create", "Work", B)
    page = await views.open(view)
    await _sections_mode(page)
    await _dnd(page, "dragstart", A)
    n = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    views.change("rename", views.sid("Work"), "Jobs")
    await until(page, f"window.__frames.filter(f => 'rows' in f).length > {n}")
    await _list_is(page, ["#Work", NAME[B], "#Other", NAME[A], NAME[C]])
    if end == "move":
        await page.mouse.move(5, 5)
        await page.mouse.move(10, 10)
    else:
        await _dnd(page, "drop", "#")
    await _list_is(page, ["#Jobs", NAME[B], "#Other", NAME[A], NAME[C]])


@VIEWS
async def test_a_menu_closed_by_the_focus_gives_its_item_its_drag_back(views: Views, view: str) -> None:
    """026-AC8: an item drags but while its ⋯ menu is open; the menu closed by the focus leaving it, the row
    or header drags again at once (no broadcast in between); in Recent mode a row stays undraggable."""
    views.change("create", "Work", B)
    work = views.sid("Work")
    page = await views.open(view)
    box = await _filter(page)

    async def close_by_focus(item: Any, before: str | None, after: str | None) -> None:
        await _menu(item)
        assert await item.get_attribute("draggable") == before
        n = await page.evaluate("window.__frames.length")
        await box.focus()
        await until(page, "!document.querySelector('.rowmenu')")
        assert await page.evaluate("window.__frames.length") == n
        assert await item.get_attribute("draggable") == after

    await close_by_focus(_row(page, A), None, None)
    await _sections_mode(page)
    await close_by_focus(_row(page, A), "false", "true")
    await close_by_focus(_head(page, work), "false", "true")
