"""The chat's members on hover (spec 022), on the chat page against a fake chat socket, and once
against the real board. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_members.py`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import browser_helpers
import pytest
import test_browser_split_ux as ux
import test_web_chat
from browser_helpers import Page, Pages, past, until  # skips without playwright
from test_browser_split_ux import _board, _id
from test_web_chat import ANN, ME, T, _at, _served

from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, data_dir, quiet_directory = test_web_chat.board, test_web_chat.data_dir, test_web_chat.quiet_directory
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)

POP = "#mpop"
ROWS = "[...document.querySelectorAll('#mpop li > .truncate')].map((n) => n.textContent)"
OPEN = "!document.getElementById('mpop').hidden"


def _person(i: int, name: str, me: bool = False) -> dict[str, Any]:
    return {"name": name, "mri": f"8:orgid:{i:032x}", "me": me}


# As the server sends them: me first, then by name.
TRIO = [_person(1, "Me Self", True), _person(2, "Ann Lee"), _person(3, "Bob Stone")]


CUR = _id(2)  # the chat ux.Split opens


@dataclass
class Fake(ux.Fake):
    """The split fake whose follow answers `people` (None: no roster), `total` of them and `apps`, in
    a label counting both; `later`: the follow answer leaves the roster to a `members` frame."""

    people: list[dict[str, Any]] | None = None
    total: int = 0
    apps: int = 0
    later: bool = False

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "follow" not in msg:
            super().on_verb(msg)
            return
        n = self.total + self.apps
        frame = {"follow": msg["follow"], "label": f"Team · {n}p", "page": "1", "opener": False}
        roster = {"people": self.people, "members_total": self.total, "apps": self.apps}
        self.send(frame if self.later else {**frame, **roster})

    def push(self, people: list[dict[str, Any]] | None, total: int, thread: str = CUR, apps: int = 0) -> None:
        self.send({"members": thread, "people": people, "members_total": total, "apps": apps})


@pytest.fixture
async def chat(pages: Pages) -> AsyncIterator[ux.Split]:
    found = ux.Split(pages, Fake(rows=_board(3)))
    yield found
    await pages.close()
    assert pages.external == []


def _fake(chat: ux.Split) -> Fake:
    assert isinstance(chat.fake, Fake)
    return chat.fake


async def _open(
    chat: ux.Split, people: list[dict[str, Any]] | None, total: int, apps: int = 0, later: bool = False
) -> Page:
    fake = _fake(chat)
    fake.people, fake.total, fake.apps, fake.later = people, total, apps, later
    page = await chat.open(query="", viewport={"width": 1100, "height": 700})
    await until(page, f"document.getElementById('members').textContent === '{total + apps} members'")
    return page


async def _closed_for_good(page: Page) -> None:
    await until(page, "!(" + OPEN + ")")
    await past(page, 400)  # no pending timer reopens it
    assert not await page.evaluate(OPEN)


async def _box(page: Page, sel: str) -> list[float]:
    return list(
        await page.evaluate(
            f"(r => [r.x, r.y, r.width, r.height])(document.querySelector({sel!r}).getBoundingClientRect())"
        )
    )


@BOTH
async def test_hover_lists_the_members_and_leaving_or_escape_closes(chat: ux.Split) -> None:
    """022-AC2: hovering "N members" opens, after a short delay, the list in the server's order with
    "You" first, initials avatars and the presence dot when known; the popover moves neither header
    nor list, keeps open while the pointer is in it, closes on leaving and on Escape."""
    page = await _open(chat, TRIO, 3)
    chat.fake.send(
        {"presence": {TRIO[1]["mri"]: {"availability": "Busy", "activity": "InACall", "confirmed": True}}}
    )
    await chat.fake.handled()
    boxes = [await _box(page, sel) for sel in ("header", "#list", "#label", "#find")]
    await page.hover("#members")
    await page.mouse.move(500, 500)  # a pointer passing over opens nothing
    await past(page, 400)
    assert not await page.evaluate(OPEN)
    await page.hover("#members")
    assert not await page.evaluate(OPEN)  # not at once: after a short delay
    await until(page, OPEN)
    assert await page.evaluate(ROWS) == ["You", "Ann Lee", "Bob Stone"]
    assert await page.locator("#mpop .mav").all_text_contents() == ["MS", "AL", "BS"]
    dots = (
        "[...document.querySelectorAll('#mpop .mav')].map((a) => a.querySelector('.pres')?.className ?? '')"
    )
    assert await page.evaluate(dots) == ["", "pres busy", ""]
    assert await page.get_attribute("#members", "aria-expanded") == "true"
    assert [await _box(page, sel) for sel in ("header", "#list", "#label", "#find")] == boxes
    await page.hover(f"{POP} li:last-child")  # through the gap: the popover itself holds it open
    await past(page, 400)
    assert await page.evaluate(OPEN)
    await page.mouse.move(500, 500)  # over the messages
    await until(page, "!(" + OPEN + ")")
    assert await page.get_attribute("#members", "aria-expanded") == "false"
    await page.hover("#members")
    await until(page, OPEN)
    await page.keyboard.press("Escape")
    assert not await page.evaluate(OPEN)


@BOTH
async def test_tab_reaches_members_and_its_focus_shows_the_list(chat: ux.Split) -> None:
    """022-AC3: "N members" is a Tab stop, a disclosure button; focusing it shows the list, Tab moves
    into the list (it stays open), Tab out or Escape closes it."""
    page = await _open(chat, TRIO, 3)
    assert await page.get_attribute("#members", "role") == "button"
    assert await page.get_attribute("#members", "aria-controls") == "mpop"
    await page.focus("#back")
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.id") == "members"
    await until(page, OPEN)
    assert await page.evaluate(ROWS) == ["You", "Ann Lee", "Bob Stone"]
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.id") == "mpop"
    await past(page, 300)
    assert await page.evaluate(OPEN)
    await page.keyboard.press("Tab")
    await until(page, "!(" + OPEN + ")")
    await page.focus("#members")
    await until(page, OPEN)
    await page.keyboard.press("Escape")
    assert not await page.evaluate(OPEN)
    await page.keyboard.press("Enter")  # the button opens it again
    assert await page.evaluate(OPEN)


@BOTH
async def test_more_than_50_members_show_50_rows_then_how_many_more(chat: ux.Split) -> None:
    """022-AC4: 50 rows then "… and N more", N counted from the whole roster; the page caps a longer
    list itself."""
    crowd = [_person(i, f"Person {i:03d}") for i in range(60)]
    page = await _open(chat, crowd[:50], 120)
    await page.focus("#members")
    await until(page, OPEN)
    assert await page.evaluate(ROWS) == [f"Person {i:03d}" for i in range(50)]
    assert await page.locator(f"{POP} > .none").text_content() == "… and 70 more"
    _fake(chat).push(crowd, 60)
    await until(page, f"document.querySelector('{POP} > .none')?.textContent === '… and 10 more'")
    assert await page.locator(f"{POP} li").count() == 50


@BOTH
async def test_no_roster_says_unavailable_and_names_stay_text(chat: ux.Split) -> None:
    """022-AC5: no roster: "members unavailable"; a name holding markup shows as that text."""
    page = await _open(chat, None, 4)
    await page.focus("#members")
    await until(page, OPEN)
    assert await page.locator(POP).text_content() == "members unavailable"
    evil = [_person(1, '<img src=x onerror="window.pwned=1"><b>Bold</b>'), _person(2, "Ann Lee")]
    _fake(chat).push(evil, 2)
    await until(page, f"{ROWS}.length === 2")
    assert await page.evaluate(ROWS) == [evil[0]["name"], "Ann Lee"]
    assert await page.locator(f"{POP} img, {POP} b").count() == 0
    assert await page.evaluate("window.pwned") is None


async def test_the_real_board_pushes_the_roster_the_popover_lists(
    board: W.Board, settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """022-AC1, 022-AC2: a chat whose roster is not cached: the follow answers, the roster fetched
    off the socket loop comes in its own frame, and the popover lists its people, "You" first, the
    bot as "+ 1 app"."""
    board.rows[T] = {"id": T, "label": "Team · 3p", "last_activity": _at(5), "last_id": "5", "typing": []}
    board.rows[T].update(seen_at=None, muted=False, mention=None)
    roster = [
        {"mri": ANN, "name": "Ann Lee"},
        {"mri": "28:00000000-0000-0000-0000-0000000000ee", "name": "Helper Bot"},
        {"mri": ME, "name": "Me Self"},
    ]

    async def fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        return {"topic": None, "members": [dict(m) for m in roster]}

    monkeypatch.setattr(Directory, "_fetch_thread", fetch)
    async with _served(board, settings) as host:
        pages.serve(host, board)
        page = await pages.open(f"/chat/{T}")
        await until(page, "document.getElementById('members').textContent === '3 members'")
        await page.hover("#members")
        await until(page, OPEN)
        await until(page, f"{ROWS}.length === 2")
        assert await page.evaluate(ROWS) == ["You", "Ann Lee"]
        assert await page.locator(f"{POP} > .none").all_text_contents() == ["+ 1 app"]
        await pages.close()
    assert pages.external == []


@BOTH
async def test_a_roster_pushed_later_fills_the_open_popover_and_another_chats_is_ignored(
    chat: ux.Split,
) -> None:
    """022-AC1, 022-AC2: the follow answer without the roster: "loading members…" until its `members`
    frame; a frame for a chat left since changes nothing."""
    page = await _open(chat, None, 3, later=True)
    await page.focus("#members")
    await until(page, OPEN)
    assert await page.locator(POP).text_content() == "loading members…"
    _fake(chat).push([_person(9, "Old Chat")], 1, thread=_id(1))
    await _fake(chat).handled()
    assert await page.locator(POP).text_content() == "loading members…"
    _fake(chat).push(TRIO, 3)
    await until(page, f"{ROWS}.length === 3")
    assert await page.evaluate(ROWS) == ["You", "Ann Lee", "Bob Stone"]


@BOTH
async def test_apps_add_up_to_the_label_count(chat: ux.Split) -> None:
    """022-AC4: the label counts bots and apps too: "+ N apps" closes the list so the lines add up."""
    page = await _open(chat, TRIO, 3, apps=2)
    assert await page.text_content("#members") == "5 members"
    await page.focus("#members")
    await until(page, OPEN)
    assert await page.locator(f"{POP} > .none").all_text_contents() == ["+ 2 apps"]


@BOTH
async def test_no_count_hides_members_from_everyone(chat: ux.Split) -> None:
    """022-AC3: a label without a count leaves no empty, unnamed button in the page."""
    page = await _open(chat, TRIO, 3)
    assert await page.locator("#members").is_visible()
    _fake(chat).send({"follow": CUR, "label": "Ann Lee", "page": "1", "opener": False, "people": None})
    await until(page, "document.getElementById('members').hidden")
    assert await page.locator("#members").is_hidden()


@BOTH
async def test_escape_inside_the_list_gives_the_focus_back_to_members(chat: ux.Split) -> None:
    """022-AC2/AC3: Escape with the focus in the list closes it and puts the focus on "N members",
    not on the page body; the popover stays closed."""
    page = await _open(chat, TRIO, 3)
    await page.focus("#members")
    await until(page, OPEN)
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.id") == "mpop"
    await page.keyboard.press("Escape")
    assert await page.evaluate("document.activeElement.id") == "members"
    await _closed_for_good(page)
    await page.keyboard.press("Enter")
    assert await page.evaluate(OPEN)
    await page.hover("#members")
    await page.keyboard.press("Escape")
    await page.mouse.move(500, 500)  # the focus still on "N members": leaving must not reopen it
    await _closed_for_good(page)


@BOTH
async def test_a_click_toggles_and_leaving_closes(chat: ux.Split) -> None:
    """022-AC2: a click opens the list without taking the focus, so leaving closes it; a second click
    closes it too."""
    page = await _open(chat, TRIO, 3)
    await page.click("#members")
    assert await page.evaluate(OPEN)
    assert await page.evaluate("document.activeElement.id") != "members"
    await page.mouse.move(500, 500)
    await _closed_for_good(page)
    await page.click("#members")
    assert await page.evaluate(OPEN)
    await page.click("#members")
    await _closed_for_good(page)
