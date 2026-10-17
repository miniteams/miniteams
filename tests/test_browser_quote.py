"""A reply's quote leads to the quoted message (spec 021), on the chat page against a fake chat socket.
Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_quote.py`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import browser_helpers
import pytest
import test_browser_split_ux as ux
from browser_helpers import ORIGIN, Page, Pages, rendered, until  # skips without playwright
from test_browser_split_ux import _board, _id

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)

ON, OFF = "1001", "1760000000001"  # quoted: on the latest page; older than it
OFF_AT = "2025-10-09T08:53:20.001Z"  # OFF's id as a time
FAR = "99999999999999999999"  # digits, but past any date
LINK = f"{ORIGIN}/linked"
MISS = "quoted message not found"
ERR = "document.getElementById('err').textContent"


def _msg(i: int, html: str = "", mid: str = "", at: str = "") -> dict[str, Any]:
    return {
        "id": mid or str(1000 + i),
        "at": at or f"2026-10-09T10:{i // 60:02d}:{i % 60:02d}.000Z",
        "from": "8:orgid:ann",
        "name": "Ann Lee",
        "me": False,
        "type": "text",
        "html": html or f"<p>m{i}</p>",
        "reactions": [],
    }


def _reply(i: int, quoted: str, inner: str = "<p>the quoted words</p>") -> dict[str, Any]:
    return _msg(
        i,
        f'<blockquote class="reply" data-msg="{quoted}"><strong>Ann Lee</strong>{inner}</blockquote>'
        f"<p>answer {i}</p>",
    )


# The latest page: 40 messages, then replies quoting ON (40), OFF (41), FAR (42), ON with a link (43),
# ON quoting a reply that quotes OFF (44).
LATEST = [
    *(_msg(i) for i in range(40)),
    _reply(40, ON),
    _reply(41, OFF),
    _reply(42, FAR),
    _reply(43, ON, f'<p><a href="{LINK}" rel="noopener" target="_blank">a link</a></p>'),
    _reply(44, ON, f'<div><blockquote class="reply" data-msg="{OFF}"><p>inner words</p></blockquote></div>'),
]


@dataclass
class Fake(ux.Fake):
    """The split fake with LATEST as the latest page; `around` pages (queued while `hold`) hold OFF
    unless `missing`, or answer an error when `fails`; a search hits OFF."""

    missing: bool = False
    fails: bool = False

    def on_verb(self, msg: dict[str, Any]) -> None:
        thread = msg.get("history")
        if thread is None and "search" in msg:
            hit = {"id": OFF, "name": "Ann Lee", "at": OFF_AT, "text": "quoted"}
            self.send({"search": msg["search"], "q": msg["q"], "hits": [hit]})
        elif thread is None:
            super().on_verb(msg)
        elif "around" in msg:
            frame: dict[str, Any] = {"history": thread, "around": msg["around"]}
            if self.fails:
                frame["error"] = "history unavailable"
            else:
                near = [
                    _msg(i, mid=str(1_700_000_000_000 + i), at=f"2025-10-09T08:5{i}:00.000Z")
                    for i in range(3)
                ]
                found = [] if self.missing else [_msg(0, "<p>quoted</p>", OFF, OFF_AT)]
                frame |= {"messages": [*near, *found], "newer": True, "source": "archive"}
            self.answer(frame)
        elif not msg.get("before") and "after" not in msg:
            self.send({"history": thread, "messages": LATEST, "source": "archive"})

    def arounds(self) -> list[str]:
        return [v["around"] for v in self.of("history") if "around" in v]


@pytest.fixture
async def split(pages: Pages) -> AsyncIterator[ux.Split]:
    found = ux.Split(pages, Fake(rows=_board(4)))
    yield found
    await pages.close()
    assert pages.external == []


async def _open(split: ux.Split) -> tuple[Page, Fake]:
    page = await split.open(viewport={"width": 1300, "height": 700})
    await page.wait_for_selector('[data-id="1044"]')
    assert isinstance(split.fake, Fake)
    return page, split.fake


def _quote(i: int) -> str:
    return f'[data-id="{1000 + i}"] blockquote'


# The message is the one marked hit, and it lies inside the list's view.
SHOWN = """id => { const n = document.querySelector(`[data-id="${id}"]`), l = document.getElementById("list");
  if (!n) return false;
  const a = n.getBoundingClientRect(), b = l.getBoundingClientRect();
  return n.classList.contains("hit") && document.querySelectorAll(".msg.hit").length === 1
    && a.top >= b.top && a.bottom <= b.bottom; }"""


async def _shown(page: Page, mid: str) -> None:
    await until(page, f"({SHOWN})({mid!r})")


async def _no_jump(page: Page, fake: Fake) -> None:
    """Nothing jumped: no message marked, no history asked around a time."""
    await fake.settled()
    assert await page.locator(".msg.hit").count() == 0
    assert fake.arounds() == []


@BOTH
async def test_a_quote_on_screen_scrolls_to_its_message(split: ux.Split) -> None:
    """021-AC2: a click on a quote whose message is loaded brings it into view, marked like a search hit,
    and asks no history."""
    page, fake = await _open(split)
    assert not await page.evaluate(f"({SHOWN})({ON!r})")  # scrolled out at the bottom of the page
    assert await page.locator(_quote(40)).evaluate("q => getComputedStyle(q).cursor") == "pointer"
    await page.locator(_quote(40)).click()
    await _shown(page, ON)
    await fake.settled()
    assert fake.arounds() == []
    assert await page.locator("#err").text_content() == ""


@BOTH
async def test_a_quote_off_screen_jumps_to_its_message(split: ux.Split) -> None:
    """021-AC3: a click on a quote whose message is not loaded asks the history around the time its id
    holds, and marks the message once that page shows."""
    page, fake = await _open(split)
    await page.locator(_quote(41)).click()
    await _shown(page, OFF)
    assert fake.arounds() == [OFF_AT]
    assert await page.locator("#err").text_content() == ""


@BOTH
async def test_enter_on_a_quoting_message_jumps(split: ux.Split) -> None:
    """021-AC4 with 018-AC4/AC5: the quote is a link to assistive tech, out of the Tab order; Enter on its
    message jumps like a click, on screen and off it, and so does Enter on the quote itself once focused; the
    focus lands on the quoted message."""
    page, fake = await _open(split)
    assert await page.locator(_quote(40)).get_attribute("role") == "link"
    assert await page.locator(_quote(40)).get_attribute("tabindex") == "-1"
    await page.locator(_quote(43)).focus()
    await page.keyboard.press("Enter")
    await _shown(page, ON)
    assert await page.evaluate("document.activeElement.dataset.id") == ON  # the focus follows the jump
    await page.evaluate("document.querySelector('.msg.hit').classList.remove('hit')")
    for i, mid in ((40, ON), (41, OFF)):
        await page.locator(f'[data-id="{1000 + i}"]').focus()
        await page.keyboard.press("Enter")
        await _shown(page, mid)
        assert await page.evaluate("document.activeElement.dataset.id") == mid
    assert fake.arounds() == [OFF_AT]


@BOTH
@pytest.mark.parametrize("how", ["missing", "fails", "far"])
async def test_a_quote_whose_message_cannot_be_found_says_so(split: ux.Split, how: str) -> None:
    """021-AC5: the page around the quote's time lacks it, that page fails, or the id holds no time:
    "quoted message not found" on the error line, nothing marked."""
    page, fake = await _open(split)
    fake.missing, fake.fails = how == "missing", how == "fails"
    await page.locator(_quote(42 if how == "far" else 41)).click()
    expected = f"{MISS}: history unavailable" if how == "fails" else MISS  # a failure says why
    await until(page, f"{ERR} === {expected!r}")
    await fake.settled()
    assert fake.arounds() == ([] if how == "far" else [OFF_AT])
    assert await page.locator(".msg.hit").count() == 0


@BOTH
async def test_selecting_text_in_a_quote_does_not_jump(split: ux.Split) -> None:
    """021: a drag selecting words in a quote ends in a click that selects them, it does not jump."""
    page, fake = await _open(split)
    words = page.locator(f"{_quote(40)} p")
    await words.scroll_into_view_if_needed()
    box = await words.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    await page.mouse.move(box["x"] + 2, y)
    await page.mouse.down()
    await page.mouse.move(box["x"] + box["width"] / 2, y, steps=5)
    await page.mouse.up()
    assert await page.evaluate("getSelection().toString()") != ""
    await _no_jump(page, fake)


@BOTH
async def test_a_link_inside_a_quote_opens_and_does_not_jump(split: ux.Split) -> None:
    """021: a link in a quote is followed, the quote does not jump."""
    page, fake = await _open(split)
    await page.context.route(LINK, lambda route: route.fulfill(body="linked"))
    async with page.expect_popup() as popup:
        await page.locator(f"{_quote(43)} a").click()
    opened = await popup.value
    await opened.wait_for_load_state()
    assert opened.url == LINK
    await _no_jump(page, fake)


async def _far_miss(page: Page) -> None:
    await page.locator(_quote(42)).click()
    await until(page, f"{ERR} === {MISS!r}")


@BOTH
async def test_a_quote_jump_clears_an_earlier_miss_at_once(split: ux.Split) -> None:
    """021-AC5: after a miss, the next quote's jump clears the error line as it starts (and so the draft
    the chat keeps of it), on screen and off it."""
    page, fake = await _open(split)
    await _far_miss(page)
    await page.locator(_quote(40)).click()
    await _shown(page, ON)
    assert await page.evaluate(ERR) == ""
    await _far_miss(page)
    fake.hold = True
    await page.locator(_quote(41)).click()
    await until(page, "window.__out.some(f => 'around' in f)")
    assert await page.evaluate(ERR) == ""  # cleared before the page answers
    fake.release(0)
    await _shown(page, OFF)


@BOTH
async def test_a_jump_that_lands_clears_an_earlier_quote_miss(split: ux.Split) -> None:
    """021-AC5: a search hit landing after a quote's miss clears the error line."""
    page, _ = await _open(split)
    await _far_miss(page)
    await page.locator("#find").fill("quoted")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()
    await page.locator(f'.msg.hit[data-id="{OFF}"]').wait_for()
    assert await page.evaluate(ERR) == ""


@BOTH
async def test_a_quote_on_screen_drops_a_jump_still_loading(split: ux.Split) -> None:
    """021-AC2: an off-screen quote's page still loading, then an on-screen quote: the late page replaces
    nothing, the on-screen message stays shown."""
    page, fake = await _open(split)
    fake.hold = True
    await page.locator(_quote(41)).click()
    await until(page, "window.__out.some(f => 'around' in f)")
    await page.locator(_quote(40)).click()
    await _shown(page, ON)
    fake.release(0)
    await fake.handled()
    await _shown(page, ON)
    assert await page.locator(f'[data-id="{OFF}"]').count() == 0


@BOTH
async def test_a_quote_inside_a_quote_is_not_a_link_of_its_own(split: ux.Split) -> None:
    """021-AC4: only the outermost quote is a link (named for assistive tech), the one it holds is not:
    a click on the inner words jumps to the outer quote's message."""
    page, fake = await _open(split)
    outer, inner = (
        page.locator(f"{_quote(44)}[data-msg='{ON}']"),
        page.locator(f"{_quote(44)}[data-msg='{OFF}']"),
    )
    assert await outer.get_attribute("role") == "link"
    assert await outer.get_attribute("tabindex") == "-1"  # Enter on its message follows it (018-AC4)
    assert await outer.get_attribute("aria-description") == "go to the quoted message"
    assert await outer.get_attribute("title") == "go to the quoted message"
    assert await inner.get_attribute("role") is None
    assert await inner.get_attribute("tabindex") is None
    await inner.click()
    await _shown(page, ON)
    await fake.settled()
    assert fake.arounds() == []


@BOTH
async def test_a_quote_on_screen_keeps_a_resync_still_loading(split: ux.Split) -> None:
    """021-AC2: a gap's resync (the page around where the reader is) still loading, then an on-screen
    quote: the resync page still lands, with what the gap missed."""
    page, fake = await _open(split)
    await page.evaluate("document.getElementById('list').scrollTop = 0")
    await rendered(page)
    fake.hold = True
    fake.send({"gap": _id(2)})
    await until(page, "window.__out.some(f => 'around' in f)")
    await page.locator(_quote(40)).click()
    await _shown(page, ON)
    fake.release(0)
    await page.locator(f'[data-id="{OFF}"]').wait_for()  # only the resync page holds it
