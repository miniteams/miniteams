"""Chat page composer (spec 012 phases 2 and 3): `:` / `@` completion, toolbar and preview, against a
fake chat socket speaking the 012 wire contract. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_chat_compose.py`.
"""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import browser_helpers
import pytest
from browser_helpers import (  # skips without playwright
    ANN,
    ANN2,
    BOB,
    EMOJIS,
    EVERYONE,
    MALFORMED,
    ORIGIN,
    POP_ASK_MS,
    Page,
    Pages,
    T,
    act,
    closed,
    open_fake,
    past,
    pick_namesakes,
    pick_out_of_order,
    pick_then_glue,
    queue_is,
    row,
    shows,
    until,
    wait,
)

from miniteams import web as W
from miniteams.compose import compose_html

pytestmark = pytest.mark.browser
pages = browser_helpers.pages

L = "19:other@thread.v2"
MINE = {"id": "1", "at": "2026-10-09T10:00:00.000Z", "from": "8:orgid:me", "name": "Me", "me": True}
# A module global read by `_open`: a mutation run swaps it for an altered copy.
PAGE = W._page("chat.html")  # as served: ui.css and ui.js inlined


@dataclass
class Fake(browser_helpers.Fake):
    """The server side of the chat socket: follow, history, board, complete, preview, send."""

    fail: bool = False
    read_only: bool = False
    opener: bool = False
    version: str = "1"
    members: list[dict[str, Any]] = field(default_factory=lambda: [EVERYONE, ANN, BOB])
    history: list[dict[str, Any]] = field(
        default_factory=lambda: [{**MINE, "html": "<p>mine</p>", "editable": True}]
    )
    busy: bool = False  # answer `history` and `search` with `busy`
    follow_error: str = ""  # answer `follow` with this error
    # spec 019: `editable` answers by message id (else a plain form of "mine"), `edit` answers
    forms: dict[str, dict[str, Any]] = field(default_factory=dict)
    edit_error: str = ""

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "follow" in msg and self.follow_error:
            self.send({"follow": msg["follow"], "error": self.follow_error})
        elif "follow" in msg:
            label = "Fictional T · 3p" if msg["follow"] == T else "Fictional L · 3p"
            follow = {"follow": msg["follow"], "label": label, "page": self.version, "opener": self.opener}
            self.send({**follow, "read_only": self.read_only})
        elif "history" in msg:
            if self.busy:
                self.send({"history": msg["history"], "before": msg.get("before"), "error": "busy"})
                return
            messages = self.history if msg["history"] == T else []
            self.send({"history": msg["history"], "messages": messages, "source": "archive"})
        elif "search" in msg:
            self.send(
                {"search": msg["search"], "q": msg["q"], **({"error": "busy"} if self.busy else {"hits": []})}
            )
        elif msg.get("board") is True:
            self.send({"rows": [row(T, "Fictional T"), row(L, "Fictional L")], "browser": False, "me": "Me"})
        elif "complete" in msg:
            q = msg["q"].lower()
            if q == "nu":
                items = MALFORMED
            elif msg["complete"] == "emoji":
                items = [e for e in EMOJIS if e["name"].startswith(q)]
            else:
                items = [
                    m
                    for m in self.members
                    if any(n.startswith(q) for n in [m["name"].lower(), *m["name"].lower().split()])
                ]
            self.answer({"complete": msg["complete"], "q": msg["q"], "items": items[:12]})
        elif "preview" in msg:
            if msg["preview"] == "bad":
                self.answer({"preview": "", "error": "<b>nope</b>"})
            else:
                html, _ = compose_html(msg["preview"], [], {}, full=True)  # the server's own renderer
                self.answer({"preview": f'<div class="pv">{html}</div>'})
        elif "send" in msg:
            self.send({"sent": msg["send"], "ok": not self.fail, "error": "boom" if self.fail else None})
        elif "editable" in msg:
            plain = {"ok": True, "text": "mine", "picks": [], "quotes": [], "images": [], "edittime": ""}
            self.answer({"editable": msg["editable"], "msg": msg["msg"], **self.forms.get(msg["msg"], plain)})
        elif "edit" in msg:
            answer = {"error": self.edit_error} if self.edit_error else {"ok": True}
            self.answer({"edit": msg["edit"], "msg": msg["msg"], **answer})


async def _open(
    pages: Pages, fake: Fake, query: str = "", wait_for: str = '[data-id="1"]', init: str = ""
) -> Page:
    # The real policy: the popup images and the preview must work under it.
    headers = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": W._page_csp(PAGE)}
    page = await open_fake(
        pages,
        fake,
        f"{ORIGIN}/chat/{quote(T, safe='')}{query}",
        PAGE,
        page_route=re.compile(r"^http://127\.0\.0\.1:8/chat/[^/]+$"),
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws\?chat$"),
        headers=headers,
        init=init,
    )
    await wait(lambda: bool(fake.of("history")))
    if wait_for:
        await page.wait_for_selector(wait_for)
    return page


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    board = Fake()
    page = await _open(pages, board)
    await page.locator("#reply").focus()
    yield board
    await pages.close()
    assert pages.external == []


def _parts(fake: Fake) -> tuple[Page, Any]:
    assert fake.page is not None
    return fake.page, fake.page.locator("#reply")


# --- completion: parity with the widget popup (012-AC8) ---


async def test_colon_lists_emoji_enter_inserts_and_does_not_send(fake: Fake) -> None:
    """012-AC8: `:fi` asks once, lists the answer under combobox semantics; Enter inserts 🔥, sends
    nothing."""
    page, box = _parts(fake)
    assert await box.get_attribute("aria-expanded") == "false"
    await box.press_sequentially("hot :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    assert fake.of("complete") == [{"complete": "emoji", "q": "fi", "thread": T}]
    assert await box.get_attribute("role") == "combobox"
    assert await box.get_attribute("aria-expanded") == "true"
    assert await page.locator("#pop").get_attribute("role") == "listbox"
    assert await page.locator("#pop").get_attribute("aria-label") == "emoji"
    assert await box.get_attribute("aria-activedescendant") == "pop-0"
    await box.press("ArrowDown")
    assert await page.locator("#pop-1").get_attribute("aria-selected") == "true"
    await box.press("ArrowUp")
    await box.press("Enter")
    assert await box.input_value() == "hot 🔥"
    assert await closed(page)
    assert await box.get_attribute("aria-expanded") == "false"
    await fake.settled()
    assert fake.of("send") == []


async def test_at_lists_everyone_first_and_send_carries_mentions_in_full_format(fake: Fake) -> None:
    """012-AC8: `@` lists everyone first, Enter/Tab insert `@Full Name`; the send carries the picks and
    `format: full` (the chat view's set)."""
    page, box = _parts(fake)
    await box.press_sequentially("@")
    await shows(page, ["everyone", "Ann Lee", "Bob Roe"])
    assert await page.locator("#pop").get_attribute("aria-label") == "members"
    await box.press("ArrowDown")
    await box.press("Enter")
    assert await box.input_value() == "@Ann Lee "
    await box.press_sequentially("hi @ev")
    await shows(page, ["everyone"])
    await box.press("Tab")
    assert await box.input_value() == "@Ann Lee hi @everyone "
    assert await page.evaluate("document.activeElement.id") == "reply"
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [
        {"send": T, "text": "@Ann Lee hi @everyone ", "mentions": [ANN, EVERYONE], "format": "full"}
    ]


async def test_a_quote_and_a_mention_travel_together(fake: Fake) -> None:
    """012-AC8: a reply with a quote keeps its `quote` list next to the mentions."""
    page, box = _parts(fake)
    await page.locator('[data-id="1"] [data-act=reply]').dispatch_event("click")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [
        {"send": T, "text": "@Ann Lee ", "mentions": [ANN], "format": "full", "quote": ["1"]}
    ]


async def test_a_stale_answer_is_ignored(fake: Fake) -> None:
    """012-AC8: the answer to `fi` landing after `fis` was asked lists nothing; `fis`'s answer does."""
    page, box = _parts(fake)
    fake.hold = True
    await box.press_sequentially(":fi")
    await wait(lambda: len(fake.held) == 1)
    await box.press_sequentially("s")
    await wait(lambda: len(fake.held) == 2)
    fake.release(0)
    await fake.handled()
    assert await closed(page)
    fake.release(1)
    await shows(page, ["🐟:fish:"])


async def test_escape_dismisses_the_word_keeps_the_text_then_enter_sends(fake: Fake) -> None:
    """012-AC8: Escape closes the list only, the same word does not reopen it, Enter then sends."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Escape")
    assert await closed(page)
    assert await box.input_value() == ":fi"
    await box.press_sequentially("s")
    await fake.settled(past_ms=POP_ASK_MS)
    assert [v["q"] for v in fake.of("complete")] == ["fi"]
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [{"send": T, "text": ":fis", "mentions": [], "format": "full"}]


async def test_a_shorter_prefix_list_is_locked_until_answered_or_two_seconds(fake: Fake) -> None:
    """012-AC8: `:fi` listed, `s` asked: Enter inserts nothing and sends nothing; unanswered, the list
    goes after the wait and Enter sends again."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    fake.hold = True
    await box.press_sequentially("s")
    await wait(lambda: len(fake.held) == 1)
    await box.press("Enter")
    assert await box.input_value() == ":fis"
    await fake.settled()
    assert fake.of("send") == []
    await page.locator("#pop").wait_for(state="hidden", timeout=3000)
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == ":fis"


async def test_a_prefix_typed_offline_releases_enter_at_once(fake: Fake) -> None:
    """012-AC8: the socket drops with the list open; the next prefix cannot be asked, so Enter reaches
    the send path and says offline instead of waiting."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await fake.ws.close()
    await until(page, "window.__ws.readyState === 3")
    await box.press_sequentially("s")
    await page.locator("#pop").wait_for(state="hidden", timeout=500)  # before the 2 s release
    await box.press("Enter")
    assert await page.locator("#err").inner_text() == "offline, not sent"


async def test_only_plain_emoji_paths_load_an_image_and_names_are_text(fake: Fake) -> None:
    """012-AC8: images only from `/emoji/<id>` (no `..`, encoded dots, newline or nested path, no
    foreign host); a name carrying markup is text; a custom pick inserts `:name:`."""
    page, box = _parts(fake)
    await box.press_sequentially(":tr")
    await shows(page, [":trok:", ":tr..:", ":tr%2e%2e:", ":tra\nb:", ":tra/b:"])
    srcs = await page.locator("#pop img").evaluate_all("imgs => imgs.map(i => i.getAttribute('src'))")
    assert srcs == ["/emoji/0-ok"]
    await box.fill(":pa")
    await shows(page, [":partyparrot:", ":pacman:", "x:pa<img src=x onerror=alert(1)>:"])
    assert await page.locator("#pop img").evaluate_all("imgs => imgs.map(i => i.getAttribute('src'))") == [
        "/emoji/0-abc"
    ]
    await until(page, "document.querySelector('#pop img').naturalWidth > 0")  # loads under the CSP
    await box.press("Enter")
    assert await box.input_value() == ":partyparrot:"


async def test_malformed_items_are_skipped(fake: Fake) -> None:
    """012-AC8: null, nameless or non-object items are dropped; the rest still lists and picks."""
    page, box = _parts(fake)
    await box.press_sequentially(":nu")
    await shows(page, ["☢:nuke:"])
    await box.press("Enter")
    assert await box.input_value() == "☢"


async def test_mid_word_triggers_ask_nothing_and_an_inserted_mention_is_not_asked_again(fake: Fake) -> None:
    """012-AC8: `10:30` and `me@ann` ask nothing; `@ann l` asks with its inner space; once `@Ann Lee `
    or `@everyone ` is inserted, typing on asks nothing more."""
    page, box = _parts(fake)
    for part in ("at 10:30", " to me@ann"):
        await box.press_sequentially(part)
        await past(page, POP_ASK_MS)
    await fake.settled()
    assert fake.of("complete") == []
    await box.fill("")
    await box.press_sequentially("@ann l")
    await shows(page, ["Ann Lee"])
    assert fake.of("complete")[-1]["q"] == "ann l"
    await box.press("Enter")
    await box.press_sequentially("hi @every")
    await shows(page, ["everyone"])
    await box.press("Enter")
    asked = len(fake.of("complete"))
    await past(page, POP_ASK_MS)  # the insert itself asks nothing either
    await box.press_sequentially("thanks")  # `everyone thanks` would still fit the one-space pattern
    await fake.settled(past_ms=POP_ASK_MS)
    assert len(fake.of("complete")) == asked
    assert await box.input_value() == "@Ann Lee hi @everyone thanks"


async def test_an_emoji_word_after_a_member_word_asks_for_emoji(fake: Fake) -> None:
    """012-AC8: `@ann :fi` reads as the emoji word, not a two-word member query."""
    page, box = _parts(fake)
    await box.press_sequentially("@ann :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    assert fake.of("complete")[-1] == {"complete": "emoji", "q": "fi", "thread": T}


async def test_quick_typing_asks_once_and_a_typed_shortcode_goes_out_as_typed(fake: Fake) -> None:
    """012-AC8: keys under the debounce ask nothing until the typing pauses; `:fire:` typed by hand
    closes the list at its second colon and is sent as text."""
    page, box = _parts(fake)
    await box.press_sequentially("a :fire", delay=20)
    await shows(page, ["🔥:fire:"])
    assert [v["q"] for v in fake.of("complete")] == ["fire"]
    await box.press_sequentially(": b")
    assert await closed(page)
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "a :fire: b"


async def test_caret_leaving_the_word_lets_enter_send(fake: Fake) -> None:
    """012-AC8: `ab :fi` listed, Home moves the caret out of the word: Enter sends the text as is."""
    page, box = _parts(fake)
    await box.press_sequentially("ab :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Home")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "ab :fi"


async def test_click_picks_a_press_keeps_focus_and_blur_closes(fake: Fake) -> None:
    """012-AC8: a click on an item inserts it without blurring the composer; focus leaving closes."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await page.locator("#pop-1").click()
    assert await box.input_value() == "🐟"
    assert await page.evaluate("document.activeElement.id") == "reply"
    await box.press_sequentially(" :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.blur()
    assert await closed(page)


async def test_the_popup_opens_inside_the_window(fake: Fake) -> None:
    """012 Page bullet: the composer sits at the bottom, so the list opens above the caret line (its top
    lies within the textarea padding), on screen."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    pop, reply = await page.locator("#pop").bounding_box(), await box.bounding_box()
    assert pop is not None and reply is not None
    height = await page.evaluate("innerHeight")
    assert (
        pop["y"] >= 0
        and pop["y"] + pop["height"] <= reply["y"] + 12
        and reply["y"] + reply["height"] <= height
    )


# --- picked set per chat ---


async def test_picks_stay_with_their_chat_across_a_split_switch_and_clear_only_on_ok(pages: Pages) -> None:
    """012-AC8, 011-AC22: a pick made in T is not sent from L (nor its preview shown there); back in T it
    is; a failed send keeps it (↻ sends it again, 017-AC6), a successful one clears it."""
    fake = Fake()
    page = await _open(pages, fake, "?chatnav=split")
    box = page.locator("#reply")
    await page.wait_for_selector('#rows li[data-id="19:other@thread.v2"]')
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    assert await box.input_value() == ""
    assert await page.locator("#view").is_hidden()  # T's preview does not follow to L
    await box.fill("@Ann Lee by hand")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == T)
    assert await box.input_value() == "@Ann Lee "
    fake.fail = True
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 2)
    await queue_is(page, ("@Ann Lee ", "failed: boom"))
    fake.fail = False
    await act(page, "retry")
    await wait(lambda: len(fake.of("send")) == 3)
    await until(page, "document.getElementById('reply').value === ''")
    await box.fill("@Ann Lee again")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 4)
    assert [(v["send"], v["mentions"]) for v in fake.of("send")] == [(L, []), (T, [ANN]), (T, [ANN]), (T, [])]
    await page.context.close()
    assert pages.external == []


async def test_picks_survive_the_reload_a_newer_page_triggers(pages: Pages) -> None:
    """012-AC8: like the draft, the picks of an unsent message survive the version reload."""
    fake = Fake()
    page = await _open(pages, fake)
    box = page.locator("#reply")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await page.evaluate("window.__same = 1")
    fake.version = "2"
    async with page.expect_event("load", timeout=10000):
        await fake.ws.close()
    assert await page.evaluate("window.__same") is None
    await page.wait_for_selector('[data-id="1"]')
    assert await box.input_value() == "@Ann Lee "
    await box.focus()
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [ANN]
    await page.context.close()
    assert pages.external == []


@pytest.mark.parametrize("third_short", [False, True])
async def test_shortened_mentions_keep_distinct_recipients_and_survive_reload(
    pages: Pages, third_short: bool
) -> None:
    other = {"name": "Ann Roe", "mri": "8:orgid:other-ann"}
    fake = Fake(members=[ANN, other])
    page = await _open(pages, fake)
    box = page.locator("#reply")
    for name in ("@ann l", "and @ann r"):
        await box.press_sequentially(name)
        await shows(page, [ANN["name"] if name == "@ann l" else other["name"]])
        await box.press("Enter")
        await box.press("Backspace")
    assert await box.input_value() == "@Ann and @Ann "
    await box.press_sequentially("and @ann l")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    if third_short:
        await box.press("Backspace")
    expected = "@Ann and @Ann and " + ("@Ann " if third_short else "@Ann Lee ")
    assert await box.input_value() == expected
    fake.version = "2"
    async with page.expect_event("load", timeout=10000):
        await fake.ws.close()
    await box.wait_for()
    assert await box.input_value() == expected
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [
        {**ANN, "name": "Ann"},
        {**other, "name": "Ann"},
        {**ANN, "name": "Ann"} if third_short else ANN,
    ]
    verb = fake.of("send")[0]
    _, props = compose_html(verb["text"], verb["mentions"], {})
    assert props is not None
    assert [m["mri"] for m in json.loads(props["mentions"])] == [ANN["mri"], other["mri"], ANN["mri"]]


async def test_shortening_an_earlier_mention_keeps_first_names_in_text_order(fake: Fake) -> None:
    other = {"name": "Ann Roe", "mri": "8:orgid:other-ann"}
    fake.members = [ANN, other]
    page, box = _parts(fake)
    for name in ("@ann l", "and @ann r"):
        await box.press_sequentially(name)
        await shows(page, [ANN["name"] if name == "@ann l" else other["name"]])
        await box.press("Enter")
    await box.press("Backspace")
    await box.evaluate("b => b.setSelectionRange(9, 9)")
    await box.press("Backspace")
    assert await box.input_value() == "@Ann and @Ann "
    await box.press("End")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [{**ANN, "name": "Ann"}, {**other, "name": "Ann"}]


@pytest.mark.parametrize("which", [0, 1])
async def test_shortening_identical_full_names_keeps_the_selected_person(fake: Fake, which: int) -> None:
    fake.members = [ANN, ANN2]
    page, box = _parts(fake)
    for i, prefix in enumerate(("@ann", "and @ann")):
        await box.press_sequentially(prefix)
        await shows(page, ["Ann Lee", "Ann Lee"])
        if i:
            await box.press("ArrowDown")
        await box.press("Enter")
    caret = 9 if which == 0 else len("@Ann Lee and @Ann Lee ")
    await box.evaluate("(b, caret) => b.setSelectionRange(caret, caret)", caret)
    await box.press("Backspace")
    expected = "@Ann and @Ann Lee " if which == 0 else "@Ann Lee and @Ann "
    assert await box.input_value() == expected
    await box.press("End")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    verb = fake.of("send")[0]
    _, props = compose_html(verb["text"], verb["mentions"], {})
    assert props is not None
    assert [m["mri"] for m in json.loads(props["mentions"])] == [ANN["mri"], ANN2["mri"]]


async def test_a_sent_shortened_mention_lets_the_next_message_ask_again(fake: Fake) -> None:
    """M1: a send empties the composer without an input event: `@` at the shortened mention's offset
    must ask again for the next message."""
    page, box = _parts(fake)
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await box.press("Backspace")
    assert await box.input_value() == "@Ann "
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    await until(page, "document.getElementById('reply').value === ''")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])


async def test_backspace_inside_a_longer_picked_name_is_plain_deletion(fake: Fake) -> None:
    """`@Ann Lee` inside a picked `@Ann Lee Smith` is no mention of Ann Lee: Backspace there deletes a
    letter and leaves both picks."""
    long = {"name": "Ann Lee Smith", "mri": "8:orgid:ann-lee-smith"}
    fake.members = [ANN, long]
    page, box = _parts(fake)
    await box.press_sequentially("@ann lee")
    await shows(page, ["Ann Lee", "Ann Lee Smith"])
    await box.press("ArrowDown")
    await box.press("Enter")
    await box.press_sequentially("and @ann l")
    await shows(page, ["Ann Lee", "Ann Lee Smith"])
    await box.press("Enter")
    assert await box.input_value() == "@Ann Lee Smith and @Ann Lee "
    await box.evaluate("b => b.setSelectionRange(8, 8)")
    await box.press("Backspace")
    assert await box.input_value() == "@Ann Le Smith and @Ann Lee "


@pytest.mark.parametrize("glue", ["x", "𝐀"])
async def test_backspace_inside_a_glued_name_is_plain_deletion(fake: Fake, glue: str) -> None:
    """L4: `@Ann Leex` is no mention, nor `@Ann Lee𝐀` (a letter outside the BMP): Backspace after `Lee`
    deletes a letter; the real `@Ann Lee` after it keeps its full name."""
    page, box = _parts(fake)
    for prefix in ("@ann", "@ann"):
        await box.press_sequentially(prefix)
        await shows(page, ["Ann Lee"])
        await box.press("Enter")
    await box.evaluate("b => b.setSelectionRange(8, 8)")
    await box.press_sequentially(glue)
    await box.press("Escape")
    await box.evaluate("b => b.setSelectionRange(8, 8)")
    await box.press("Backspace")
    assert await box.input_value() == f"@Ann Le{glue} @Ann Lee "


async def test_reaction_hover_lists_every_emotion_and_its_people(fake: Fake) -> None:
    page, _ = _parts(fake)
    reactions = [
        {"key": "launch", "emoji": "🚀", "count": 1, "mine": False, "people": ["Ann Lee"]},
        {"key": "crossedfingers", "emoji": "🤞", "count": 2, "mine": False, "people": ["Bob Roe", "Me"]},
        {"key": "yes-tone1", "emoji": "👍🏻", "count": 1, "mine": False, "people": ["Ann Lee"]},
    ]
    fake.send({"edit": {**MINE, "reactions": reactions}, "thread": T})
    await fake.handled()
    chips = page.locator('[data-id="1"] .reacts > *')
    assert await chips.count() == 3
    for chip in await chips.all():
        await chip.hover()
        assert await chip.get_attribute("title") == "🚀 — Ann Lee\n🤞 — Bob Roe, Me\n👍🏻 — Ann Lee"
    assert await chips.nth(2).inner_text() == "👍🏻 1"
    fake.send({"edit": {**MINE, "reactions": reactions[:1]}, "thread": T})
    await fake.handled()
    assert await chips.first.get_attribute("title") == "🚀 — Ann Lee"


async def test_custom_reaction_shows_its_image_or_its_name(fake: Fake) -> None:
    page, _ = _parts(fake)
    reactions = [
        {"key": "kekwait;0-a", "emoji": ":kekwait:", "src": "/emoji/0-a", "count": 1, "mine": False},
        {"key": "partyparrot;0-b", "emoji": ":partyparrot:", "count": 2, "mine": False},
    ]
    fake.send({"edit": {**MINE, "reactions": reactions}, "thread": T})
    await fake.handled()
    chips = page.locator('[data-id="1"] .reacts > *')
    assert await chips.nth(0).locator("img.emoji").get_attribute("src") == "/emoji/0-a"
    assert (await chips.nth(0).inner_text()).strip() == "1"
    assert await chips.nth(1).locator(".custom-emoji").inner_text() == "partyparrot"
    assert ":" not in await chips.nth(1).inner_text()


async def test_repick_after_reload_keeps_both_short_and_full_mentions(pages: Pages) -> None:
    fake = Fake()
    page = await _open(pages, fake)
    box = page.locator("#reply")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await box.press("Backspace")
    fake.version = "2"
    async with page.expect_event("load", timeout=10000):
        await fake.ws.close()
    await box.wait_for()
    await box.press_sequentially("and @ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    assert await box.input_value() == "@Ann and @Ann Lee "
    # A second reload must also preserve both labels for the same person.
    fake.version = "3"
    async with page.expect_event("load", timeout=10000):
        await fake.ws.close()
    await box.wait_for()
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [{**ANN, "name": "Ann"}, ANN]


async def test_invalid_saved_picks_are_left_out(pages: Pages) -> None:
    """012-AC8: a saved draft whose picks were tampered with sends only the well-formed ones, two
    people sharing a name included."""
    fake = Fake()
    page = await _open(pages, fake)
    bad = [None, {"name": "Bob Roe", "mri": 5}, {"name": "everyone", "mri": T, "everyone": "yes"}, ANN, ANN2]
    draft = {"text": "@Ann Lee @Ann Lee @Bob Roe @everyone", "quotes": [], "picked": bad}
    await page.evaluate(
        f"sessionStorage.setItem({json.dumps('draft:' + T)}, {json.dumps(json.dumps(draft))})"
    )
    await page.reload()
    await page.wait_for_selector('[data-id="1"]')
    box = page.locator("#reply")
    assert await box.input_value() == draft["text"]
    await box.focus()
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [ANN, ANN2]
    await page.context.close()
    assert pages.external == []


async def test_an_answer_for_the_chat_left_opens_nothing(pages: Pages) -> None:
    """012-AC8, 011-AC22: `:fi` asked in T, Back to L before the answer: the late answer lists nothing."""
    fake = Fake()
    page = await _open(pages, fake, "?chatnav=split")
    box = page.locator("#reply")
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == T)
    await box.focus()
    fake.hold = True
    await box.press_sequentially(":fi")
    await wait(lambda: len(fake.held) == 1)
    await page.go_back()  # a switch that leaves the focus in the composer
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    fake.release(0)
    await fake.handled()
    assert await closed(page)
    await page.context.close()
    assert pages.external == []


# --- toolbar (012-AC14) ---


async def _select(page: Page, text: str, a: int, b: int) -> None:
    await page.locator("#reply").fill(text)
    await page.evaluate(
        f"(r => {{ r.focus(); r.setSelectionRange({a}, {b}); }})(document.getElementById('reply'))"
    )


async def _selection(page: Page) -> list[Any]:
    return list(
        await page.evaluate(
            "(r => [r.value, r.selectionStart, r.selectionEnd])(document.getElementById('reply'))"
        )
    )


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("bold", "say **hi** now"),
        ("italic", "say *hi* now"),
        ("strike", "say ~~hi~~ now"),
        ("code", "say `hi` now"),
        ("list", "- say hi now"),
        ("quote", "> say hi now"),
    ],
)
async def test_each_toolbar_button_wraps_the_selection(fake: Fake, fmt: str, expected: str) -> None:
    """012-AC14: each button wraps the selected word in its markers (lines: prefixes them), keeps focus,
    sends nothing."""
    page, box = _parts(fake)
    await _select(page, "say hi now", 4, 6)
    await page.locator(f'#tools [data-fmt="{fmt}"]').click()
    assert await box.input_value() == expected
    assert await page.evaluate("document.activeElement.id") == "reply"
    if fmt not in ("list", "quote"):  # the word stays selected inside its markers
        value, a, b = await _selection(page)
        assert value[a:b] == "hi"
    await fake.settled()
    assert fake.of("send") == []


async def test_toolbar_buttons_are_labelled(fake: Fake) -> None:
    """012-AC14: every toolbar button has an accessible name."""
    page, _ = _parts(fake)
    labels = await page.locator("#tools button").evaluate_all(
        "bs => bs.map(b => b.getAttribute('aria-label'))"
    )
    assert labels == ["bold", "italic", "strikethrough", "code", "list", "quote", "preview"]


async def test_ctrl_b_and_ctrl_i_wrap_and_an_empty_selection_types_inside(fake: Fake) -> None:
    """012-AC14: Ctrl+B / Ctrl+I wrap the selection; with no selection the markers go around the caret
    and typing lands between them."""
    page, box = _parts(fake)
    await _select(page, "a b", 0, 1)
    await box.press("Control+b")
    assert await box.input_value() == "**a** b"
    await page.evaluate(
        "(r => r.setSelectionRange(r.value.length, r.value.length))(document.getElementById('reply'))"
    )
    await box.press_sequentially(" ")
    await box.press("Control+i")
    await box.press_sequentially("c")
    assert await box.input_value() == "**a** b *c*"
    await fake.settled()
    assert fake.of("send") == []


async def test_list_and_quote_prefix_every_selected_line(fake: Fake) -> None:
    """012-AC14: a selection over lines prefixes each, from the start of the first line; a trailing
    newline does not pull in the next line."""
    page, box = _parts(fake)
    await _select(page, "intro\none\ntwo\nend", 8, 14)  # from inside `one` through `two\n`
    await page.locator('#tools [data-fmt="list"]').click()
    assert await box.input_value() == "intro\n- one\n- two\nend"
    await _select(page, "one\ntwo", 0, 7)
    await page.locator('#tools [data-fmt="quote"]').click()
    assert await box.input_value() == "> one\n> two"


async def test_code_on_a_multi_line_selection_makes_a_fence(fake: Fake) -> None:
    """012-AC14: `</>` on lines writes a ``` fence, each marker on a line of its own."""
    page, box = _parts(fake)
    await _select(page, "see a\nb done", 4, 7)
    await page.locator('#tools [data-fmt="code"]').click()
    assert await box.input_value() == "see \n```\na\nb\n```\n done"


# --- preview (012-AC14, AC11/AC12 shown through the server's HTML) ---


async def test_preview_shows_the_server_html_without_sending(fake: Fake) -> None:
    """012-AC14: the toggle asks `{preview, format: full}`, shows the answer as HTML above the composer,
    sends nothing; toggled again it hides."""
    page, box = _parts(fake)
    await box.fill("**hi**")
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    assert await page.locator("#view b").inner_text() == "hi"
    assert await page.locator("#show").get_attribute("aria-pressed") == "true"
    assert fake.of("preview") == [{"preview": "**hi**", "format": "full"}]
    view, reply = await page.locator("#view").bounding_box(), await box.bounding_box()
    assert view is not None and reply is not None and view["y"] < reply["y"]
    await fake.settled()
    assert fake.of("send") == []
    await page.locator("#show").click()
    assert await page.locator("#view").is_hidden()
    assert await page.locator("#show").get_attribute("aria-pressed") == "false"


async def test_preview_hides_when_the_text_changes_and_on_send(fake: Fake) -> None:
    """012-AC14: the shown preview goes as soon as the text changes, and when the message is sent."""
    page, box = _parts(fake)
    await box.fill("a")
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await box.press_sequentially("b")
    assert await page.locator("#view").is_hidden()
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await page.locator('#tools [data-fmt="bold"]').click()  # a toolbar edit is a change too
    assert await page.locator("#view").is_hidden()
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await box.press("Enter")
    assert await page.locator("#view").is_hidden()
    await wait(lambda: len(fake.of("send")) == 1)


async def test_a_stale_preview_is_dropped(fake: Fake) -> None:
    """012-AC14: an answer to an ask made before the text changed, or landing after the toggle went
    off, shows nothing; the answer to the latest ask does."""
    page, box = _parts(fake)
    fake.hold = True
    await box.fill("one")
    await page.locator("#show").click()
    await wait(lambda: len(fake.held) == 1)
    await box.press_sequentially("two")  # hides; asked again below, the first answer still on its way
    await page.locator("#show").click()
    await wait(lambda: len(fake.held) == 2)
    fake.release(0)
    await fake.handled()
    assert await page.locator("#view").is_hidden()
    fake.release(1)
    await page.locator("#view .pv").wait_for()
    assert await page.locator("#view").inner_text() == "onetwo"
    await page.locator("#show").click()  # off before its answer: nothing shows
    await page.locator("#show").click()
    await wait(lambda: len(fake.held) == 3)
    await page.locator("#show").click()
    fake.release(2)
    await fake.handled()
    assert await page.locator("#view").is_hidden()


async def test_preview_after_reconnect_shows_again(fake: Fake) -> None:
    """012-AC14: an ask whose answer died with the socket releases the button and does not hold back
    the next preview."""
    page, box = _parts(fake)
    fake.hold = True
    await box.fill("a")
    await page.locator("#show").click()
    await wait(lambda: len(fake.held) == 1)
    await fake.ws.close()
    await until(page, "document.getElementById('show').getAttribute('aria-pressed') === 'false'")
    await wait(lambda: len(fake.of("follow")) == 2)  # reconnected
    fake.hold = False
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()


async def test_preview_links_open_beside_the_chat(fake: Fake) -> None:
    """012-AC14: a link in the preview opens a new tab, never in place of the chat and its draft."""
    page, box = _parts(fake)
    await box.fill("[x](https://example.invalid/)")
    await page.locator("#show").click()
    await page.locator("#view a").wait_for()
    link = page.locator("#view a")
    assert (await link.get_attribute("target"), await link.get_attribute("rel")) == ("_blank", "noopener")


async def test_bold_on_lines_wraps_each_line_inside_its_list_prefix(fake: Fake) -> None:
    """012-AC14, 012-AC12: B over two lines gives two bold lines; over list items each stays an item."""
    page, box = _parts(fake)
    await _select(page, "a\nb", 0, 3)
    await page.locator('#tools [data-fmt="bold"]').click()
    assert await box.input_value() == "**a**\n**b**"
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    assert await page.locator("#view b").all_text_contents() == ["a", "b"]
    await page.locator("#show").click()
    await _select(page, "- one\n- two", 0, 11)
    await page.locator('#tools [data-fmt="bold"]').click()
    assert await box.input_value() == "- **one**\n- **two**"
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    assert await page.locator("#view li b").all_text_contents() == ["one", "two"]


async def test_spaces_at_the_selection_ends_stay_outside_the_markers(fake: Fake) -> None:
    """012-AC14: `a ` selected gives `**a** `, a marker the server can close."""
    page, box = _parts(fake)
    await _select(page, " a b", 0, 3)
    await page.locator('#tools [data-fmt="bold"]').click()
    assert await box.input_value() == " **a** b"


async def test_ctrl_b_during_an_ime_composition_does_nothing(fake: Fake) -> None:
    """012-AC14: a Ctrl+B the IME still owns formats nothing."""
    page, box = _parts(fake)
    await _select(page, "ab", 0, 2)
    ev = "{key: 'b', ctrlKey: true, keyCode: 229, isComposing: true, bubbles: true, cancelable: true}"
    await box.evaluate(f"b => b.dispatchEvent(new KeyboardEvent('keydown', {ev}))")
    assert await box.input_value() == "ab"


async def test_ctrl_z_undoes_a_toolbar_edit_and_a_pick(fake: Fake) -> None:
    """012-AC14, 012-AC8: the toolbar and the list edit through the undo history."""
    page, box = _parts(fake)
    await _select(page, "say hi", 4, 6)
    await page.locator('#tools [data-fmt="bold"]').click()
    assert await box.input_value() == "say **hi**"
    await box.press("Control+z")
    assert await box.input_value() == "say hi"
    await box.fill("")
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Enter")
    assert await box.input_value() == "🔥"
    await box.press("Control+z")
    assert await box.input_value() == ":fi"


async def test_bold_with_the_list_open_closes_it(fake: Fake) -> None:
    """012-AC8, 012-AC14: B clicked or Ctrl+B pressed while the list is open closes the list."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await page.locator('#tools [data-fmt="bold"]').click()
    assert await closed(page)
    await box.fill(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Control+b")
    assert await closed(page)
    await fake.settled(past_ms=POP_ASK_MS)  # nothing reopens it: no ask, so no answer either
    await fake.handled()
    assert await closed(page)


async def test_a_preview_error_is_text(fake: Fake) -> None:
    """012-AC14: the server's error shows in the box as text, never as markup."""
    page, box = _parts(fake)
    await box.fill("bad")
    await page.locator("#show").click()
    await page.locator("#view.err").wait_for()
    assert await page.locator("#view").inner_text() == "<b>nope</b>"
    assert await page.locator("#view b").count() == 0


async def test_a_read_only_channel_has_no_toolbar(pages: Pages) -> None:
    """012-AC14, 011 Composer bullet: a team channel page shows no toolbar and no preview toggle."""
    fake = Fake(read_only=True)
    page = await _open(pages, fake)
    await until(page, "document.body.classList.contains('readonly')")
    assert await page.locator("#tools").is_hidden()
    assert await page.locator("#show").is_hidden()
    await page.context.close()
    assert pages.external == []


# --- review fixes: quote offer, IME, busy cap, partial updates, direction, same-name mentions ---


async def test_reply_needs_a_sender(pages: Pages) -> None:
    """011 Message actions: ↩ only where `send` takes a quote; a text message without a sender has none."""
    anon = {"id": "2", "at": "2026-10-09T10:01:00.000Z", "from": "", "name": "", "me": False, "type": "text"}
    fake = Fake(
        history=[
            {**MINE, "html": "<p>mine</p>", "editable": True},
            {**anon, "html": "<p>x</p>", "editable": True},
        ]
    )
    page = await _open(pages, fake, wait_for='[data-id="2"]')
    assert await page.locator('[data-id="1"] [data-act=reply]').count() == 1
    assert await page.locator('[data-id="2"] [data-act=reply]').count() == 0


@pytest.mark.parametrize(
    "event",
    ["{key: 'Enter', isComposing: true}", "{key: 'Enter', keyCode: 229}"],
    ids=["isComposing", "keyCode-229"],
)
async def test_enter_during_an_ime_composition_neither_sends_nor_saves(fake: Fake, event: str) -> None:
    """011 Composer bullet (spec 006 keys): an Enter the IME still owns confirms the composition only,
    in the composer and in the edit-in-place editor; a plain Enter then sends and saves."""
    page, box = _parts(fake)
    press = (
        f"b => b.dispatchEvent(new KeyboardEvent('keydown', {{...{event}, bubbles: true, cancelable: true}}))"
    )
    await box.fill("にほん")
    await box.evaluate(press)
    await page.locator('[data-id="1"] [data-act=edit]').dispatch_event("click")
    editor = page.locator(".editor")
    await editor.fill("にほんご")
    await editor.evaluate(press)
    await fake.settled()
    assert fake.of("send") == [] and fake.of("edit") == []
    assert await box.input_value() == "にほん"
    await editor.press("Enter")
    await box.press("Enter")
    await fake.settled()
    assert [v["text"] for v in fake.of("edit")] == ["にほんご"]
    assert [v["text"] for v in fake.of("send")] == ["にほん"]


async def test_busy_answers_are_retried_ten_times_then_say_unavailable(pages: Pages) -> None:
    """011 History bullet: `busy` is asked again, at most ten times, then the page says the history
    (or the search) is unavailable and asks nothing more."""
    fake = Fake(busy=True)
    page = await _open(pages, fake, wait_for="")
    await page.locator("#find").fill("x")
    await page.locator("#find").press("Enter")
    await until(page, "document.getElementById('note').textContent === 'history: history unavailable'", 1000)
    await until(page, "document.querySelector('#hits .err')?.textContent === 'search unavailable'", 1000)
    await fake.settled(past_ms=1000)  # past two retry delays: an eleventh retry would be in
    assert (len(fake.of("history")), len(fake.of("search"))) == (11, 11)


async def test_a_reaction_update_keeps_the_rest_of_the_message(pages: Pages) -> None:
    """011-AC7: an update without a body (a reaction) changes the reactions and nothing else: the
    name, the edited marker and the edit action stay."""
    fake = Fake(history=[{**MINE, "html": "<p>mine</p>", "editable": True, "edited": True, "type": "text"}])
    page = await _open(pages, fake)
    like = {"key": "like", "emoji": "👍", "count": 1, "mine": False}
    blank = {
        "name": "",
        "type": "other",
        "editable": False,
        "edited": False,
        "deleted": False,
        "mentions": None,
    }
    fake.send({"edit": {**MINE, **blank, "reactions": [like]}, "thread": T})
    await fake.handled()
    msg = page.locator('[data-id="1"]')
    assert await msg.locator(".reacts").inner_text() == "👍 1"
    assert await msg.locator(".head b").inner_text() == "Me"
    assert await msg.locator(".edited").count() == 1
    assert await msg.locator("[data-act=edit]").count() == 1
    assert await msg.locator(".html").inner_text() == "mine"


async def test_names_and_bodies_take_their_own_direction(pages: Pages) -> None:
    """A right-to-left message reads right to left: bodies, sender names, row names and snippets
    carry dir=auto."""
    rtl = {"id": "2", "at": "2026-10-09T10:01:00.000Z", "from": "8:orgid:x", "name": "שרה לוי", "me": False}
    fake = Fake(history=[{**rtl, "type": "text", "html": "<p>שלום לכולם</p>", "editable": True}])
    page = await _open(pages, fake, "?chatnav=split", wait_for='[data-id="2"]')
    await page.wait_for_selector("#rows li")
    dirs = await page.evaluate(
        """[...['[data-id="2"] .html', '[data-id="2"] .head b', '#rows .name', '#rows .prev']]
        .map((s) => document.querySelector(s).getAttribute('dir'))"""
    )
    assert dirs == ["auto"] * 4


async def test_two_people_with_one_name_are_both_mentioned(fake: Fake) -> None:
    """012-AC8: two members sharing a display name are two picks; the send carries both mentions."""
    fake.members = [ANN, ANN2]
    page, box = _parts(fake)
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("Enter")
    await box.press_sequentially("and @ann")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("ArrowDown")
    await box.press("Enter")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [ANN, ANN2]


@pytest.mark.parametrize(
    ("picks", "sent"), [([0, 1], [ANN2]), ([0, 1, 0], [ANN])], ids=["other", "re-picked"]
)
async def test_a_deleted_pick_does_not_mention_its_namesake(fake: Fake, picks: list[int], sent: Any) -> None:
    """M1: a namesake picked then deleted from the text is not mentioned; the one picked last is."""
    fake.members = [ANN, ANN2]
    assert await pick_namesakes(fake, picks) == sent


async def test_a_deleted_pick_stays_out_when_its_name_is_typed_again(fake: Fake) -> None:
    """L8: each pick prunes the picks gone from the text: a later hand-typed `@Ann Lee` must not revive
    the namesake deleted before the last pick."""
    fake.members = [ANN, ANN2]
    page, box = _parts(fake)
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("Enter")
    await box.press("Control+a")
    await box.press("Backspace")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("ArrowDown")
    await box.press("Enter")
    await box.press_sequentially("and @Ann Lee")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("Escape")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [ANN2]


async def test_picks_out_of_text_order_mention_both(fake: Fake) -> None:
    """M1: a mention inserted before an earlier pick still counts, and so does the earlier one."""
    assert await pick_out_of_order(fake) == [BOB, ANN]


async def test_a_longer_word_is_no_mention(fake: Fake) -> None:
    """L4: `@Ann Leeds` is no mention of Ann Lee, as the server reads it (a raw substring would count it)."""
    assert await pick_then_glue(fake) == []


# Delays of 1 s and more (the reconnect backoff) are recorded and run at once: the test reads the
# backoff without waiting it out.
DELAYS = """(() => { const real = window.setTimeout; window.__delays = [];
  window.setTimeout = (f, ms, ...a) => {
    if (ms >= 1000) { window.__delays.push(ms); ms = 0; }
    return real(f, ms, ...a);
  };
})();"""


async def test_a_follow_that_keeps_failing_keeps_backing_off(pages: Pages) -> None:
    """L2: the backoff resets on a followed chat, not on an open socket: a follow refused every time
    must not redial once a second."""
    fake = Fake(follow_error="unknown chat")
    page = await _open(pages, fake, wait_for="", init=DELAYS)
    for n in (1, 2, 3, 4):
        await wait(lambda n=n: len(fake.of("follow")) == n, f"follow {n}")
        await fake.handled()
        if n == 3:
            fake.follow_error = ""  # the 4th follow succeeds: the next drop starts over
        await fake.ws.close()
    await wait(lambda: len(fake.of("follow")) == 5, "follow 5")
    assert await page.evaluate("window.__delays") == [1000, 2000, 4000, 1000]
    await pages.close()


async def test_a_gap_frame_reloads_the_latest_page_and_keeps_the_draft(fake: Fake) -> None:
    """Server dropped relayed frames (`gap`): the page asks the latest page again and shows what it
    missed, the draft untouched; a gap for another chat asks nothing."""
    page, box = _parts(fake)
    await box.fill("draft")
    asked = len(fake.of("history"))
    fake.send({"gap": L})
    await fake.handled()
    await fake.settled()
    assert len(fake.of("history")) == asked
    fake.history = [
        *fake.history,
        {**MINE, "id": "2", "at": "2026-10-09T10:01:00.000Z", "html": "<p>missed</p>"},
    ]
    fake.send({"gap": T})
    await page.wait_for_selector('[data-id="2"]')
    assert fake.of("history")[asked:] == [{"history": T, "before": None}]
    assert await box.input_value() == "draft"


GAP = "2026-10-01T00:00:00.000Z"  # where the server stopped reading past unreadable rows
OLD = {
    "id": "0",
    "at": "2026-09-30T10:00:00.000Z",
    "from": ANN["mri"],
    "name": "Ann Lee",
    "html": "<p>old</p>",
}


@dataclass
class Gapped(Fake):
    """An archive whose page before the latest one is all unreadable rows: empty, with a `cursor`."""

    def on_verb(self, msg: dict[str, Any]) -> None:
        before = msg.get("before")
        if "history" not in msg or "after" in msg or "around" in msg:
            super().on_verb(msg)
        elif not before:
            self.send(
                {"history": T, "before": None, "messages": self.history, "older": True, "source": "archive"}
            )
        elif before == MINE["at"]:
            head = {"history": T, "before": before, "older": True, "source": "archive"}
            self.send({**head, "messages": [], "cursor": GAP})
        else:
            self.send(
                {"history": T, "before": before, "messages": [OLD], "older": False, "source": "archive"}
            )


async def test_an_empty_page_with_a_cursor_asks_on_from_the_cursor(pages: Pages) -> None:
    """L4: past unreadable rows the server answers an empty page and a `cursor`: the next page is
    asked from that cursor, not from the edge message again."""
    fake = Gapped()
    page = await _open(pages, fake)
    await page.wait_for_selector('[data-id="0"]')
    assert [v["before"] for v in fake.of("history")] == [None, MINE["at"], GAP]
    await pages.close()
