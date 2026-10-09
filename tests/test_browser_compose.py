"""Widget composer `:` / `@` completion (spec 012 phase 2) against a fake board speaking the 012 wire
contract (`complete`, `send` with `mentions` + `format`). Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_compose.py`.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

import pytest
import test_browser
from test_browser import PNG, Page, Pages, Route  # its import skips without playwright

pytestmark = pytest.mark.browser
pages = test_browser.pages

ORIGIN = "http://127.0.0.1:9"  # never dialled: every request on it is answered by a route
T = "19:fake@thread.v2"
ANN = {"name": "Ann Lee", "mri": "8:orgid:ann"}
BOB = {"name": "Bob Roe", "mri": "8:orgid:bob"}
EVERYONE = {"name": "everyone", "mri": T, "everyone": True}
EMOJIS = [
    {"name": "fire", "char": "🔥"},
    {"name": "fish", "char": "🐟"},
    {"name": "partyparrot", "custom": "0-abc", "src": "/emoji/0-abc"},
    {"name": "pacman", "custom": "0-far", "src": "https://evil.example/x.png"},
    {"name": "pa<img src=x onerror=alert(1)>", "char": "x"},
    {"name": "trok", "custom": "0-ok", "src": "/emoji/0-ok"},
    *({"name": f"tr{i}", "custom": i, "src": f"/emoji/{i}"} for i in ("..", "%2e%2e", "a\nb", "a/b")),
    *({"name": f"zz{i:02d}", "char": "z"} for i in range(12)),  # more rows than the list shows
]
MALFORMED: list[Any] = [None, {"name": 5}, "x", {"name": "nuke", "char": "☢"}]
_BARRIER = {"barrier": 1}


@dataclass
class Fake:
    """The board side of the socket: answers `complete` from the tables above, acks `send`."""

    page: Page | None = None
    verbs: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    hold: bool = False  # queue `complete` answers instead of sending them
    fail: bool = False
    ws: Any = None

    def attach(self, ws: Any) -> None:
        self.ws = ws
        ws.on_message(self._on_message)
        row = {"id": T, "label": "Fictional chat", "last_activity": "2026-10-09T10:00:00.000Z", "typing": []}
        ws.send(json.dumps({"rows": [row], "page": "1", "opener": False, "browser": False, "me": "Me"}))

    def _on_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        self.verbs.append(msg)
        if "complete" in msg:
            q = msg["q"].lower()
            if q == "nu":
                items = MALFORMED
            elif msg["complete"] == "emoji":
                items = [e for e in EMOJIS if e["name"].startswith(q)]
            else:
                names = {
                    m["name"]: [m["name"].lower(), *m["name"].lower().split()] for m in (EVERYONE, ANN, BOB)
                }
                items = [m for m in (EVERYONE, ANN, BOB) if any(n.startswith(q) for n in names[m["name"]])]
            answer = {"complete": msg["complete"], "q": msg["q"], "items": items[:12]}
            if self.hold:
                self.held.append(answer)
            else:
                self.ws.send(json.dumps(answer))
        elif "send" in msg:
            ack = {"sent": msg["send"], "ok": not self.fail, "error": "boom" if self.fail else None}
            self.ws.send(json.dumps(ack))

    def release(self, i: int) -> None:
        self.ws.send(json.dumps(self.held[i]))

    def of(self, verb: str) -> list[dict[str, Any]]:
        return [v for v in self.verbs if verb in v]

    async def settled(self) -> None:
        """Every verb the page sent so far reached us: the socket is ordered, a barrier closes it."""
        assert self.page is not None
        await self.page.evaluate(f"window.__ws.send({json.dumps(json.dumps(_BARRIER))})")
        await _wait(lambda: _BARRIER in self.verbs)
        self.verbs.remove(_BARRIER)


async def _wait(cond: Callable[[], bool]) -> None:
    for _ in range(500):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in 5 s")


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    widget = files("miniteams").joinpath("widget.html").read_text(encoding="utf-8")
    ctx = await pages.browser.new_context()
    board = Fake()
    await ctx.route_web_socket(ORIGIN.replace("http", "ws") + "/ws", board.attach)
    # After the socket route: its own WebSocket stand-in must be the class _FRAMES wraps.
    await ctx.add_init_script(test_browser._FRAMES)

    async def other(route: Route) -> None:
        if not route.request.url.startswith(ORIGIN + "/"):
            pages.external.append(route.request.url)
        await route.abort()

    async def emoji(route: Route) -> None:
        await route.fulfill(body=PNG, content_type="image/png")

    async def page_html(route: Route) -> None:
        await route.fulfill(body=widget, content_type="text/html; charset=utf-8")

    # Last registered wins: the catch-all goes first.
    await ctx.route("**/*", other)
    await ctx.route(ORIGIN + "/emoji/*", emoji)
    await ctx.route(ORIGIN + "/", page_html)
    page = board.page = await ctx.new_page()
    page.on("pageerror", lambda exc: pages.errors.append(str(exc)))
    await page.goto(ORIGIN + "/")
    await page.wait_for_selector("a.name")
    await page.locator("button.reply").dispatch_event("click")
    await page.wait_for_function("document.activeElement.id === 'reply'")
    yield board
    await ctx.close()
    assert pages.external == []


async def _shows(page: Page, expected: list[str]) -> None:
    """The open list reads `expected`: an answer to an earlier prefix may show first."""
    seen: list[str] = []
    for _ in range(500):
        if not await page.locator("#pop").is_hidden():
            seen = await page.locator("#pop [role=option]").all_text_contents()
            if seen == expected:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"list shows {seen}, expected {expected}")


async def _closed(page: Page) -> bool:
    return await page.locator("#pop").is_hidden()


async def test_colon_opens_the_emoji_list_and_enter_inserts_without_sending(fake: Fake) -> None:
    """012-AC8: `:fi` asks once the typing pauses, lists the answer; Enter inserts 🔥 and sends nothing."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("hot :fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    assert fake.of("complete") == [{"complete": "emoji", "q": "fi", "thread": T}]
    assert await page.locator("#pop").get_attribute("role") == "listbox"
    assert await box.get_attribute("aria-activedescendant") == "pop-0"
    await box.press("ArrowDown")
    assert await page.locator("#pop-1").get_attribute("aria-selected") == "true"
    await box.press("ArrowUp")
    await box.press("Enter")
    assert await box.input_value() == "hot 🔥"
    assert await _closed(page)
    await fake.settled()
    assert fake.of("send") == []


async def test_at_lists_members_everyone_first_and_send_carries_the_mentions(fake: Fake) -> None:
    """012-AC8: `@` lists everyone first, Enter inserts `@Full Name` and records the pick; Tab picks
    too; the send carries the picked mentions and `format: inline`."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("@")
    await _shows(page, ["everyone", "Ann Lee", "Bob Roe"])
    assert fake.of("complete")[-1] == {"complete": "member", "q": "", "thread": T}
    await box.press("ArrowDown")
    await box.press("Enter")
    assert await box.input_value() == "@Ann Lee "
    await box.press_sequentially("hi @ev")
    await _shows(page, ["everyone"])
    await box.press("Tab")
    assert await box.input_value() == "@Ann Lee hi @everyone "
    assert await page.evaluate("document.activeElement.id") == "reply"
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [
        {"send": T, "text": "@Ann Lee hi @everyone ", "mentions": [ANN, EVERYONE], "format": "inline"}
    ]


async def test_a_removed_mention_is_not_sent_and_picks_survive_a_failure_only(fake: Fake) -> None:
    """012-AC8: a pick whose `@Name` left the text is not sent; the picks survive a failed send and
    clear on a successful one."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    for prefix in ("ann", "bob"):
        await box.press_sequentially("@" + prefix)
        await _shows(page, [{"ann": "Ann Lee", "bob": "Bob Roe"}[prefix]])
        await box.press("Enter")
    assert await box.input_value() == "@Ann Lee @Bob Roe "
    await box.fill("@Ann Lee only")
    fake.fail = True
    await box.press("Enter")
    await page.wait_for_function("!document.getElementById('reply').disabled")
    fake.fail = False
    await box.press("Enter")
    await page.wait_for_selector("#reply", state="detached")
    await page.locator("button.reply").dispatch_event("click")
    await box.fill("@Ann Lee again")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 3)
    assert [v["mentions"] for v in fake.of("send")] == [[ANN], [ANN], []]


async def test_escape_closes_the_list_keeps_the_text_then_enter_sends(fake: Fake) -> None:
    """012-AC8: Escape closes the list only (text and composer stay), the same word does not reopen it,
    and Enter with the list closed sends."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Escape")
    assert await _closed(page)
    assert await box.input_value() == ":fi"
    await box.press_sequentially("s")
    await asyncio.sleep(0.3)  # past the debounce
    await fake.settled()
    assert [v["q"] for v in fake.of("complete")] == ["fi"]
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [{"send": T, "text": ":fis", "mentions": [], "format": "inline"}]


async def test_a_typed_shortcode_is_sent_as_typed(fake: Fake) -> None:
    """012-AC8: `:fire:` typed by hand closes the list at the second colon and goes out as text."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("a :fire", delay=20)  # under the debounce, over a task turn
    await _shows(page, ["🔥:fire:"])
    assert [v["q"] for v in fake.of("complete")] == ["fire"]  # one question once the typing pauses
    await box.press_sequentially(": b")
    assert await _closed(page)
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "a :fire: b"


async def test_mid_word_colon_and_at_ask_nothing(fake: Fake) -> None:
    """012-AC8: only a word start triggers: a time or an address typed in the text asks nothing."""
    box = fake.page.locator("#reply")  # type: ignore[union-attr]
    for part in ("at 10:30", " to me@ann"):  # each pause outlasts the debounce: both ends get their chance
        await box.press_sequentially(part)
        await asyncio.sleep(0.3)
    await fake.settled()
    assert fake.of("complete") == []


async def test_a_stale_answer_is_ignored(fake: Fake) -> None:
    """012-AC8: the answer to `fi` landing after `fis` was asked lists nothing; `fis`'s answer does."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    fake.hold = True
    await box.press_sequentially(":fi")
    await _wait(lambda: len(fake.held) == 1)
    await box.press_sequentially("s")
    await _wait(lambda: len(fake.held) == 2)
    fake.release(0)
    await asyncio.sleep(0.2)
    assert await _closed(page)
    fake.release(1)
    await _shows(page, ["🐟:fish:"])


async def test_ime_composition_never_asks_nor_picks(fake: Fake) -> None:
    """012-AC8: text still being composed asks nothing; Enter confirming a composition neither picks
    nor sends."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Input.imeSetComposition", {"text": ":fi", "selectionStart": 3, "selectionEnd": 3})
    assert await box.input_value() == ":fi"
    await asyncio.sleep(0.3)  # past the debounce
    await fake.settled()
    assert fake.of("complete") == []
    await cdp.send("Input.insertText", {"text": ""})
    await box.fill(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    composing = "{key: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true}"
    await box.evaluate(f"b => b.dispatchEvent(new KeyboardEvent('keydown', {composing}))")
    assert await box.input_value() == ":fi"
    assert not await _closed(page)
    await fake.settled()
    assert fake.of("send") == []


async def test_custom_emoji_shows_its_image_and_inserts_its_name(fake: Fake) -> None:
    """012-AC8: a custom emoji lists with its `/emoji/<id>` image and inserts `:name:`; a foreign
    image URL is not loaded and a name is text, never markup."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":pa")
    await _shows(page, [":partyparrot:", ":pacman:", "x:pa<img src=x onerror=alert(1)>:"])
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
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    fake.hold = True
    await box.press_sequentially("s")
    await box.press("Enter")
    assert await box.input_value() == ":fis"
    await _wait(lambda: len(fake.held) == 1)
    fake.release(0)
    await _shows(page, ["🐟:fish:"])
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
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    assert await box.get_attribute("aria-expanded") == "true"
    assert await page.locator("#pop").get_attribute("aria-label") == "emoji"
    await box.fill("@")
    await _shows(page, ["everyone", "Ann Lee", "Bob Roe"])
    assert await page.locator("#pop").get_attribute("aria-label") == "members"
    await box.press("Escape")
    assert await box.get_attribute("aria-expanded") == "false"


async def test_member_query_reaches_a_last_name_and_an_inserted_mention_stays_closed(fake: Fake) -> None:
    """012-AC8: `@ann l` asks with its inner space; after an inserted `@Name ` typing on asks nothing
    or gets an empty answer, and the list stays closed."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially("@ann l")
    await _shows(page, ["Ann Lee"])
    assert fake.of("complete")[-1]["q"] == "ann l"
    await box.press("Enter")
    await box.press_sequentially("hi @every")
    await _shows(page, ["everyone"])
    await box.press("Enter")
    await box.press_sequentially("x")
    await asyncio.sleep(0.3)  # past the debounce
    await fake.settled()
    await asyncio.sleep(0.1)  # an answer to the last question has landed
    assert await _closed(page)
    assert await box.input_value() == "@Ann Lee hi @everyone x"
    assert [v["q"] for v in fake.of("complete")][-1] == "every"  # the inserted mention is not asked again


async def test_a_press_on_the_list_scrollbar_keeps_it_open(fake: Fake) -> None:
    """012-AC8: a list longer than its box scrolls; pressing its scrollbar neither blurs nor closes."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":zz")
    await _shows(page, [f"z:zz{i:02d}:" for i in range(12)])
    assert await page.evaluate("(p => p.scrollHeight > p.clientHeight)(document.getElementById('pop'))")
    rect = await page.locator("#pop").bounding_box()
    assert rect is not None
    await page.mouse.click(rect["x"] + rect["width"] - 4, rect["y"] + rect["height"] - 8)
    await asyncio.sleep(0.1)
    assert not await _closed(page)
    assert await page.evaluate("document.activeElement.id") == "reply"


async def test_blur_closes_the_list(fake: Fake) -> None:
    """012-AC8: focus leaving the textarea closes the list."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.blur()
    assert page is not None
    assert await _closed(page)


async def test_enter_after_the_caret_left_the_word_sends(fake: Fake) -> None:
    """012-AC8: `ab :fi` listed, Home moves the caret out of the word: Enter sends the text as is."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    await box.press_sequentially("ab :fi")
    assert page is not None
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Home")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [{"send": T, "text": "ab :fi", "mentions": [], "format": "inline"}]


async def test_only_plain_emoji_paths_load_an_image(fake: Fake) -> None:
    """012-AC8: `/emoji/..`, `/emoji/%2e%2e`, a newline or a nested path render no image."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":tr")
    await _shows(page, [":trok:", ":tr..:", ":tr%2e%2e:", ":tra\nb:", ":tra/b:"])
    srcs = await page.locator("#pop img").evaluate_all("imgs => imgs.map(i => i.getAttribute('src'))")
    assert srcs == ["/emoji/0-ok"]


async def test_malformed_items_are_skipped(fake: Fake) -> None:
    """012-AC8: null, nameless or non-object items in an answer are dropped; the rest still lists."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":nu")
    await _shows(page, ["☢:nuke:"])
    await box.press("Enter")
    assert await box.input_value() == "☢"


async def test_an_unanswered_prefix_releases_enter_after_a_while(fake: Fake) -> None:
    """012-AC8: `:fi` listed, `s` asked but never answered: after the wait Enter sends again."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    fake.hold = True
    await box.press_sequentially("s")
    await _wait(lambda: len(fake.held) == 1)
    await page.wait_for_selector("#pop", state="hidden", timeout=3000)
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == ":fis"


async def test_a_prefix_typed_offline_lets_enter_report_the_failure(fake: Fake) -> None:
    """012-AC8: the socket drops with the list open; the next prefix cannot be asked, so Enter goes to
    the send path and shows the offline error instead of waiting."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
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
    await _shows(page, ["everyone"])
    await box.press("Enter")
    asked = len(fake.of("complete"))
    await box.press_sequentially("thanks")
    await asyncio.sleep(0.3)  # past the debounce
    await fake.settled()
    assert len(fake.of("complete")) == asked
    assert await box.input_value() == "@everyone thanks"


async def test_enter_on_another_trigger_word_sends_instead_of_inserting(fake: Fake) -> None:
    """012-AC8: `@fi :fi` lists emoji; ← ×4 lands the caret after `@fi`, same prefix, other word:
    Enter sends the text, nothing inserted."""
    page, box = fake.page, fake.page.locator("#reply")  # type: ignore[union-attr]
    assert page is not None
    await box.press_sequentially("@fi :fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    for _ in range(4):
        await box.press("ArrowLeft")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "@fi :fi"
