"""A sender's name leads to the private chat with them (spec 023 phase 1), on the chat page against a fake
chat socket. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_dm.py`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import browser_helpers
import pytest
import test_browser_split_ux as ux
from browser_helpers import ORIGIN, Page, Pages, until, wait  # skips without playwright
from test_browser_split_ux import _board, _id

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)

ME = "8:orgid:00000000-0000-0000-0000-00000000000a"
ANN = "8:orgid:00000000-0000-0000-0000-00000000000b"  # a 1:1 with me on the board
BOB = "8:orgid:00000000-0000-0000-0000-00000000000c"  # a 1:1 only in the archive
CY = "8:orgid:00000000-0000-0000-0000-00000000000d"  # no 1:1 with me
GHOST = "28:bot-id"  # an app: no 1:1 to reach
ODD = "8:orgid:not-a-guid"  # a person the server cannot place
DM_ANN = "19:00000000-0000-0000-0000-00000000000b_00000000-0000-0000-0000-00000000000a@unq.gbl.spaces"
DM_BOB = "19:00000000-0000-0000-0000-00000000000a_00000000-0000-0000-0000-00000000000c@unq.gbl.spaces"
GROUP = _id(2)  # the chat the page opens on
ERR = "document.getElementById('err').textContent"
SENDERS = [
    (ANN, "Ann Lee"),
    (ME, "Me"),
    (BOB, "Bob Ray"),
    (CY, "Cy Tan"),
    (GHOST, "Some Bot"),
    ("", "Nobody"),
    (ODD, "Odd One"),
    ("8:ORGID:00000000-0000-0000-0000-00000000000E", "Eve Upper"),  # the server folds the case
]


def _msg(i: int, mri: str, name: str) -> dict[str, Any]:
    # An hour apart: each message keeps its head (no grouping under the one before).
    at = f"2026-10-09T{10 + i}:00:00.000Z"
    return {
        "id": str(1000 + i),
        "at": at,
        "from": mri,
        "name": name,
        "me": mri == ME,
        "type": "text",
        "html": f"<p>m{i}</p>",
        "reactions": [],
    }


def _who(i: int) -> str:
    return f'[data-id="{1000 + i}"] .head b'


@dataclass
class Fake(ux.Fake):
    """The split fake whose chats hold one message per SENDERS entry; `dm` answers ANN's and BOB's 1:1,
    an error for the others, `failed` for every one when `fails` (queued while `hold`)."""

    fails: bool = False
    browser: bool = False
    chats: dict[str, str] = field(default_factory=lambda: {ANN: DM_ANN, BOB: DM_BOB})

    def broadcast(self) -> None:
        self.send({"rows": self.rows, "browser": self.browser, "me": "Me"})

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "dm" in msg:
            mri = msg["dm"]
            frame: dict[str, Any] = {"dm": mri}
            if self.fails:
                frame["error"] = "failed"
            elif mri in self.chats:
                frame["chat"] = self.chats[mri]
            else:
                frame["error"] = "no private chat with them yet" if mri == CY else "unknown person"
            self.answer(frame)
        elif "history" in msg and not msg.get("before") and "after" not in msg:
            messages = [_msg(i, mri, name) for i, (mri, name) in enumerate(SENDERS)]
            self.send({"history": msg["history"], "messages": messages, "source": "archive"})
        elif "history" not in msg:
            super().on_verb(msg)

    def dms(self) -> list[str]:
        return [v["dm"] for v in self.of("dm")]


def _rows() -> list[dict[str, Any]]:
    rows = _board(4)
    return [*rows, {**rows[0], "id": DM_ANN, "label": "Ann Lee"}]


@pytest.fixture
async def split(pages: Pages) -> AsyncIterator[ux.Split]:
    found = ux.Split(pages, Fake(rows=_rows()))
    yield found
    await pages.close()
    assert pages.external == []


def _url(thread: str, query: str = "?chatnav=split") -> str:
    return f"{ORIGIN}/chat/{quote(thread, safe='')}{query}"


async def _open(split: ux.Split, at: int = 2, query: str = "?chatnav=split") -> tuple[Page, Fake]:
    page = await split.open(at, query, viewport={"width": 1300, "height": 700})
    await page.wait_for_selector(f'[data-id="{1000 + len(SENDERS) - 1}"]')
    assert isinstance(split.fake, Fake)
    return page, split.fake


async def _shows(page: Page, fake: Fake, thread: str, query: str = "?chatnav=split") -> None:
    await page.wait_for_url(_url(thread, query))
    await wait(lambda: fake.follows()[-1:] == [thread], "follow")
    await page.wait_for_selector(f'[data-id="{1000 + len(SENDERS) - 1}"]')


@BOTH
async def test_only_someone_elses_name_in_a_group_chat_is_a_target(split: ux.Split) -> None:
    """023-AC1: in a chat with several people, another sender's name is a link-like target (pointer, role
    link, `p` on its message: 018-AC4/AC5 take it out of the Tab order); mine is not."""
    page, fake = await _open(split)
    ann = page.locator(_who(0))
    assert await ann.get_attribute("role") == "link"
    assert await ann.get_attribute("tabindex") == "-1"
    assert await ann.evaluate("b => getComputedStyle(b).cursor") == "pointer"
    mine = page.locator(_who(1))
    assert await mine.get_attribute("role") is None
    assert await mine.get_attribute("tabindex") is None
    assert await mine.evaluate("b => getComputedStyle(b).cursor") != "pointer"
    for i in (4, 5):  # an app, no sender id: nobody to reach
        assert await page.locator(_who(i)).get_attribute("role") is None
    assert await page.locator(_who(7)).get_attribute("role") == "link"
    for i in (1, 4, 0):  # mine, an app's: no lookup; Ann's
        await page.locator(f'[data-id="{1000 + i}"]').focus()
        await page.keyboard.press("p")
    await wait(lambda: fake.dms() == [ANN], "dm")


@BOTH
async def test_a_one_to_one_chat_shows_no_target(split: ux.Split) -> None:
    """023-AC1: in a 1:1, no sender name is a target."""
    page, fake = await _open(split)
    await page.locator(_who(0)).click()
    await _shows(page, fake, DM_ANN)
    assert await page.locator(".head b").count() == len(SENDERS)
    assert await page.locator(".head b[role=link], .head b[tabindex], .head b.who").count() == 0


@BOTH
@pytest.mark.parametrize("how", ["click", "enter"])
async def test_a_name_switches_to_the_one_to_one_and_back_returns(split: ux.Split, how: str) -> None:
    """023-AC2: a click on the name (or Enter on it) switches the page to the 1:1, on the board or only in
    the archive (either id order); Back returns to the group chat."""
    page, fake = await _open(split)
    await page.evaluate("window.__kept = 1")
    for i, dm in ((0, DM_ANN), (2, DM_BOB)):
        if how == "click":
            await page.locator(_who(i)).click()
        else:
            await page.locator(_who(i)).focus()
            await page.keyboard.press("Enter")
        await _shows(page, fake, dm)
        await page.go_back()
        await _shows(page, fake, GROUP)
    assert fake.dms() == [ANN, BOB]
    assert await page.evaluate(ERR) == ""
    assert await page.evaluate("window.__kept") == 1  # switched in place, as a row of the list does


@BOTH
async def test_a_name_switches_in_list_mode_too(split: ux.Split) -> None:
    """023-AC2: without the list (chatnav list), the page goes to the 1:1; Back returns."""
    page, fake = await _open(split, query="")
    await page.locator(_who(0)).click()
    await _shows(page, fake, DM_ANN, "")
    await page.go_back()
    await _shows(page, fake, GROUP, "")


@BOTH
@pytest.mark.parametrize("click", [{"button": "middle"}, {"modifiers": ["Control"]}, {"modifiers": ["Meta"]}])
async def test_middle_or_ctrl_click_opens_the_one_to_one_in_a_new_tab(
    split: ux.Split, click: dict[str, Any]
) -> None:
    """023-AC3: middle click or Ctrl/⌘ click opens the 1:1 in a new tab (split, as the list's rows); the
    shown chat stays."""
    page, fake = await _open(split)
    ws = fake.ws
    async with page.context.expect_page() as tab:
        await page.locator(_who(0)).click(**click)
    new = await tab.value
    await new.wait_for_url(_url(DM_ANN))
    fake.ws = ws  # the new tab's page dialled the fake too
    fake.send({"dm": ANN, "chat": DM_ANN})  # a repeated answer opens no second tab
    await fake.handled()
    assert len(page.context.pages) == 2
    await new.close()
    assert page.url == _url(GROUP)
    assert await page.evaluate("window.__out.filter(f => 'follow' in f).map(f => f.follow)") == [GROUP]


@BOTH
async def test_a_right_click_on_a_name_asks_nothing(split: ux.Split) -> None:
    """023-AC3: only the middle button opens a tab; the right one keeps its menu."""
    page, fake = await _open(split)
    await page.locator(_who(0)).click(button="right")
    await page.keyboard.press("Escape")
    await fake.settled()
    assert fake.dms() == []
    assert len(page.context.pages) == 1


@BOTH
async def test_with_browser_set_a_new_tab_goes_through_the_server_for_a_board_chat(split: ux.Split) -> None:
    """023-AC3: with --browser, the server opens a 1:1 on the board, as for the list's rows; one only in
    the archive (the server would refuse it) opens from the page."""
    assert isinstance(split.fake, Fake)
    split.fake.browser = True
    page, fake = await _open(split)
    await page.locator(_who(0)).click(button="middle")
    await wait(lambda: fake.of("open") == [{"open": DM_ANN, "chat": True, "chatnav": "split"}], "open")
    async with page.context.expect_page() as tab:
        await page.locator(_who(2)).click(button="middle")
    new = await tab.value
    await new.wait_for_url(_url(DM_BOB))
    await new.close()
    await fake.settled()
    assert len(fake.of("open")) == 1
    assert page.url == _url(GROUP)


@BOTH
@pytest.mark.parametrize(
    ("i", "error"), [(3, "no private chat with them yet"), (6, "unknown person"), (0, "failed")]
)
async def test_a_lookup_that_finds_nothing_says_so(split: ux.Split, i: int, error: str) -> None:
    """023-AC5: no 1:1, an unknown person or a failed lookup says so on the error line; the page stays."""
    page, fake = await _open(split)
    fake.fails = error == "failed"
    await page.locator(_who(i)).click()
    await until(page, f"{ERR} === {'private chat: ' + error!r}")
    await fake.settled()
    assert page.url == _url(GROUP)
    assert fake.follows()[-1] == GROUP


@BOTH
async def test_only_the_latest_name_asked_is_answered(split: ux.Split) -> None:
    """023-AC2: Ann's name then Bob's: Ann's late answer moves nothing, Bob's switches."""
    page, fake = await _open(split)
    fake.hold = True
    await page.locator(_who(0)).click()
    await page.locator(_who(2)).click()
    await wait(lambda: len(fake.held) == 2, "two lookups")
    fake.release(0)
    await fake.handled()
    assert page.url == _url(GROUP)
    fake.release(1)
    await _shows(page, fake, DM_BOB)
    fake.send({"dm": BOB, "chat": DM_BOB})  # answered already: nothing asked, nothing to do
    await fake.handled()


@BOTH
async def test_an_answer_after_leaving_the_chat_moves_nothing(split: ux.Split) -> None:
    """023-AC2: the page switched to another chat before the answer: it stays there."""
    page, fake = await _open(split)
    fake.hold = True
    await page.locator(_who(0)).click()
    await wait(lambda: len(fake.held) == 1, "lookup")
    await page.locator(f'#rows li[data-id="{_id(1)}"] a.name').click()
    await _shows(page, fake, _id(1))
    fake.release(0)
    await fake.handled()
    assert page.url == _url(_id(1))
    assert await page.evaluate(ERR) == ""


@BOTH
async def test_a_name_clicked_offline_says_so(split: ux.Split) -> None:
    """023-AC5: with the socket down, the lookup cannot be asked: the error line says so."""
    page, fake = await _open(split)
    await fake.ws.close()
    await page.wait_for_function("window.__ws.readyState === 3")
    await page.locator(_who(0)).click()
    assert await page.evaluate(ERR) == "offline, private chat not opened"


@BOTH
@pytest.mark.parametrize("click", [{}, {"modifiers": ["Control"]}])
async def test_a_clicked_name_does_not_pin_the_action_bar(split: ux.Split, click: dict[str, Any]) -> None:
    """023-AC2 with 011-AC12: a name clicked (or Ctrl clicked) with no 1:1 leaves no bar pinned once the
    pointer leaves; a keyboard's focus stays on the name, Enter or an assistive tech's click alike."""
    page, fake = await _open(split)
    bar = page.locator('[data-id="1003"] .actions')
    await page.locator(_who(3)).click(**click)
    await until(page, f"{ERR} === 'private chat: no private chat with them yet'")
    await page.locator("#reply").hover()
    assert await bar.is_hidden()
    await page.locator(_who(3)).focus()
    await page.keyboard.press("Enter")
    await page.locator(_who(3)).evaluate("b => b.click()")  # detail 0, as a screen reader's
    await wait(lambda: len(fake.dms()) == 3, "lookups")
    await fake.settled()
    assert await page.evaluate("document.activeElement.textContent") == "Cy Tan"


@BOTH
async def test_selecting_a_name_does_not_leave(split: ux.Split) -> None:
    """023-AC2: a drag selecting a name (to copy it) asks nothing."""
    page, fake = await _open(split)
    name = page.locator(_who(0))
    box = await name.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    await page.mouse.move(box["x"] + 1, y)
    await page.mouse.down()
    await page.mouse.move(box["x"] + box["width"] - 1, y, steps=5)
    await page.mouse.up()
    assert await page.evaluate("getSelection().toString()") != ""
    await fake.settled()
    assert fake.dms() == []
    assert page.url == _url(GROUP)
