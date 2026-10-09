"""Chat page composer (spec 012 phases 2 and 3): `:` / `@` completion, toolbar and preview, against a
fake chat socket speaking the 012 wire contract. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_chat_compose.py`.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any
from urllib.parse import quote

import pytest
import test_browser
from test_browser import PNG, Page, Pages, Route  # its import skips without playwright

from miniteams import web as W
from miniteams.compose import compose_html

pytestmark = pytest.mark.browser
pages = test_browser.pages

ORIGIN = "http://127.0.0.1:9"  # never dialled: every request on it is answered by a route
T = "19:fake@thread.v2"
L = "19:other@thread.v2"
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
]
MALFORMED: list[Any] = [None, {"name": 5}, "x", {"name": "nuke", "char": "☢"}]
MINE = {"id": "1", "at": "2026-10-09T10:00:00.000Z", "from": "8:orgid:me", "name": "Me", "me": True}
_BARRIER = {"barrier": 1}
# A module global read by `_open`: a mutation run swaps it for an altered copy.
PAGE = files("miniteams").joinpath("chat.html").read_text(encoding="utf-8")


def _row(thread_id: str, label: str) -> dict[str, Any]:
    return {"id": thread_id, "label": label, "last_activity": "2026-10-09T10:00:00.000Z", "typing": []}


@dataclass
class Fake:
    """The server side of the chat socket: follow, history, board, complete, preview, send."""

    page: Page | None = None
    verbs: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    hold: bool = False  # queue `complete` and `preview` answers instead of sending them
    fail: bool = False
    read_only: bool = False
    version: str = "1"
    ws: Any = None

    def attach(self, ws: Any) -> None:
        self.ws = ws
        ws.on_message(self._on_message)

    def _answer(self, answer: dict[str, Any]) -> None:
        if self.hold:
            self.held.append(answer)
        else:
            self.ws.send(json.dumps(answer))

    def _on_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        self.verbs.append(msg)
        if "follow" in msg:
            label = "Fictional T · 3p" if msg["follow"] == T else "Fictional L · 3p"
            follow = {"follow": msg["follow"], "label": label, "page": self.version, "opener": False}
            self.ws.send(json.dumps({**follow, "read_only": self.read_only}))
        elif "history" in msg:
            messages = [{**MINE, "html": "<p>mine</p>", "editable": True}] if msg["history"] == T else []
            self.ws.send(json.dumps({"history": msg["history"], "messages": messages, "source": "archive"}))
        elif msg.get("board") is True:
            rows = [_row(T, "Fictional T"), _row(L, "Fictional L")]
            self.ws.send(json.dumps({"rows": rows, "browser": False, "me": "Me"}))
        elif "complete" in msg:
            q = msg["q"].lower()
            if q == "nu":
                items = MALFORMED
            elif msg["complete"] == "emoji":
                items = [e for e in EMOJIS if e["name"].startswith(q)]
            else:
                people = (EVERYONE, ANN, BOB)
                items = [
                    m
                    for m in people
                    if any(n.startswith(q) for n in [m["name"].lower(), *m["name"].lower().split()])
                ]
            self._answer({"complete": msg["complete"], "q": msg["q"], "items": items[:12]})
        elif "preview" in msg:
            if msg["preview"] == "bad":
                self._answer({"preview": "", "error": "<b>nope</b>"})
            else:
                html, _ = compose_html(msg["preview"], [], {}, full=True)  # the server's own renderer
                self._answer({"preview": f'<div class="pv">{html}</div>'})
        elif "send" in msg:
            self.ws.send(
                json.dumps({"sent": msg["send"], "ok": not self.fail, "error": "boom" if self.fail else None})
            )

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


async def _until(page: Page, js: str) -> None:
    """wait_for_function evaluates a string the page's CSP refuses: poll through evaluate instead."""
    for _ in range(500):
        if await page.evaluate(js):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{js} not true in 5 s")


async def _open(pages: Pages, fake: Fake, query: str = "") -> Page:
    ctx = await pages.browser.new_context()
    await ctx.route_web_socket(re.compile(r"^ws://127\.0\.0\.1:9/ws\?chat$"), fake.attach)
    await ctx.add_init_script(test_browser._FRAMES)  # after the socket route, as in test_browser_compose

    async def other(route: Route) -> None:
        if not route.request.url.startswith(ORIGIN + "/"):
            pages.external.append(route.request.url)
        await route.abort()

    async def emoji(route: Route) -> None:
        await route.fulfill(body=PNG, content_type="image/png")

    async def page_html(route: Route) -> None:
        # The real policy: the popup images and the preview must work under it.
        headers = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": W._chat_csp(PAGE)}
        await route.fulfill(body=PAGE, headers=headers)

    await ctx.route("**/*", other)  # last registered wins: the catch-all goes first
    await ctx.route(ORIGIN + "/emoji/*", emoji)
    await ctx.route(re.compile(r"^http://127\.0\.0\.1:9/chat/[^/]+$"), page_html)
    page = fake.page = await ctx.new_page()
    page.on("pageerror", lambda exc: pages.errors.append(str(exc)))
    await page.goto(f"{ORIGIN}/chat/{quote(T, safe='')}{query}")
    await _wait(lambda: bool(fake.of("history")))
    await page.wait_for_selector('[data-id="1"]')
    return page


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    board = Fake()
    page = await _open(pages, board)
    await page.locator("#reply").focus()
    yield board
    await page.context.close()
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
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
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
    assert await _closed(page)
    assert await box.get_attribute("aria-expanded") == "false"
    await fake.settled()
    assert fake.of("send") == []


async def test_at_lists_everyone_first_and_send_carries_mentions_in_full_format(fake: Fake) -> None:
    """012-AC8: `@` lists everyone first, Enter/Tab insert `@Full Name`; the send carries the picks and
    `format: full` (the chat view's set)."""
    page, box = _parts(fake)
    await box.press_sequentially("@")
    await _shows(page, ["everyone", "Ann Lee", "Bob Roe"])
    assert await page.locator("#pop").get_attribute("aria-label") == "members"
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
        {"send": T, "text": "@Ann Lee hi @everyone ", "mentions": [ANN, EVERYONE], "format": "full"}
    ]


async def test_a_quote_and_a_mention_travel_together(fake: Fake) -> None:
    """012-AC8: a reply with a quote keeps its `quote` list next to the mentions."""
    page, box = _parts(fake)
    await page.locator('[data-id="1"] [data-act=reply]').dispatch_event("click")
    await box.press_sequentially("@ann")
    await _shows(page, ["Ann Lee"])
    await box.press("Enter")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send") == [
        {"send": T, "text": "@Ann Lee ", "mentions": [ANN], "format": "full", "quote": ["1"]}
    ]


async def test_a_stale_answer_is_ignored(fake: Fake) -> None:
    """012-AC8: the answer to `fi` landing after `fis` was asked lists nothing; `fis`'s answer does."""
    page, box = _parts(fake)
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


async def test_escape_dismisses_the_word_keeps_the_text_then_enter_sends(fake: Fake) -> None:
    """012-AC8: Escape closes the list only, the same word does not reopen it, Enter then sends."""
    page, box = _parts(fake)
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
    assert fake.of("send") == [{"send": T, "text": ":fis", "mentions": [], "format": "full"}]


async def test_a_shorter_prefix_list_is_locked_until_answered_or_two_seconds(fake: Fake) -> None:
    """012-AC8: `:fi` listed, `s` asked: Enter inserts nothing and sends nothing; unanswered, the list
    goes after the wait and Enter sends again."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    fake.hold = True
    await box.press_sequentially("s")
    await _wait(lambda: len(fake.held) == 1)
    await box.press("Enter")
    assert await box.input_value() == ":fis"
    await fake.settled()
    assert fake.of("send") == []
    await page.locator("#pop").wait_for(state="hidden", timeout=3000)
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == ":fis"


async def test_a_prefix_typed_offline_releases_enter_at_once(fake: Fake) -> None:
    """012-AC8: the socket drops with the list open; the next prefix cannot be asked, so Enter reaches
    the send path and says offline instead of waiting."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await fake.ws.close()
    await _until(page, "window.__ws.readyState === 3")
    await box.press_sequentially("s")
    await page.locator("#pop").wait_for(state="hidden", timeout=500)  # before the 2 s release
    await box.press("Enter")
    assert await page.locator("#err").inner_text() == "offline, not sent"


async def test_only_plain_emoji_paths_load_an_image_and_names_are_text(fake: Fake) -> None:
    """012-AC8: images only from `/emoji/<id>` (no `..`, encoded dots, newline or nested path, no
    foreign host); a name carrying markup is text; a custom pick inserts `:name:`."""
    page, box = _parts(fake)
    await box.press_sequentially(":tr")
    await _shows(page, [":trok:", ":tr..:", ":tr%2e%2e:", ":tra\nb:", ":tra/b:"])
    srcs = await page.locator("#pop img").evaluate_all("imgs => imgs.map(i => i.getAttribute('src'))")
    assert srcs == ["/emoji/0-ok"]
    await box.fill(":pa")
    await _shows(page, [":partyparrot:", ":pacman:", "x:pa<img src=x onerror=alert(1)>:"])
    assert await page.locator("#pop img").evaluate_all("imgs => imgs.map(i => i.getAttribute('src'))") == [
        "/emoji/0-abc"
    ]
    await _until(page, "document.querySelector('#pop img').naturalWidth > 0")  # loads under the CSP
    await box.press("Enter")
    assert await box.input_value() == ":partyparrot:"


async def test_malformed_items_are_skipped(fake: Fake) -> None:
    """012-AC8: null, nameless or non-object items are dropped; the rest still lists and picks."""
    page, box = _parts(fake)
    await box.press_sequentially(":nu")
    await _shows(page, ["☢:nuke:"])
    await box.press("Enter")
    assert await box.input_value() == "☢"


async def test_mid_word_triggers_ask_nothing_and_an_inserted_mention_is_not_asked_again(fake: Fake) -> None:
    """012-AC8: `10:30` and `me@ann` ask nothing; `@ann l` asks with its inner space; once `@Ann Lee `
    or `@everyone ` is inserted, typing on asks nothing more."""
    page, box = _parts(fake)
    for part in ("at 10:30", " to me@ann"):
        await box.press_sequentially(part)
        await asyncio.sleep(0.3)  # past the debounce
    await fake.settled()
    assert fake.of("complete") == []
    await box.fill("")
    await box.press_sequentially("@ann l")
    await _shows(page, ["Ann Lee"])
    assert fake.of("complete")[-1]["q"] == "ann l"
    await box.press("Enter")
    await box.press_sequentially("hi @every")
    await _shows(page, ["everyone"])
    await box.press("Enter")
    asked = len(fake.of("complete"))
    await asyncio.sleep(0.3)  # past the debounce: the insert itself asks nothing either
    await box.press_sequentially("thanks")  # `everyone thanks` would still fit the one-space pattern
    await asyncio.sleep(0.3)
    await fake.settled()
    assert len(fake.of("complete")) == asked
    assert await box.input_value() == "@Ann Lee hi @everyone thanks"


async def test_an_emoji_word_after_a_member_word_asks_for_emoji(fake: Fake) -> None:
    """012-AC8: `@ann :fi` reads as the emoji word, not a two-word member query."""
    page, box = _parts(fake)
    await box.press_sequentially("@ann :fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    assert fake.of("complete")[-1] == {"complete": "emoji", "q": "fi", "thread": T}


async def test_quick_typing_asks_once_and_a_typed_shortcode_goes_out_as_typed(fake: Fake) -> None:
    """012-AC8: keys under the debounce ask nothing until the typing pauses; `:fire:` typed by hand
    closes the list at its second colon and is sent as text."""
    page, box = _parts(fake)
    await box.press_sequentially("a :fire", delay=20)
    await _shows(page, ["🔥:fire:"])
    assert [v["q"] for v in fake.of("complete")] == ["fire"]
    await box.press_sequentially(": b")
    assert await _closed(page)
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "a :fire: b"


async def test_caret_leaving_the_word_lets_enter_send(fake: Fake) -> None:
    """012-AC8: `ab :fi` listed, Home moves the caret out of the word: Enter sends the text as is."""
    page, box = _parts(fake)
    await box.press_sequentially("ab :fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Home")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "ab :fi"


async def test_click_picks_a_press_keeps_focus_and_blur_closes(fake: Fake) -> None:
    """012-AC8: a click on an item inserts it without blurring the composer; focus leaving closes."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await page.locator("#pop-1").click()
    assert await box.input_value() == "🐟"
    assert await page.evaluate("document.activeElement.id") == "reply"
    await box.press_sequentially(" :fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.blur()
    assert await _closed(page)


async def test_the_popup_opens_inside_the_window(fake: Fake) -> None:
    """012 Page bullet: the composer sits at the bottom, so the list opens above the caret line (its top
    lies within the textarea padding), on screen."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    pop, reply = await page.locator("#pop").bounding_box(), await box.bounding_box()
    assert pop is not None and reply is not None
    height = await page.evaluate("innerHeight")
    assert (
        pop["y"] >= 0
        and pop["y"] + pop["height"] <= reply["y"] + 12
        and reply["y"] + reply["height"] <= height
    )


async def test_the_edit_in_place_editor_stays_plain(fake: Fake) -> None:
    """012 Formatting bullet: an edit stays plain text, no completion in the message editor."""
    page, _ = _parts(fake)
    await page.locator('[data-id="1"] [data-act=edit]').dispatch_event("click")
    editor = page.locator(".editor")
    await editor.press_sequentially(" :fi @a")
    await asyncio.sleep(0.3)
    await fake.settled()
    assert fake.of("complete") == []
    assert await _closed(page)


# --- picked set per chat ---


async def test_picks_stay_with_their_chat_across_a_split_switch_and_clear_only_on_ok(pages: Pages) -> None:
    """012-AC8, 011-AC22: a pick made in T is not sent from L (nor its preview shown there); back in T it
    is; a failed send keeps it, a successful one clears it."""
    fake = Fake()
    page = await _open(pages, fake, "?chatnav=split")
    box = page.locator("#reply")
    await page.wait_for_selector('#rows li[data-id="19:other@thread.v2"]')
    await box.press_sequentially("@ann")
    await _shows(page, ["Ann Lee"])
    await box.press("Enter")
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await _wait(lambda: fake.of("history")[-1]["history"] == L)
    assert await box.input_value() == ""
    assert await page.locator("#view").is_hidden()  # T's preview does not follow to L
    await box.fill("@Ann Lee by hand")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    await _wait(lambda: fake.of("history")[-1]["history"] == T)
    assert await box.input_value() == "@Ann Lee "
    fake.fail = True
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 2)
    await _until(page, "!document.getElementById('reply').disabled")
    fake.fail = False
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 3)
    await _until(page, "document.getElementById('reply').value === ''")
    await box.fill("@Ann Lee again")
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 4)
    assert [(v["send"], v["mentions"]) for v in fake.of("send")] == [(L, []), (T, [ANN]), (T, [ANN]), (T, [])]
    await page.context.close()
    assert pages.external == []


async def test_picks_survive_the_reload_a_newer_page_triggers(pages: Pages) -> None:
    """012-AC8: like the draft, the picks of an unsent message survive the version reload."""
    fake = Fake()
    page = await _open(pages, fake)
    box = page.locator("#reply")
    await box.press_sequentially("@ann")
    await _shows(page, ["Ann Lee"])
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
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [ANN]
    await page.context.close()
    assert pages.external == []


async def test_invalid_saved_picks_are_left_out(pages: Pages) -> None:
    """012-AC8: a saved draft whose picks were tampered with sends only the well-formed ones."""
    fake = Fake()
    page = await _open(pages, fake)
    bad = [None, {"name": "Bob Roe", "mri": 5}, {"name": "everyone", "mri": T, "everyone": "yes"}, ANN]
    draft = {"text": "@Ann Lee @Bob Roe @everyone", "quotes": [], "picked": bad}
    await page.evaluate(
        f"sessionStorage.setItem({json.dumps('draft:' + T)}, {json.dumps(json.dumps(draft))})"
    )
    await page.reload()
    await page.wait_for_selector('[data-id="1"]')
    box = page.locator("#reply")
    assert await box.input_value() == draft["text"]
    await box.focus()
    await box.press("Enter")
    await _wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["mentions"] == [ANN]
    await page.context.close()
    assert pages.external == []


async def test_an_answer_for_the_chat_left_opens_nothing(pages: Pages) -> None:
    """012-AC8, 011-AC22: `:fi` asked in T, Back to L before the answer: the late answer lists nothing."""
    fake = Fake()
    page = await _open(pages, fake, "?chatnav=split")
    box = page.locator("#reply")
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await _wait(lambda: fake.of("history")[-1]["history"] == L)
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    await _wait(lambda: fake.of("history")[-1]["history"] == T)
    await box.focus()
    fake.hold = True
    await box.press_sequentially(":fi")
    await _wait(lambda: len(fake.held) == 1)
    await page.go_back()  # a switch that leaves the focus in the composer
    await _wait(lambda: fake.of("history")[-1]["history"] == L)
    fake.release(0)
    await asyncio.sleep(0.2)
    assert await _closed(page)
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
    await _wait(lambda: len(fake.of("send")) == 1)


async def test_a_stale_preview_is_dropped(fake: Fake) -> None:
    """012-AC14: an answer to an ask made before the text changed, or landing after the toggle went
    off, shows nothing; the answer to the latest ask does."""
    page, box = _parts(fake)
    fake.hold = True
    await box.fill("one")
    await page.locator("#show").click()
    await _wait(lambda: len(fake.held) == 1)
    await box.press_sequentially("two")  # hides; asked again below, the first answer still on its way
    await page.locator("#show").click()
    await _wait(lambda: len(fake.held) == 2)
    fake.release(0)
    await asyncio.sleep(0.2)
    assert await page.locator("#view").is_hidden()
    fake.release(1)
    await page.locator("#view .pv").wait_for()
    assert await page.locator("#view").inner_text() == "onetwo"
    await page.locator("#show").click()  # off before its answer: nothing shows
    await page.locator("#show").click()
    await _wait(lambda: len(fake.held) == 3)
    await page.locator("#show").click()
    fake.release(2)
    await asyncio.sleep(0.2)
    assert await page.locator("#view").is_hidden()


async def test_preview_after_reconnect_shows_again(fake: Fake) -> None:
    """012-AC14: an ask whose answer died with the socket releases the button and does not hold back
    the next preview."""
    page, box = _parts(fake)
    fake.hold = True
    await box.fill("a")
    await page.locator("#show").click()
    await _wait(lambda: len(fake.held) == 1)
    await fake.ws.close()
    await _until(page, "document.getElementById('show').getAttribute('aria-pressed') === 'false'")
    await _wait(lambda: len(fake.of("follow")) == 2)  # reconnected
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
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Enter")
    assert await box.input_value() == "🔥"
    await box.press("Control+z")
    assert await box.input_value() == ":fi"


async def test_bold_with_the_list_open_closes_it(fake: Fake) -> None:
    """012-AC8, 012-AC14: B clicked or Ctrl+B pressed while the list is open closes the list."""
    page, box = _parts(fake)
    await box.press_sequentially(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await page.locator('#tools [data-fmt="bold"]').click()
    assert await _closed(page)
    await box.fill(":fi")
    await _shows(page, ["🔥:fire:", "🐟:fish:"])
    await box.press("Control+b")
    assert await _closed(page)
    await asyncio.sleep(0.3)  # past the debounce: nothing reopens it
    assert await _closed(page)


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
    await _until(page, "document.body.classList.contains('readonly')")
    assert await page.locator("#tools").is_hidden()
    assert await page.locator("#show").is_hidden()
    await page.context.close()
    assert pages.external == []
