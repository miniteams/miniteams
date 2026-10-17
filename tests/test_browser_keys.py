"""Keyboard navigation in the chat page (spec 018), against a fake chat socket. Synthetic data, no network,
no Teams.

Run: `uv run --with playwright pytest tests/test_browser_keys.py`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import browser_helpers
import pytest
import test_browser_split_ux as ux
from browser_helpers import Page, Pages, rendered, until, wait  # skips without playwright
from test_browser_split_ux import _board, _id

from miniteams.render import files_html

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)

ME = "8:orgid:00000000-0000-0000-0000-00000000000a"
ANN = "8:orgid:00000000-0000-0000-0000-00000000000b"
GROUP = _id(2)
OLDER, LATEST = range(1000, 1020), range(1020, 1060)  # the page above, the latest page
QUOTING, QUOTED = 1052, 1030  # a reply on the latest page quoting a message on it
HEAD, CONT = 1053, 1054  # Ann twice within a minute: the second one folds under the first's head
RUN, FOLDED = 1055, 1056  # two deleted messages in a row: the second folds into the run
MINE, LAST = 1058, 1059  # my editable message, then Ann's, the chat's last
LINKED = 1050  # a URL then a shared file; its neighbours carry a link each
CARD = 1049  # a card with a show-more toggle (Action.ShowCard)
WIDE = 1047  # a code block wider than the thread: a scroller
FILE = "https://contoso.sharepoint.com/sites/team/Shared%20Documents/plan.pdf"
LINKS = {
    1048: ("https://example.org/b",),
    LINKED: ("https://example.org/a", FILE),
    1051: ("https://example.org/c",),
}


def _msg(i: int) -> dict[str, Any]:
    mine = i in (MINE, 1057)
    minute = i * 10 - (9 if i == CONT else 0)  # ten minutes apart: every message its own head
    links = "".join(f' <a href="{u}" rel="noopener" target="_blank">{u}</a>' for u in LINKS.get(i, ()))
    html = f"<p>m{i}{links}</p>"
    if i == CARD:
        html += '<div class="card"><details><summary>more</summary><p>shown</p></details></div>'
    if i == WIDE:
        html += f"<pre><code>{'x' * 400}</code></pre>"
    if i == QUOTING:
        quote = (
            f'<blockquote class="reply" data-msg="{QUOTED}"><strong>Ann</strong><p>m{QUOTED}</p></blockquote>'
        )
        html = quote + html
    return {
        "id": str(i),
        "at": f"2026-10-0{1 + minute // 1440}T{minute // 60 % 24:02d}:{minute % 60:02d}:00.000Z",
        "from": ME if mine else ANN,
        "name": "Me" if mine else "Ann Lee",
        "me": mine,
        "type": "text",
        "html": html,
        "editable": i not in (RUN, FOLDED),
        "deleted": i in (RUN, FOLDED),
        "reactions": [],
    }


@dataclass
class Fake(ux.Fake):
    """The split fake whose chat holds OLDER then LATEST; member completion lists Ann; `read_only` follows."""

    read_only: bool = False
    latest: list[dict[str, Any]] | None = None  # the latest page, LATEST when None

    def on_verb(self, msg: dict[str, Any]) -> None:
        thread = msg.get("history")
        if "follow" in msg:
            label = "Fictional chat · 3p"
            self.send({"follow": msg["follow"], "label": label, "page": "1", "read_only": self.read_only})
        elif "complete" in msg:
            items = [{"name": "Ann Lee", "mri": ANN}] if msg["complete"] == "member" else []
            self.send({"complete": msg["complete"], "q": msg["q"], "items": items})
        elif thread is None:
            super().on_verb(msg)
        elif msg.get("before"):
            frame = {"history": thread, "before": msg["before"], "source": "archive"}
            self.send({**frame, "messages": [_msg(i) for i in OLDER]})
        elif "around" in msg:  # a jump or a resync: the whole chat, both quoted messages in it
            frame = {"history": thread, "around": msg["around"], "source": "archive"}
            self.send({**frame, "messages": [_msg(i) for i in (*OLDER, *LATEST)]})
        elif "after" not in msg:
            messages = self.latest or [_msg(i) for i in LATEST]
            self.send(
                {"history": thread, "messages": messages, "older": self.latest is None, "source": "archive"}
            )

    def befores(self) -> list[str]:
        return [v["before"] for v in self.of("history") if v.get("before")]


@pytest.fixture
async def split(pages: Pages) -> AsyncIterator[ux.Split]:
    found = ux.Split(pages, Fake(rows=_board(4)))
    yield found
    await pages.close()
    assert pages.external == []


async def _open(split: ux.Split, **ctx: Any) -> tuple[Page, Fake]:
    page = await split.open(viewport=ctx.pop("viewport", {"width": 1300, "height": 700}), **ctx)
    await page.wait_for_selector(f'[data-id="{LAST}"]')
    assert isinstance(split.fake, Fake)
    return page, split.fake


FOCUSED = "document.activeElement.dataset.id ?? document.activeElement.id"
# The focused message lies inside the list's view and wears the focus outline.
SHOWN = """id => { const n = document.activeElement, l = document.getElementById("list");
  if (n.dataset.id !== id) return false;
  const a = n.getBoundingClientRect(), b = l.getBoundingClientRect();
  return a.top >= b.top - 1 && a.bottom <= b.bottom + 1 && getComputedStyle(n).outlineStyle === "solid"; }"""


async def _at(page: Page) -> str:
    return str(await page.evaluate(FOCUSED))


async def _shown(page: Page, mid: int) -> None:
    await until(page, f"({SHOWN})('{mid}')")


async def _enter_thread(page: Page) -> None:
    """↑ from an empty composer: the keyboard's own way in."""
    await page.locator("#reply").focus()
    await page.keyboard.press("ArrowUp")
    await _shown(page, LAST)


async def _walk_to(page: Page, mid: int) -> None:
    """From the composer, ↑ message by message up to `mid`."""
    await _enter_thread(page)
    for _ in range(60):
        if await _at(page) == str(mid):
            return
        await page.keyboard.press("ArrowUp")
    raise AssertionError(f"{mid} not reached")


# --- AC1: composer → thread ---


@BOTH
@pytest.mark.parametrize(
    ("text", "caret"),
    [("", 0), ("one line", 8), ("one line", 0), ("first\nsecond", 3), ("word " * 60, 0)],
)
async def test_up_from_the_composer_on_its_first_visual_line_focuses_the_last_message(
    split: ux.Split, text: str, caret: int
) -> None:
    """018-AC1: ↑ in an empty composer, or with the caret on its first visual line, focuses the last
    message."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.fill(text)
    await box.evaluate("(b, at) => b.setSelectionRange(at, at)", caret)
    await box.press("ArrowUp")
    await _shown(page, LAST)
    assert await box.input_value() == text


@BOTH
@pytest.mark.parametrize(("text", "caret"), [("first\nsecond", 9), ("word " * 60, 300)])
async def test_up_below_the_first_visual_line_stays_in_the_composer(
    split: ux.Split, text: str, caret: int
) -> None:
    """018-AC1: below the first line, a typed one or one the box wrapped, ↑ moves the caret, not the focus."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.fill(text)
    await box.evaluate("(b, at) => b.setSelectionRange(at, at)", caret)
    two_lines = "b => b.scrollHeight >= 2 * parseFloat(getComputedStyle(b).lineHeight)"
    assert await box.evaluate(two_lines)
    await box.press("ArrowUp")
    assert await _at(page) == "reply"
    assert await box.evaluate("b => b.selectionStart") < caret


@BOTH
@pytest.mark.parametrize(("shift", "stays"), [(0, True), (1, True), (-3, False)])
async def test_up_at_a_wrapped_lines_first_word_follows_the_line_the_word_is_on(
    split: ux.Split, shift: int, stays: bool
) -> None:
    """018-AC1: a caret at, or inside, the first word of the second visual line is on that line, the whole
    word wrapped with it: ↑ moves the caret; inside the first line's last word, ↑ leaves."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.fill("word " * 60)
    await box.evaluate("b => b.setSelectionRange(0, 0)")
    await box.press("End")  # the browser's own end of the first visual line
    start2 = await box.evaluate("b => b.value.slice(b.selectionStart).search(/\\S/) + b.selectionStart")
    await box.evaluate("(b, at) => b.setSelectionRange(at, at)", start2 + shift)
    await box.press("ArrowUp")
    if stays:
        assert await _at(page) == "reply"
        assert await box.evaluate("b => b.selectionStart") < start2
    else:
        await _shown(page, LAST)


@BOTH
async def test_up_with_a_selection_stays_in_the_composer(split: ux.Split) -> None:
    """018-AC1: not with a selection."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.fill("one line")
    await box.evaluate("b => b.setSelectionRange(0, 3)")
    await box.press("ArrowUp")
    assert await _at(page) == "reply"


@BOTH
async def test_up_with_the_completion_list_open_walks_the_list(split: ux.Split) -> None:
    """018-AC1: not with an open completion list: ↑ is the list's."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.press_sequentially("@ann")
    await browser_helpers.shows(page, ["Ann Lee"])
    await box.press("ArrowUp")
    assert await _at(page) == "reply"
    assert not await browser_helpers.closed(page)


@BOTH
@pytest.mark.parametrize("ime", ["isComposing", "keyCode 229"])
async def test_up_during_an_ime_composition_stays_in_the_composer(split: ux.Split, ime: str) -> None:
    """018-AC1: not during an IME composition (Firefox flags it, Chrome sends keyCode 229)."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.focus()
    await box.evaluate(
        """(b, ime) => { const e = new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true,
          cancelable: true, isComposing: ime === "isComposing" });
          if (ime !== "isComposing") Object.defineProperty(e, "keyCode", { get: () => 229 });
          b.dispatchEvent(e); }""",
        ime,
    )
    assert await _at(page) == "reply"


# --- AC2: the thread ---


@BOTH
async def test_arrows_walk_the_messages_folded_runs_included(split: ux.Split) -> None:
    """018-AC2: ↑/↓ move by message; a grouped message is a step, its head in view with it; a deleted run
    is one step, its head; the focused message is in view and outlined."""
    page, _ = await _open(split, viewport={"width": 1300, "height": 400})  # room below CONT to scroll it up
    await _enter_thread(page)
    path = [await _at(page)]
    for _ in range(6):
        await page.keyboard.press("ArrowUp")
        path.append(await _at(page))
    assert path == [str(i) for i in (LAST, MINE, 1057, RUN, CONT, HEAD, QUOTING)]
    assert await page.locator(f'[data-id="{CONT}"]').evaluate("n => n.classList.contains('cont')")
    # The grouped message at the list's top edge, its head above it out of view: ↑ onto it shows both.
    await page.locator(f'[data-id="{RUN}"]').evaluate("n => n.focus({ preventScroll: true })")
    top = "id => document.querySelector(`[data-id='${id}']`).getBoundingClientRect().top"
    lst = "document.getElementById('list')"
    await page.evaluate(
        f"(l => {{ l.scrollTop += ({top})('{CONT}') - l.getBoundingClientRect().top; }})({lst})"
    )
    await rendered(page)
    assert await page.evaluate(
        f"({top})('{HEAD}') < document.getElementById('list').getBoundingClientRect().top"
    )
    await page.keyboard.press("ArrowUp")
    await _shown(page, CONT)
    assert await page.evaluate(
        f"({top})('{HEAD}') >= document.getElementById('list').getBoundingClientRect().top - 1"
    )


@BOTH
async def test_page_keys_move_by_a_screen_and_home_end_to_the_ends(split: ux.Split) -> None:
    """018-AC2: PgUp/PgDn move about a screen, Home/End to the first/last loaded message, each in view."""
    page, _ = await _open(split, viewport={"width": 1300, "height": 500})
    await _enter_thread(page)
    await page.keyboard.press("PageUp")
    up = int(await _at(page))
    await _shown(page, up)
    shift = "id => document.querySelector(`[data-id='${id}']`).getBoundingClientRect().top"
    gap = await page.evaluate(f"({shift})('{LAST}') - ({shift})('{up}')")
    height = await page.evaluate("document.getElementById('list').clientHeight")
    assert height <= gap < 2 * height
    await page.keyboard.press("PageDown")
    assert up < int(await _at(page)) <= LAST
    await page.keyboard.press("End")
    await _shown(page, LAST)
    await page.keyboard.press("PageDown")  # at the end: stays
    await _shown(page, LAST)


@BOTH
async def test_reaching_the_top_loads_older_history(split: ux.Split) -> None:
    """018-AC2: Home reaches the first loaded message, which loads the page above as scrolling does; the
    focus stays on that message, ↑ then steps into the older page."""
    page, fake = await _open(split)
    await _enter_thread(page)
    await page.keyboard.press("Home")
    await _shown(page, LATEST[0])
    await wait(lambda: fake.befores() != [], "older page asked")
    await page.wait_for_selector(f'[data-id="{OLDER[-1]}"]')
    assert await _at(page) == str(LATEST[0])
    await page.keyboard.press("ArrowUp")
    await _shown(page, OLDER[-1])


@BOTH
async def test_the_thread_is_one_tab_stop_that_follows_the_focus(split: ux.Split) -> None:
    """018-AC2: one Tab stop in the thread, the last message until another is focused, then that one: Tab
    out and Shift+Tab back return to it."""
    page, _ = await _open(split)
    stops = "[...document.querySelectorAll('#msgs [tabindex]')].filter(n => n.tabIndex >= 0)"
    stops += ".map(n => n.dataset.id)"
    assert await page.evaluate(stops) == [str(LAST)]
    await _walk_to(page, QUOTING)
    assert await page.evaluate(stops) == [str(QUOTING)]
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.closest('#msgs') === null")
    await page.keyboard.press("Shift+Tab")
    await _shown(page, QUOTING)
    split.fake.send({"thread": GROUP, "msg": _msg(1060)})
    await page.wait_for_selector('[data-id="1060"]')
    assert await page.evaluate(stops) == [str(QUOTING)]  # a new message leaves the stop where it was


@BOTH
async def test_moving_through_the_thread_keeps_the_reader_at_the_bottom(split: ux.Split) -> None:
    """018-AC2: into the thread and back at the bottom, a new message still scrolls the list down to it."""
    page, fake = await _open(split)
    await _enter_thread(page)
    await page.keyboard.press("ArrowUp")
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("ArrowDown")
    assert await _at(page) == "reply"
    await rendered(page)
    fake.send({"thread": GROUP, "msg": _msg(1060)})
    await page.wait_for_selector('[data-id="1060"]')
    await until(
        page, "(l => l.scrollHeight - l.scrollTop - l.clientHeight < 40)(document.getElementById('list'))"
    )
    assert await page.locator("#latest").is_hidden()


# --- AC3: thread → composer ---


@BOTH
@pytest.mark.parametrize("key", ["ArrowDown", "Escape"])
async def test_down_on_the_last_message_or_escape_returns_to_the_composer(split: ux.Split, key: str) -> None:
    """018-AC3: ↓ on the last message, or Escape on any, returns to the composer, its caret at the end."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.fill("draft")
    await box.evaluate("b => b.setSelectionRange(0, 0)")
    await box.press("ArrowUp")
    await _shown(page, LAST)
    if key == "Escape":
        await page.keyboard.press("ArrowUp")
    await page.keyboard.press(key)
    assert await _at(page) == "reply"
    assert await box.evaluate("b => [b.selectionStart, b.selectionEnd]") == [5, 5]
    assert await box.input_value() == "draft"


@BOTH
@pytest.mark.parametrize("key", ["x", "E", "1", "?", "["])
async def test_a_non_action_character_returns_to_the_composer_and_is_typed(split: ux.Split, key: str) -> None:
    """018-AC3: a printable character that is no action key lands at the composer's end (Shift+e is not e)."""
    page, _ = await _open(split)
    box = page.locator("#reply")
    await box.fill("draft ")
    await box.evaluate("b => b.setSelectionRange(0, 0)")
    await box.press("ArrowUp")
    await page.keyboard.press("ArrowUp")
    await page.keyboard.press(key)
    assert await _at(page) == "reply"
    assert await box.input_value() == "draft " + key
    assert await page.locator(".msg .editor, #quotes:not([hidden])").count() == 0


# --- AC4: actions ---


@BOTH
async def test_e_edits_my_message_and_escape_returns_to_it(split: ux.Split) -> None:
    """018-AC4: `e` on my editable message opens its editor as ✎ does (its toolbar one Tab stop too);
    Escape closes it back on the message."""
    page, fake = await _open(split)
    await _walk_to(page, MINE)
    await page.keyboard.press("e")
    editor = page.locator(f'[data-id="{MINE}"] .editor')
    await editor.wait_for()
    assert await editor.evaluate("b => b === document.activeElement")
    await wait(lambda: fake.of("editable") == [{"editable": GROUP, "msg": str(MINE)}], "editable")
    bar = f'[data-id="{MINE}"] .editbox .tools button'
    assert await page.locator(bar).evaluate_all("bs => bs.map(b => b.tabIndex)") == [
        0,
        -1,
        -1,
        -1,
        -1,
        -1,
        -1,
    ]
    await editor.press("Escape")
    await _shown(page, MINE)
    assert await page.locator(".editor").count() == 0


@BOTH
async def test_r_quotes_the_message_in_the_composer(split: ux.Split) -> None:
    """018-AC4: `r` adds the message as a quote, as ↩ does, and lands in the composer."""
    page, _ = await _open(split)
    await _walk_to(page, HEAD)
    await page.keyboard.press("r")
    assert await page.locator("#quotes .quote").evaluate_all("qs => qs.map(q => q.dataset.quote)") == [
        str(HEAD)
    ]
    assert await _at(page) == "reply"


@BOTH
async def test_plus_opens_the_picker_and_a_pick_returns_to_the_message(split: ux.Split) -> None:
    """018-AC4: `+` opens the reaction picker on its first emoji; Enter reacts and the focus is back on the
    message; Escape closes the picker back on the message."""
    page, fake = await _open(split)
    await _walk_to(page, HEAD)
    picker = page.locator(f'[data-id="{HEAD}"] .picker')
    await page.keyboard.press("+")
    assert await picker.is_visible()
    assert await page.evaluate("document.activeElement.dataset.fav") == "👍"
    await page.keyboard.press("Enter")
    await wait(lambda: len(fake.of("react")) == 1, "react")
    assert fake.of("react")[0]["msg"] == str(HEAD)
    await _shown(page, HEAD)
    await page.keyboard.press("+")
    await page.keyboard.press("Escape")
    assert await picker.is_hidden()
    await _shown(page, HEAD)


@BOTH
async def test_escape_with_a_clicked_picker_only_closes_it(split: ux.Split) -> None:
    """018-AC4: Escape on a message whose picker a click opened closes the picker, the focus stays on the
    message, not sent to the composer."""
    page, _ = await _open(split)
    await _walk_to(page, HEAD)
    picker = page.locator(f'[data-id="{HEAD}"] .picker')
    await page.locator(f'[data-id="{HEAD}"] [data-act=react]').click()
    assert await picker.is_visible()
    await page.keyboard.press("Escape")
    assert await picker.is_hidden()
    await _shown(page, HEAD)
    await page.keyboard.press("Escape")  # the picker closed: Escape is the thread's again
    assert await _at(page) == "reply"


@BOTH
async def test_tabbing_out_of_the_picker_closes_it(split: ux.Split) -> None:
    """018-AC4: Tab walks the picker opened by `+`; leaving it closes it, its buttons no Tab stops left."""
    page, _ = await _open(split)
    await _walk_to(page, HEAD)
    picker = page.locator(f'[data-id="{HEAD}"] .picker')
    await page.keyboard.press("+")
    await page.keyboard.press("Tab")
    assert await picker.is_visible()
    assert await picker.evaluate("p => p.contains(document.activeElement)")
    for _ in range(20):
        await page.keyboard.press("Tab")
        if not await picker.evaluate("p => p.contains(document.activeElement)"):
            break
    assert await picker.is_hidden()


@BOTH
async def test_p_asks_for_the_private_chat_with_the_sender(split: ux.Split) -> None:
    """018-AC4: `p` asks for the 1:1 with the sender, as a click on the name does, grouped messages too."""
    page, fake = await _open(split)
    await _walk_to(page, CONT)
    await page.keyboard.press("p")
    await wait(lambda: fake.of("dm") == [{"dm": ANN}], "dm")


@BOTH
async def test_enter_on_a_quoting_message_follows_its_quote(split: ux.Split) -> None:
    """018-AC4: Enter on a message holding a quote jumps to the quoted message, as a click does."""
    page, fake = await _open(split)
    await _walk_to(page, QUOTING)
    await page.keyboard.press("Enter")
    await page.locator(f'.msg.hit[data-id="{QUOTED}"]').wait_for()
    await _shown(page, QUOTED)
    await fake.settled()
    assert [v for v in fake.of("history") if "around" in v] == []


@BOTH
async def test_enter_on_a_quote_not_loaded_lands_the_focus_on_the_quoted_message(split: ux.Split) -> None:
    """018-AC4: Enter on a quote whose message is off the page jumps there, the focus and Tab stop with it."""
    page, fake = await _open(split)
    await page.locator(f'[data-id="{QUOTING}"] blockquote').evaluate(
        f"q => {{ q.dataset.msg = '{OLDER[5]}'; }}"
    )
    await _walk_to(page, QUOTING)
    await page.keyboard.press("Enter")
    await page.locator(f'.msg.hit[data-id="{OLDER[5]}"]').wait_for()
    await _shown(page, OLDER[5])
    assert await page.evaluate("document.querySelector('#msgs > [tabindex=\"0\"]').dataset.id") == str(
        OLDER[5]
    )
    assert len([v for v in fake.of("history") if "around" in v]) == 1


@BOTH
async def test_a_resync_keeps_the_focus_in_the_thread(split: ux.Split) -> None:
    """018-AC2: a page redrawn under the reader (a gap, a reconnect) gives the focus back to their message."""
    page, fake = await _open(split)
    await _walk_to(page, HEAD)
    fake.send({"gap": GROUP})
    await wait(lambda: len(fake.of("history")) == 2, "resync")
    await fake.handled()
    await _shown(page, HEAD)


@BOTH
async def test_a_resync_while_typing_keeps_the_focus_in_the_composer(split: ux.Split) -> None:
    """018-AC2: a page redrawn while the reader types does not pull the focus into the thread."""
    page, fake = await _open(split)
    await _walk_to(page, HEAD)
    await page.keyboard.press("Escape")
    await page.keyboard.type("hello")
    fake.send({"gap": GROUP})
    await wait(lambda: len(fake.of("history")) == 2, "resync")
    await fake.handled()
    await rendered(page)
    assert await _at(page) == "reply"
    assert await page.locator("#reply").input_value() == "hello"


@BOTH
async def test_a_trim_of_the_focused_message_keeps_the_focus_in_the_thread(split: ux.Split) -> None:
    """018-AC2: a message arriving at the bottom trims the oldest one off a full screen; were it focused,
    the focus goes to the thread's stop, not lost to the page."""
    assert isinstance(split.fake, Fake)
    many = range(2000, 2600)  # LIVE_MAX of them
    split.fake.latest = [{**_msg(LAST), "id": str(i), "html": f"<p>m{i}</p>"} for i in many]
    page = await split.open(viewport={"width": 1300, "height": 700})
    await page.wait_for_selector(f'[data-id="{many[-1]}"]')
    await page.locator(f'[data-id="{many[0]}"]').evaluate("n => n.focus({ preventScroll: true })")
    split.fake.send({"thread": GROUP, "msg": {**_msg(LAST), "id": "2600", "html": "<p>m2600</p>"}})
    await page.wait_for_selector('[data-id="2600"]')
    await page.wait_for_selector(f'[data-id="{many[0]}"]', state="detached")
    assert await page.evaluate("document.activeElement.closest('#msgs > [tabindex=\"0\"]') !== null")


@BOTH
@pytest.mark.parametrize("control", ["blockquote", ".run"])
async def test_thread_keys_work_from_a_clicked_control_of_a_message(split: ux.Split, control: str) -> None:
    """018-AC2: a click on a quote or the run's toggle focuses it; the arrows still walk from its message."""
    page, _ = await _open(split)
    mid = QUOTING if control == "blockquote" else RUN
    await page.locator(f'[data-id="{mid}"] {control}').click()
    await page.keyboard.press("ArrowUp")
    await _shown(page, mid - 1)


@BOTH
async def test_altgr_characters_return_to_the_composer(split: ux.Split) -> None:
    """018-AC3: AltGr arrives as Ctrl+Alt on Windows: its character is typed; a real Ctrl+Alt is not."""
    page, _ = await _open(split)
    await _walk_to(page, HEAD)
    press = """altGr => document.activeElement.dispatchEvent(new KeyboardEvent("keydown",
      { key: "{", ctrlKey: true, altKey: true, modifierAltGraph: altGr, bubbles: true, cancelable: true }))"""
    await page.evaluate(f"({press})(false)")
    assert await _at(page) == str(HEAD)
    await page.evaluate(f"({press})(true)")
    assert await _at(page) == "reply"
    assert await page.locator("#reply").input_value() == "{"


@BOTH
@pytest.mark.parametrize("mac", [True, False])
async def test_a_macos_option_character_returns_to_the_composer(split: ux.Split, mac: bool) -> None:
    """018-AC3: on macOS Option types characters (Option+r is ®): typed in the composer; elsewhere Alt+key
    is a shortcut, left alone."""
    init = """Object.defineProperty(Navigator.prototype, "platform", { get: () => "MacIntel" });
      Object.defineProperty(Navigator.prototype, "userAgentData", { get: () => ({ platform: "macOS" }) });"""
    page, _ = await _open(split, init=init if mac else "")
    await _walk_to(page, HEAD)
    await page.evaluate(
        """() => document.activeElement.dispatchEvent(new KeyboardEvent("keydown",
          { key: "®", altKey: true, bubbles: true, cancelable: true }))"""
    )
    assert await _at(page) == ("reply" if mac else str(HEAD))
    assert await page.locator("#reply").input_value() == ("®" if mac else "")


@BOTH
async def test_plus_with_the_picker_open_keeps_it_open(split: ux.Split) -> None:
    """018-AC4: `+` on a message whose picker a click opened focuses its first emoji, it does not close it."""
    page, _ = await _open(split)
    await _walk_to(page, HEAD)
    await page.locator(f'[data-id="{HEAD}"] [data-act=react]').click()
    picker = page.locator(f'[data-id="{HEAD}"] .picker')
    assert await picker.is_visible()
    assert await _at(page) == str(HEAD)
    await page.keyboard.press("+")
    assert await picker.is_visible()
    assert await page.evaluate("document.activeElement.dataset.fav") == "👍"


@BOTH
async def test_enter_on_a_deleted_run_toggles_it(split: ux.Split) -> None:
    """018-AC4: Enter on a run's head unfolds it, as its toggle does; its messages become steps."""
    page, _ = await _open(split)
    await _walk_to(page, RUN)
    await page.keyboard.press("Enter")
    await page.locator(f'[data-id="{FOLDED}"]').wait_for()
    await _shown(page, RUN)
    await page.keyboard.press("ArrowDown")
    await _shown(page, FOLDED)


@BOTH
async def test_enter_on_a_message_not_editable_here_opens_it_in_teams(split: ux.Split) -> None:
    """018-AC4: after `e` on a message the server will not edit here, Enter opens it in Teams as its
    "Open in Teams" link does."""
    page, fake = await _open(split)
    await page.evaluate("window.open = (u) => { window.opened = u; }")
    await _walk_to(page, MINE)
    await page.keyboard.press("e")
    await wait(lambda: fake.of("editable") != [], "editable")
    fake.send({"editable": GROUP, "msg": str(MINE), "blocked": "a card"})
    await page.locator(f'[data-id="{MINE}"] .blocked button').wait_for()
    await _shown(page, MINE)
    await page.keyboard.press("Enter")
    opened = str(await page.evaluate("window.opened ?? ''"))
    assert opened.startswith("https://teams.microsoft.com/l/message/") and f"/{MINE}?" in opened


@BOTH
@pytest.mark.parametrize(
    ("mid", "key"),
    [(LAST, "e"), (RUN, "e"), (RUN, "r"), (RUN, "+"), (MINE, "p"), (LAST, "Enter")],
)
async def test_an_action_key_does_nothing_where_its_button_is_not_offered(
    split: ux.Split, mid: int, key: str
) -> None:
    """018-AC4: `e` on someone else's message, `e` `r` `+` on a deleted one, `p` on mine, Enter without a
    quote: nothing happens, nothing is typed, the focus stays."""
    page, fake = await _open(split)
    await _walk_to(page, mid)
    await page.keyboard.press(key)
    await fake.settled()
    assert fake.of("editable") == fake.of("dm") == fake.of("react") == []
    assert len(fake.of("history")) == 1
    assert await page.locator(".editor, .msg.hit, .picker:not([hidden]), #quotes .quote").count() == 0
    assert await page.locator("#reply").input_value() == ""
    assert await _at(page) == str(mid)


@BOTH
async def test_a_read_only_chat_offers_no_action_keys(split: ux.Split) -> None:
    """018-AC4: in a read-only chat the action bar offers no quote nor reaction: `r` and `+` do nothing; a
    letter has no composer to land in."""
    assert isinstance(split.fake, Fake)
    split.fake.read_only = True
    page, fake = await _open(split)
    await until(page, "document.body.classList.contains('readonly')")
    await page.locator(f'[data-id="{HEAD}"]').focus()
    for key in ("r", "+", "x"):
        await page.keyboard.press(key)
    await fake.settled()
    assert fake.of("react") == []
    assert await page.locator("#reply").input_value() == ""
    assert await page.locator(".picker:not([hidden]), #quotes .quote").count() == 0


# --- AC5: Tab stops ---


@BOTH
async def test_per_message_controls_leave_the_tab_order(split: ux.Split) -> None:
    """018-AC5: action buttons, sender names, quotes, reaction chips, run toggles, card toggles and the
    "can't edit here" link are out of the Tab order; they stay clickable."""
    page, fake = await _open(split)
    fake.send(
        {"thread": GROUP, "edit": {**_msg(HEAD), "reactions": [{"emoji": "👍", "key": "like", "count": 1}]}}
    )
    await page.locator(f'[data-id="{HEAD}"] .reacts button').wait_for()
    await _walk_to(page, MINE)
    await page.keyboard.press("e")
    await wait(lambda: fake.of("editable") != [], "editable")
    fake.send({"editable": GROUP, "msg": str(MINE), "blocked": "a card"})
    await page.locator(f'[data-id="{MINE}"] .blocked button').wait_for()
    controls = (
        "#msgs .actions button, #msgs .who, #msgs blockquote[role=link], #msgs .reacts button, #msgs .run,"
        " #msgs summary, #msgs .blocked button"
    )
    kinds = await page.locator(controls).evaluate_all(
        "ns => [...new Set(ns.map(n => n.matches('.who') ? 'who' : n.matches('blockquote') ? 'quote'"
        " : n.matches('.reacts *') ? 'chip' : n.matches('.run') ? 'run' : n.matches('summary') ? 'card'"
        " : n.matches('.blocked *') ? 'blocked' : 'action'))].sort()"
    )
    assert kinds == ["action", "blocked", "card", "chip", "quote", "run", "who"]
    assert await page.locator(controls).evaluate_all("ns => ns.filter(n => n.tabIndex >= 0).length") == 0
    await page.locator(f'[data-id="{HEAD}"] .who').click()
    await wait(lambda: fake.of("dm") == [{"dm": ANN}], "dm")


@BOTH
async def test_a_cards_toggle_is_a_tab_stop_only_in_the_focused_message(split: ux.Split) -> None:
    """018-AC5: a card's show-more toggle is out of Tab like a link, except in the focused message: Tab
    reaches it there, Escape returns to its message, ↓ hands the stop on."""
    page, _ = await _open(split)
    toggle = page.locator(f'[data-id="{CARD}"] summary')
    assert await toggle.evaluate("s => s.tabIndex") == -1
    await _walk_to(page, CARD)
    assert await toggle.evaluate("s => s.tabIndex") == 0
    await page.keyboard.press("Tab")
    assert await toggle.evaluate("s => s === document.activeElement")
    await page.keyboard.press("Escape")
    await _shown(page, CARD)
    await page.keyboard.press("ArrowDown")
    assert await toggle.evaluate("s => s.tabIndex") == -1


@BOTH
async def test_the_toolbar_is_one_tab_stop_walked_by_arrows(split: ux.Split) -> None:
    """018-AC5: the formatting bar is one Tab stop; ←/→ walk it and wrap, Home/End reach its ends; the stop
    follows the focus; Tab leaves it for the composer."""
    page, _ = await _open(split)
    labels = "document.activeElement.ariaLabel"
    assert await page.locator("#tools").get_attribute("role") == "toolbar"
    await page.locator("#reply").focus()
    await page.keyboard.press("Shift+Tab")
    assert await page.evaluate(labels) == "bold"
    walk = []
    for key in ("ArrowRight", "ArrowRight", "ArrowLeft", "ArrowLeft", "ArrowLeft", "Home", "End"):
        await page.keyboard.press(key)
        walk.append(await page.evaluate(labels))
    assert walk == ["italic", "strikethrough", "italic", "bold", "preview", "bold", "preview"]
    stops = (
        "[...document.querySelectorAll('#tools button')].filter(b => b.tabIndex >= 0).map(b => b.ariaLabel)"
    )
    assert await page.evaluate(stops) == ["preview"]
    await page.keyboard.press("Tab")
    assert await _at(page) == "reply"
    await page.keyboard.press("Shift+Tab")
    assert await page.evaluate(labels) == "preview"


@BOTH
async def test_tab_hops_header_thread_toolbar_composer(split: ux.Split) -> None:
    """018-AC5: Tab from the header's last control goes thread → toolbar → composer, Shift+Tab back."""
    page, _ = await _open(split)
    await page.locator("#gear").focus()
    zone = (
        "(e => e.closest('#msgs') ? 'thread:' + e.dataset.id : e.closest('#tools') ? 'toolbar'"
        " : e.closest('header') ? 'header' : e.id)(document.activeElement)"
    )
    path = []
    for key in ["Tab"] * 3 + ["Shift+Tab"] * 3:
        await page.keyboard.press(key)
        path.append(await page.evaluate(zone))
    assert path == [f"thread:{LAST}", "toolbar", "reply", "toolbar", f"thread:{LAST}", "header"]


@BOTH
async def test_a_wide_code_block_is_a_tab_stop_only_in_the_focused_message(split: ux.Split) -> None:
    """018-AC5: a scrollable code block is out of Tab like a link, except in the focused message: Tab
    reaches it there and ←/→ scroll it."""
    page, _ = await _open(split)
    pre = page.locator(f'[data-id="{WIDE}"] pre')
    assert await pre.evaluate("p => p.scrollWidth > p.clientWidth")
    await page.locator("#gear").focus()
    await page.keyboard.press("Tab")
    assert await _at(page) == str(LAST)
    await _walk_to(page, WIDE)
    await page.keyboard.press("Tab")
    assert await pre.evaluate("p => p === document.activeElement")
    await page.keyboard.press("ArrowRight")
    await until(page, f"document.querySelector('[data-id=\"{WIDE}\"] pre').scrollLeft > 0")
    assert await pre.evaluate("p => p === document.activeElement")


TABBABLE = "[...document.querySelectorAll('#msgs a[href]')].filter(a => a.tabIndex >= 0).map(a => a.href)"


@BOTH
async def test_tab_walks_the_focused_messages_links_then_the_toolbar(split: ux.Split) -> None:
    """018-AC5: only the focused message's links are Tab stops; Tab walks them then the toolbar, Shift+Tab
    walks back to the message."""
    page, _ = await _open(split)
    await _walk_to(page, LINKED)
    assert await page.evaluate(TABBABLE) == list(LINKS[LINKED])
    focus = "(e => e.closest('#tools') ? 'toolbar' : e.href || e.dataset.id)(document.activeElement)"
    path = []
    for key in ["Tab"] * 3 + ["Shift+Tab"] * 3:
        await page.keyboard.press(key)
        path.append(await page.evaluate(focus))
    url, file = LINKS[LINKED]
    assert path == [url, file, "toolbar", file, url, str(LINKED)]


@BOTH
async def test_moving_the_focus_moves_the_tabbable_links(split: ux.Split) -> None:
    """018-AC5: ↓ hands the Tab stops to the next message's links."""
    page, _ = await _open(split)
    await _walk_to(page, LINKED)
    await page.keyboard.press("ArrowDown")
    await _shown(page, LINKED + 1)
    assert await page.evaluate(TABBABLE) == list(LINKS[LINKED + 1])


@BOTH
async def test_an_arrival_or_a_redraw_keeps_the_focused_messages_links_tabbable(split: ux.Split) -> None:
    """018-AC5: a live message with a link, then a reaction redrawing the focused one, leave the Tab stops on
    the focused message's links."""
    page, fake = await _open(split)
    await _walk_to(page, LINKED)
    fake.send(
        {"thread": GROUP, "msg": {**_msg(1060), "html": '<p><a href="https://example.org/d">d</a></p>'}}
    )
    await page.wait_for_selector('[data-id="1060"]')
    assert await page.evaluate(TABBABLE) == list(LINKS[LINKED])
    like = [{"emoji": "👍", "key": "like", "count": 1}]
    fake.send({"thread": GROUP, "edit": {**_msg(LINKED), "reactions": like}})
    await page.locator(f'[data-id="{LINKED}"] .reacts button').wait_for()
    assert await page.evaluate(TABBABLE) == list(LINKS[LINKED])
    assert await _at(page) == str(LINKED)


@BOTH
async def test_a_file_cards_link_is_a_tab_stop_only_in_the_focused_message(split: ux.Split) -> None:
    """018-AC5, 027-AC2: a shared file's card link is out of Tab like a link, except in the focused message;
    its unlinked card is never a stop."""
    page, fake = await _open(split)
    await _walk_to(page, LINKED)
    files = {**_msg(1060), "html": "<p>files</p>" + files_html(browser_helpers.FILES)}
    fake.send({"thread": GROUP, "msg": files})
    card = page.locator('[data-id="1060"] a.file')
    await card.wait_for()
    assert await card.evaluate("a => a.tabIndex") == -1
    await page.locator("#reply").focus()
    await page.keyboard.press("ArrowUp")  # the new last message
    await _shown(page, 1060)
    assert await page.evaluate(TABBABLE) == [browser_helpers.SHARE]
    await page.keyboard.press("Tab")
    assert await card.evaluate("a => a === document.activeElement")
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.closest('#tools') !== null")
    await page.keyboard.press("Shift+Tab")
    await page.keyboard.press("Escape")
    await _shown(page, 1060)
    await page.keyboard.press("ArrowUp")
    assert await card.evaluate("a => a.tabIndex") == -1


@BOTH
async def test_arrows_and_escape_from_a_link(split: ux.Split) -> None:
    """018-AC2: ↑/↓ on a link move to the previous/next message; Escape returns to the link's message."""
    page, _ = await _open(split)
    await _walk_to(page, LINKED)
    href = "document.activeElement.href"
    await page.keyboard.press("Tab")
    assert await page.evaluate(href) == LINKS[LINKED][0]
    await page.keyboard.press("ArrowDown")
    await _shown(page, LINKED + 1)
    await page.keyboard.press("Tab")
    assert await page.evaluate(href) == LINKS[LINKED + 1][0]
    await page.keyboard.press("ArrowUp")
    await _shown(page, LINKED)
    await page.keyboard.press("Tab")
    await page.keyboard.press("Escape")
    await _shown(page, LINKED)


@BOTH
@pytest.mark.parametrize("start", ["message", "link"])
async def test_space_scrolls_the_thread_by_a_screen(split: ux.Split, start: str) -> None:
    """018-AC3: Space on a message or its link scrolls a screen down, Shift+Space up; nothing is typed; the
    focus stays while its message is in view, else moves to the first (down) or last (up) one in view."""
    page, _ = await _open(split)
    await _walk_to(page, LINKED)
    if start == "link":
        await page.keyboard.press("Tab")
    top, h = "document.getElementById('list').scrollTop", "document.getElementById('list').clientHeight"
    owner = "document.activeElement.closest('#msgs > .msg').dataset.id"
    seen = """() => { const v = document.getElementById("list").getBoundingClientRect();
      return [...document.querySelectorAll("#msgs > .msg:not(.folded)")].filter(n => {
        const b = n.getBoundingClientRect(); return b.top >= v.top - 1 && b.bottom <= v.bottom + 1;
      }).map(n => n.dataset.id); }"""
    here = "(e => e.href || e.dataset.id)(document.activeElement)"
    focused = await page.evaluate(here)
    screen, bottom = await page.evaluate(h), await page.evaluate(top)
    await page.keyboard.press("Space")  # at the bottom already: no scroll, its message still in view
    assert await page.evaluate(top) == pytest.approx(bottom, abs=2)
    assert await page.evaluate(here) == focused
    assert await page.evaluate(owner) == str(LINKED)
    await page.keyboard.press("Shift+Space")
    assert bottom - await page.evaluate(top) == pytest.approx(screen, abs=2)
    up = await page.evaluate(owner)
    assert up != str(LINKED)
    assert up == (await page.evaluate(seen))[-1]
    await page.keyboard.press("Space")
    assert await page.evaluate(top) == pytest.approx(bottom, abs=2)
    assert await page.evaluate(owner) == (await page.evaluate(seen))[0]
    assert await page.locator("#reply").input_value() == ""


# --- AC6: search ---


@BOTH
async def test_slash_from_the_thread_focuses_the_chat_search(split: ux.Split) -> None:
    """018-AC6: `/` on a message focuses the chat's search box, the slash not typed."""
    page, _ = await _open(split)
    await page.locator("#find").fill("old")
    await _enter_thread(page)
    await page.keyboard.press("/")
    assert await _at(page) == "find"
    assert await page.locator("#find").input_value() == "old"
    assert await page.locator("#find").evaluate("f => [f.selectionStart, f.selectionEnd]") == [0, 3]


@BOTH
@pytest.mark.parametrize("start", ["#reply", "message", "#filter"])
async def test_ctrl_shift_f_focuses_the_chat_search_from_anywhere(split: ux.Split, start: str) -> None:
    """018-AC6: Ctrl+Shift+F focuses the chat search from the composer, the thread or the list."""
    page, _ = await _open(split)
    if start == "message":
        await _enter_thread(page)
    else:
        await page.locator(start).focus()
    await page.keyboard.press("Control+Shift+F")
    assert await _at(page) == "find"


@BOTH
@pytest.mark.parametrize("start", ["#reply", "message"])
async def test_ctrl_f_is_left_to_the_browser(split: ux.Split, start: str) -> None:
    """018-AC6: the page does not take Ctrl+F: not prevented, the focus stays."""
    page, _ = await _open(split)
    await page.evaluate(
        "addEventListener('keydown', (e) => { if (e.key === 'f') window.__taken = e.defaultPrevented; })"
    )
    if start == "message":
        await _enter_thread(page)
    else:
        await page.locator(start).focus()
    before = await _at(page)
    await page.keyboard.press("Control+f")
    assert await page.evaluate("window.__taken") is False
    assert await _at(page) == before


# --- AC7: unchanged keys ---


@BOTH
async def test_ctrl_k_and_the_list_keys_work_from_the_thread(split: ux.Split) -> None:
    """018-AC7: from a focused message Ctrl+K opens the list's filter, ↓ walks its rows from there; the
    rows' own ↓ moves the focus along them."""
    page, _ = await _open(split)
    await _enter_thread(page)
    await page.keyboard.press("Control+k")
    assert await _at(page) == "filter"
    await page.keyboard.press("ArrowDown")
    assert await page.locator("#rows li.sel").get_attribute("data-id") == _id(0)
    assert await _at(page) == "filter"
    await page.locator(f'#rows li[data-id="{_id(0)}"] .name').focus()
    await page.keyboard.press("ArrowDown")
    assert await page.evaluate("document.activeElement.closest('li').dataset.id") == _id(1)
