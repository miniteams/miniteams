"""Widget composer `:` / `@` completion (spec 012 phase 2) against a fake board speaking the 012 wire
contract (`complete`, `send` with `mentions` + `format`). Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_compose.py`.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import browser_helpers
import pytest
from browser_helpers import (  # skips without playwright
    ANN,
    ANN2,
    BOB,
    CONTRAST,
    EMOJIS,
    EVERYONE,
    MALFORMED,
    ORIGIN,
    POP_ASK_MS,
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
    rendered,
    row,
    server_reads,
    shows,
    wait,
)

from miniteams import web as W

pytestmark = pytest.mark.browser
pages = browser_helpers.pages

# A module global read by the fixture: a mutation run swaps it for an altered copy.
PAGE = W._page("widget.html")  # as served: ui.css and ui.js inlined


@dataclass
class Fake(browser_helpers.Fake):
    """The board side of the widget socket: answers `complete` from the tables above, acks `send`."""

    fail: bool = False
    label: str = "Fictional chat"
    members: list[dict[str, Any]] = field(default_factory=lambda: [EVERYONE, ANN, BOB])

    def attach(self, ws: Any) -> None:
        super().attach(ws)
        self.send({"rows": [row(T, self.label)], "page": "1", "opener": False, "browser": False, "me": "Me"})

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "complete" in msg:
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
        elif "send" in msg:
            self.send({"sent": msg["send"], "ok": not self.fail, "error": "boom" if self.fail else None})


async def _open(pages: Pages, board: Fake) -> None:
    page = await open_fake(
        pages,
        board,
        ORIGIN + "/",
        PAGE,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
    )
    await page.wait_for_selector("a.name")
    await page.locator("button.reply").dispatch_event("click")
    await page.wait_for_function("document.activeElement.id === 'reply'")


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    board = Fake()
    await _open(pages, board)
    yield board
    await pages.close()
    assert pages.external == []


async def test_colon_opens_the_emoji_list_and_enter_inserts_without_sending(fake: Fake) -> None:
    """012-AC8: `:fi` asks once the typing pauses, lists the answer; Enter inserts 🔥 and sends nothing."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("hot :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    assert fake.of("complete") == [{"complete": "emoji", "q": "fi", "thread": T}]
    assert await page.locator("#pop").get_attribute("role") == "listbox"
    assert await box.get_attribute("aria-activedescendant") == "pop-0"
    await box.press("ArrowDown")
    assert await page.locator("#pop-1").get_attribute("aria-selected") == "true"
    await box.press("ArrowUp")
    await box.press("Enter")
    assert await box.input_value() == "hot 🔥"
    assert await closed(page)
    await fake.settled()
    assert fake.of("send") == []


async def test_at_lists_members_everyone_first_and_send_carries_the_mentions(fake: Fake) -> None:
    """012-AC8: `@` lists everyone first, Enter inserts `@Full Name` and records the pick; Tab picks
    too; the send carries the picked mentions and `format: inline`."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("@")
    await shows(page, ["everyone", "Ann Lee", "Bob Roe"])
    assert fake.of("complete")[-1] == {"complete": "member", "q": "", "thread": T}
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
        {
            "send": T,
            "text": "@Ann Lee hi @everyone ",
            "mentions": [{**ANN, "at": 0}, {**EVERYONE, "at": 12}],
            "format": "inline",
        }
    ]


@pytest.mark.parametrize("trailing_space", [True, False])
async def test_backspace_shortens_a_picked_mention_without_losing_the_recipient(
    fake: Fake, trailing_space: bool
) -> None:
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    if not trailing_space:
        await box.press("ArrowLeft")
        await box.press("Delete")
    await box.press("Backspace")
    assert await box.input_value() == ("@Ann " if trailing_space else "@Ann")
    await box.press_sequentially("hi" if trailing_space else " hi")
    await box.press("Backspace")
    assert await box.input_value() == "@Ann h"
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [{**ANN, "at": 0}]  # who was picked, at its `@` (spec 032)
    assert server_reads(fake.of("send")[0], fake.members) == [ANN["mri"]]


@pytest.mark.parametrize("member", [EVERYONE, {"name": "Cher", "mri": "8:orgid:cher"}])
async def test_backspace_leaves_everyone_and_single_word_mentions_as_normal_text(
    fake: Fake, member: Any
) -> None:
    fake.members = [member]
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("@" + member["name"][:2].lower())
    await shows(page, [member["name"]])
    await box.press("Enter")
    await box.press("Backspace")
    assert await box.input_value() == "@" + member["name"]
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [{**member, "at": 0}]


async def test_a_removed_mention_is_not_sent_and_picks_survive_a_failure_only(fake: Fake) -> None:
    """012-AC8, 017-AC6: a pick whose `@Name` left the text is not sent; a failed send keeps its picks
    (↻ sends them again), a sent one clears them."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    for prefix in ("ann", "bob"):
        await box.press_sequentially("@" + prefix)
        await shows(page, [{"ann": "Ann Lee", "bob": "Bob Roe"}[prefix]])
        await box.press("Enter")
    assert await box.input_value() == "@Ann Lee @Bob Roe "
    await box.evaluate("b => b.setSelectionRange(9, 18)")
    await box.press("Delete")
    await box.press_sequentially("only")
    assert await box.input_value() == "@Ann Lee only"
    fake.fail = True
    await box.press("Enter")
    await queue_is(page, ("@Ann Lee only", "failed: boom"))
    fake.fail = False
    await act(page, "retry")
    await wait(lambda: len(fake.of("send")) == 2)
    await box.fill("@Ann Lee again")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 3)
    assert [v["mentions"] for v in fake.of("send")] == [[{**ANN, "at": 0}], [{**ANN, "at": 0}], []]


async def test_escape_closes_the_list_keeps_the_text_then_enter_sends(fake: Fake) -> None:
    """012-AC8: Escape closes the list only (text and composer stay), the same word does not reopen it,
    and Enter with the list closed sends."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
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
    assert fake.of("send") == [{"send": T, "text": ":fis", "mentions": [], "format": "inline"}]


async def test_a_typed_shortcode_is_sent_as_typed(fake: Fake) -> None:
    """012-AC8: `:fire:` typed by hand closes the list at the second colon and goes out as text."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("a :fire", delay=20)  # under the debounce, over a task turn
    await shows(page, ["🔥:fire:"])
    assert [v["q"] for v in fake.of("complete")] == ["fire"]  # one question once the typing pauses
    await box.press_sequentially(": b")
    assert await closed(page)
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "a :fire: b"


async def test_mid_word_colon_and_at_ask_nothing(fake: Fake) -> None:
    """012-AC8: only a word start triggers: a time or an address typed in the text asks nothing."""
    box = fake.page.locator("#reply")  # type: ignore[union-attr]
    for part in ("at 10:30", " to me@ann"):  # each pause outlasts the debounce: both ends get their chance
        await box.press_sequentially(part)
        await past(fake.page, POP_ASK_MS)  # type: ignore[arg-type]
    await fake.settled()
    assert fake.of("complete") == []


async def test_a_stale_answer_is_ignored(fake: Fake) -> None:
    """012-AC8: the answer to `fi` landing after `fis` was asked lists nothing; `fis`'s answer does."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
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


async def test_ime_composition_never_asks_nor_picks(fake: Fake) -> None:
    """012-AC8: text still being composed asks nothing; Enter confirming a composition neither picks
    nor sends."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Input.imeSetComposition", {"text": ":fi", "selectionStart": 3, "selectionEnd": 3})
    assert await box.input_value() == ":fi"
    await fake.settled(past_ms=POP_ASK_MS)
    assert fake.of("complete") == []
    await cdp.send("Input.insertText", {"text": ""})
    await box.fill(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    composing = "{key: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true}"
    await box.evaluate(f"b => b.dispatchEvent(new KeyboardEvent('keydown', {composing}))")
    assert await box.input_value() == ":fi"
    assert not await closed(page)
    await fake.settled()
    assert fake.of("send") == []


async def test_custom_emoji_shows_its_image_and_inserts_its_name(fake: Fake) -> None:
    """012-AC8: a custom emoji lists with its `/emoji/<id>` image and inserts `:name:`; a foreign
    image URL is not loaded and a name is text, never markup."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":pa")
    await shows(page, [":partyparrot:", ":pacman:", "x:pa<img src=x onerror=alert(1)>:"])
    imgs = page.locator("#pop img")
    assert await imgs.count() == 1
    assert await imgs.get_attribute("src") == "/emoji/0-abc"
    await page.wait_for_function("document.querySelector('#pop img').naturalWidth > 0")
    await box.press("Enter")
    assert await box.input_value() == ":partyparrot:"


async def test_a_list_for_a_shorter_prefix_is_not_picked_while_the_longer_one_is_asked(fake: Fake) -> None:
    """012-AC8: `:fi` listed, `s` typed: Enter before `fis` is answered inserts nothing, sends nothing;
    once answered it picks from the new list."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    fake.hold = True
    await box.press_sequentially("s")
    await box.press("Enter")
    assert await box.input_value() == ":fis"
    await wait(lambda: len(fake.held) == 1)
    fake.release(0)
    await shows(page, ["🐟:fish:"])
    await box.press("Enter")
    assert await box.input_value() == "🐟"
    await fake.settled()
    assert fake.of("send") == []


async def test_combobox_attributes_follow_the_list(fake: Fake) -> None:
    """012-AC8: the textarea is a combobox whose aria-expanded follows the list; the list is labelled."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    assert await box.get_attribute("role") == "combobox"
    assert await box.get_attribute("aria-expanded") == "false"
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    assert await box.get_attribute("aria-expanded") == "true"
    assert await page.locator("#pop").get_attribute("aria-label") == "emoji"
    await box.fill("@")
    await shows(page, ["everyone", "Ann Lee", "Bob Roe"])
    assert await page.locator("#pop").get_attribute("aria-label") == "members"
    await box.press("Escape")
    assert await box.get_attribute("aria-expanded") == "false"


async def test_member_query_reaches_a_last_name_and_an_inserted_mention_stays_closed(fake: Fake) -> None:
    """012-AC8: `@ann l` asks with its inner space; after an inserted `@Name ` typing on asks nothing
    or gets an empty answer, and the list stays closed."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially("@ann l")
    await shows(page, ["Ann Lee"])
    assert fake.of("complete")[-1]["q"] == "ann l"
    await box.press("Enter")
    await box.press_sequentially("hi @every")
    await shows(page, ["everyone"])
    await box.press("Enter")
    await box.press_sequentially("x")
    await fake.settled(past_ms=POP_ASK_MS)
    await fake.handled()  # an answer to the last question has landed
    assert await closed(page)
    assert await box.input_value() == "@Ann Lee hi @everyone x"
    assert [v["q"] for v in fake.of("complete")][-1] == "every"  # the inserted mention is not asked again


async def test_a_press_on_the_list_scrollbar_keeps_it_open(fake: Fake) -> None:
    """012-AC8: a list longer than its box scrolls; pressing its scrollbar neither blurs nor closes."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":zz")
    await shows(page, [f"z:zz{i:02d}:" for i in range(12)])
    assert await page.evaluate("(p => p.scrollHeight > p.clientHeight)(document.getElementById('pop'))")
    rect = await page.locator("#pop").bounding_box()
    assert rect is not None
    await page.mouse.click(rect["x"] + rect["width"] - 4, rect["y"] + rect["height"] - 8)
    await rendered(page)
    assert not await closed(page)
    assert await page.evaluate("document.activeElement.id") == "reply"


async def test_blur_closes_the_list(fake: Fake) -> None:
    """012-AC8: focus leaving the textarea closes the list."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.blur()
    assert page is not None
    assert await closed(page)


async def test_enter_after_the_caret_left_the_word_sends(fake: Fake) -> None:
    """012-AC8: `ab :fi` listed, Home moves the caret out of the word: Enter sends the text as is."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("ab :fi")
    assert page is not None
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Home")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [{"send": T, "text": "ab :fi", "mentions": [], "format": "inline"}]


async def test_only_plain_emoji_paths_load_an_image(fake: Fake) -> None:
    """012-AC8: `/emoji/..`, `/emoji/%2e%2e`, a newline or a nested path render no image."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":tr")
    await shows(page, [":trok:", ":tr..:", ":tr%2e%2e:", ":tra\nb:", ":tra/b:"])
    srcs = await page.locator("#pop img").evaluate_all("imgs => imgs.map(i => i.getAttribute('src'))")
    assert srcs == ["/emoji/0-ok"]


async def test_malformed_items_are_skipped(fake: Fake) -> None:
    """012-AC8: null, nameless or non-object items in an answer are dropped; the rest still lists."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":nu")
    await shows(page, ["☢:nuke:"])
    await box.press("Enter")
    assert await box.input_value() == "☢"


async def test_an_unanswered_prefix_releases_enter_after_a_while(fake: Fake) -> None:
    """012-AC8: `:fi` listed, `s` asked but never answered: after the wait Enter sends again."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    fake.hold = True
    await box.press_sequentially("s")
    await wait(lambda: len(fake.held) == 1)
    await page.wait_for_selector("#pop", state="hidden", timeout=3000)
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == ":fis"


async def test_a_prefix_typed_offline_lets_enter_report_the_failure(fake: Fake) -> None:
    """012-AC8: the socket drops with the list open; the next prefix cannot be asked, so Enter goes to
    the send path and shows the offline error instead of waiting."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    await fake.ws.close()
    await page.wait_for_function("window.__ws.readyState === 3")
    await box.press_sequentially("s")
    await page.wait_for_selector("#pop", state="hidden", timeout=500)  # before the 2 s release
    await box.press("Enter")
    assert await page.locator(".composer small").inner_text() == "offline, not sent"


async def test_typing_after_an_inserted_mention_asks_nothing(fake: Fake) -> None:
    """012-AC8: `@everyone ` picked, then `thanks`: no question for `everyone t`, `everyone th`…"""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially("@every")
    await shows(page, ["everyone"])
    await box.press("Enter")
    asked = len(fake.of("complete"))
    await box.press_sequentially("thanks")
    await fake.settled(past_ms=POP_ASK_MS)
    assert len(fake.of("complete")) == asked
    assert await box.input_value() == "@everyone thanks"


async def test_enter_on_another_trigger_word_sends_instead_of_inserting(fake: Fake) -> None:
    """012-AC8: `@fi :fi` lists emoji; ← ×4 lands the caret after `@fi`, same prefix, other word:
    Enter sends the text, nothing inserted."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially("@fi :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"])
    for _ in range(4):
        await box.press("ArrowLeft")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "@fi :fi"


# --- review fixes: same-name mentions, direction, contrast ---


async def test_two_people_with_one_name_are_both_mentioned(fake: Fake) -> None:
    """012-AC8: two members sharing a display name are two picks; the send carries both mentions."""
    fake.members = [ANN, ANN2]
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("Enter")
    await box.press_sequentially("and @ann")
    await shows(page, ["Ann Lee", "Ann Lee"])
    await box.press("ArrowDown")
    await box.press("Enter")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert server_reads(fake.of("send")[0], fake.members) == [ANN["mri"], ANN2["mri"]]


@pytest.mark.parametrize(
    ("picks", "sent"), [([0, 1], [ANN2["mri"]]), ([0, 1, 0], [ANN["mri"]])], ids=["other", "re-picked"]
)
async def test_a_deleted_pick_does_not_mention_its_namesake(fake: Fake, picks: list[int], sent: Any) -> None:
    """M1: a namesake picked then deleted from the text is not mentioned; the one picked last is."""
    fake.members = [ANN, ANN2]
    assert await pick_namesakes(fake, picks) == sent


async def test_picks_out_of_text_order_mention_both(fake: Fake) -> None:
    """M1: a mention inserted before an earlier pick still counts, and so does the earlier one; the picks
    go in text order (spec 032)."""
    assert await pick_out_of_order(fake) == [ANN["mri"], BOB["mri"]]


async def test_a_longer_word_leaves_the_first_name_mention(fake: Fake) -> None:
    """L4, spec 032: `@Ann Leeds` is no `@Ann Lee`, but `@Ann` (her first name, a form of the pick) followed
    by `Leeds`, as the server reads it."""
    assert await pick_then_glue(fake) == [ANN["mri"]]


async def test_row_names_and_snippets_take_their_own_direction(pages: Pages) -> None:
    """A right-to-left chat name and snippet read right to left: both carry dir=auto."""
    board = Fake(label="קבוצת עבודה")
    await _open(pages, board)
    assert board.page is not None
    dirs = await board.page.evaluate(
        "['li .name', 'li .prev'].map((s) => document.querySelector(s).getAttribute('dir'))"
    )
    assert dirs == ["auto", "auto"]
    await pages.close()


async def test_dim_text_reaches_4_5_to_1(fake: Fake) -> None:
    """The widget's dim texts (inactive tabs, a plain row's snippet) contrast at least 4.5:1 in light."""
    page = fake.page
    assert page is not None
    await page.evaluate("document.documentElement.dataset.theme = 'light'")
    ratios = await page.evaluate(
        f"[...document.querySelectorAll('#tabs button:not(.on), li .prev')].map((e) => ({CONTRAST})(e))"
    )
    assert len(ratios) == 5 and min(ratios) >= 4.5, ratios
