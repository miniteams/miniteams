"""Chat page and widget in a headless browser (spec 011). Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser.py` (playwright stays out of the project deps).
"""

import asyncio
import contextlib
import json
import os
import re
import resource
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import test_web_chat
from test_web_chat import ANN, ME, OBJ, C, T, _archive, _at, _event, _m, _served, _until

from miniteams import web as W
from miniteams.archive_store import find_chat_dir
from miniteams.config import Settings

CHROME = Path("/usr/bin/google-chrome")
# e2e layers must not silently skip in CI: there a missing playwright or Chrome fails instead.
if os.environ.get("CI", "").lower() not in ("1", "true"):
    pytest.importorskip("playwright.async_api", reason="run with `uv run --with playwright`")
    if not CHROME.exists():
        pytest.skip(f"{CHROME} missing", allow_module_level=True)

from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright  # noqa: E402

pytestmark = pytest.mark.browser
# test_web_chat's fixtures; `board` builds on this module's `data_dir`.
board, calls, launched, quiet_directory, sent = (
    test_web_chat.board,
    test_web_chat.calls,
    test_web_chat.launched,
    test_web_chat.quiet_directory,
    test_web_chat.sent,
)

L = "19:llll@thread.v2"  # archived, long enough for the list to scroll
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"  # pragma: allowlist secret
)
# Each socket frame lands in window.__frames once the page's own handler ran (setTimeout: after it);
# window.__ws is the page's latest socket, for the tests' own verbs.
_FRAMES = """(() => { const Real = WebSocket; window.__frames = [];
  window.WebSocket = class extends Real { constructor(...a) { super(...a); window.__ws = this;
    this.addEventListener("message", (e) => setTimeout(() => window.__frames.push(JSON.parse(e.data)))); } };
})();"""
_BARRIER = ("barrier", False)
# (thread, web) for an `open` verb; (thread, "chat") when it asks for our own chat page.
Verb = tuple[str, bool | str]
_LIST = "document.getElementById('list')"
_AT_BOTTOM = f"(l => l.scrollHeight - l.scrollTop - l.clientHeight < 40)({_LIST})"


@dataclass
class Pages:
    browser: Browser
    host: str = ""
    external: list[str] = field(default_factory=list)  # requests that tried to leave 127.0.0.1
    errors: list[str] = field(default_factory=list)

    async def open(self, path: str, *, csp: bool = False, **ctx: Any) -> Page:
        """A page on `path` of the served board. wait_for_function evaluates, which the chat CSP
        forbids: contexts bypass it unless `csp` asks for the real policy."""
        context = await self.browser.new_context(bypass_csp=not csp, **ctx)
        await context.add_init_script(_FRAMES)
        await context.route(re.compile(r"^(?!http://127\.0\.0\.1:)"), self._block)
        page = await context.new_page()
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        await page.goto(f"http://{self.host}{path}")
        return page

    async def _block(self, route: Route) -> None:
        self.external.append(route.request.url)
        await route.abort()


@contextlib.contextmanager
def _uncapped() -> Iterator[None]:
    """Chrome reserves far more address space than conftest's RLIMIT_AS cap: lift it while the
    playwright driver (and so Chrome, its child) is spawned, then restore it for this process."""
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


@pytest.fixture
async def pages() -> AsyncIterator[Pages]:
    with _uncapped():
        driver = async_playwright()
        p = await driver.__aenter__()
    browser = None
    try:
        browser = await p.chromium.launch(executable_path=str(CHROME), headless=True)
        found = Pages(browser)
        yield found
    finally:
        if browser is not None:
            await browser.close()
        await driver.__aexit__(None, None, None)
    assert found.errors == []


def _chat(thread_id: str) -> str:
    return f"/chat/{quote(thread_id, safe='')}"


async def _frames(page: Page) -> int:
    return int(await page.evaluate("window.__frames.length"))


async def _handled(page: Page, key: str, since: int = 0) -> None:
    """Wait until the page handled a frame carrying `key` (`follow`, `history`…) after frame `since`."""
    await page.wait_for_function(f"window.__frames.slice({since}).some(f => {json.dumps(key)} in f)")


async def _drop(board: W.Board) -> None:
    for sock in list(board._follows):
        await sock.close()


# --- chat page (011 phase 2) ---


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Overrides test_web_chat's archive: what the page renders, plus a long chat and a channel."""
    path = tmp_path / "data"
    img = f"https://eu-api.asm.skype.com/v1/objects/{OBJ}/views/imgo"
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Self"}
    like = {"emotions": [{"key": "like", "users": [{"mri": ANN}]}]}
    _archive(
        path,
        T,
        [
            _m("1", _at(1), "<p>hello <b>there</b><script>alert(1)</script></p>"),
            _m("2", _at(2), f'<p><img itemtype="http://schema.skype.com/AMSImage" src="{img}"></p>'),
            _m("3", _at(3), "<p>mine</p>", properties=like, **mine),
        ],
    )
    chat_dir = find_chat_dir(path, T)
    assert chat_dir is not None
    (chat_dir / "media" / f"{OBJ}.png").write_bytes(PNG)
    _archive(path, L, [_m(str(i), _at(i)) for i in range(10, 40)])
    _archive(path, C, [])
    return path


@pytest.fixture
async def chat(board: W.Board, settings: Settings, pages: Pages) -> AsyncIterator[Pages]:
    async with _served(board, settings) as host:
        pages.host = host
        yield pages
    assert pages.external == []


async def test_page_renders_the_archived_history(chat: Pages) -> None:
    """011-AC3, 011-AC6: archived messages render sanitised, grouped, with reactions and /media images."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    assert await page.locator(".msg").count() == 3
    assert await page.locator("script", has_text="alert").count() == 0
    assert await page.locator(".msg").first.locator(".html b").inner_text() == "there"
    await page.wait_for_function("document.querySelector('.html img')?.naturalWidth > 0")
    assert await page.locator(".msg.cont").count() == 1
    assert await page.locator(".msg.me .reacts span").inner_text() == "👍 1"


async def test_live_append_edit_delete_reaction_and_typing(board: W.Board, chat: Pages) -> None:
    """011-AC7: a followed page appends, edits in place, marks deletes, updates reactions, shows typing."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await board.on_event(_event("NewMessage", T, **_m("4", _at(4), "<p>live one</p>")))
    await page.wait_for_selector('[data-id="4"]')
    assert await page.locator(".msg").count() == 4
    reaction = {"emotions": [{"key": "heart", "users": [{"mri": ME}]}]}
    partial = {k: v for k, v in _m("4", _at(4)).items() if k != "content"}
    await board.on_event(_event("MessageUpdate", T, **{**partial, "properties": reaction}))
    await page.wait_for_selector('[data-id="4"] .reacts span.mine')
    assert "live one" in await page.locator('[data-id="4"] .html').inner_text()  # a partial edit keeps it
    edit = {"edittime": "1789466500000"}
    await board.on_event(_event("MessageUpdate", T, **_m("4", _at(4), "<p>live two</p>", properties=edit)))
    await page.wait_for_selector('[data-id="4"] .edited')
    assert "live two" in await page.locator('[data-id="4"] .html').inner_text()
    gone = {"deletetime": "1789466600000"}
    await board.on_event(
        _event("MessageUpdate", T, **{**_m("4", _at(4), "x"), "content": "", "properties": gone})
    )
    await page.wait_for_selector('[data-id="4"].deleted')
    assert await page.locator('[data-id="4"] .html').inner_text() == "message deleted"
    await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
    await page.wait_for_function("document.getElementById('typing').textContent.includes('Ann Lee')")


async def test_composer_keys_success_and_failure(
    chat: Pages, sent: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC8: Shift+Enter breaks the line, Enter sends once and clears; a failure keeps the text."""
    page = await chat.open(_chat(T))
    await _handled(page, "follow")
    box = page.locator("#reply")
    await box.fill("first line")
    await box.press("Shift+Enter")
    await box.press_sequentially("second")
    await box.press("Enter")
    await page.wait_for_function("document.getElementById('reply').value === ''")
    assert sent == [(T, "first line\nsecond")]

    def failing(*args: Any) -> None:
        raise httpx.ConnectError("boom", request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(W, "send_message", failing)
    await box.fill("keep me")
    await box.press("Control+Enter")
    await page.wait_for_function("document.getElementById('err').textContent !== ''")
    assert await box.input_value() == "keep me"
    assert await page.locator("#err").inner_text() == "network error"
    assert not await box.is_disabled()


async def test_open_in_teams_without_opener_opens_the_https_link(board: W.Board, chat: Pages) -> None:
    """011-AC19: with `--opener none` the header button opens the https deep link in a new tab."""
    page = await chat.open(_chat(T))
    await page.wait_for_selector("#teams:not([disabled])")
    async with page.context.expect_page() as tab:
        await page.click("#teams")
    await (await tab.value).close()
    await _until(chat.external, 1)
    assert chat.external == [W.deep_link(T, "3")]  # blocked by the route: nothing left the machine
    chat.external.clear()


async def test_open_in_teams_with_opener_runs_it(
    launched: list[tuple[str, ...]], board: W.Board, chat: Pages
) -> None:
    """011-AC19: header and message Open in Teams go through the server's opener, on the right message."""
    board.opener = ["xdg-open"]
    page = await chat.open(_chat(T))
    await page.wait_for_selector("#teams:not([disabled])")
    await page.click("#teams")
    await _until(launched, 1)
    first = page.locator(".msg").first
    await first.hover()
    await first.locator(".actions [data-act=teams]").click()
    await _until(launched, 2)
    scheme = board.open_scheme
    assert launched == [("xdg-open", W.deep_link(T, "3", scheme)), ("xdg-open", W.deep_link(T, "1", scheme))]
    assert len(page.context.pages) == 1


async def test_csp_blocks_an_inline_handler_the_sanitiser_let_through(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC6: with the real CSP, an event attribute in a message body never runs."""
    page = await chat.open(_chat(T), csp=True)
    await page.wait_for_selector("#status:not(.off)")  # the page's own script and socket pass the CSP
    monkeypatch.setattr(W, "sanitize", lambda content, *a: content)
    body = '<p>x<img src="/nope.png" onerror="window.__pwned=1"></p>'
    async with page.expect_console_message(lambda msg: "inline event handler" in msg.text):
        await board.on_event(_event("NewMessage", T, **_m("20", _at(20), body)))
    assert await page.locator('[data-id="20"] img[onerror]').count() == 1  # the slip reached the DOM
    assert await page.evaluate("window.__pwned") is None


async def test_reconnect_keeps_the_readers_place(board: W.Board, chat: Pages) -> None:
    """Spec 011 Page bullet: after a drop the page follows again and keeps the reader's place."""
    page = await chat.open(_chat(L), viewport={"width": 700, "height": 300})
    await _handled(page, "history")
    assert await page.evaluate(f"(l => l.scrollHeight > l.clientHeight * 2)({_LIST})")
    await page.evaluate("window.__same = 1")
    for scroll, kept in (("0", "l.scrollTop === 0"), ("l.scrollHeight", _AT_BOTTOM)):
        # dispatch: the page reads its scroll position in the handler, not on a later frame
        await page.evaluate(
            f"(l => {{ l.scrollTop = {scroll}; l.dispatchEvent(new Event('scroll')); }})({_LIST})"
        )
        since = await _frames(page)
        await _drop(board)
        await _handled(page, "history", since)
        assert await page.evaluate(f"(l => {kept})({_LIST})"), scroll
    assert await page.evaluate("window.__same") == 1  # reconnected, not reloaded


async def test_edit_during_the_history_read_is_applied_after_it(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 011 Page bullet: an edit relayed while the latest page is read survives that page."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    reading, release = threading.Event(), threading.Event()
    real_read = W.read_page

    def held_read(*args: Any) -> Any:
        reading.set()
        release.wait(5)
        return real_read(*args)

    monkeypatch.setattr(W, "read_page", held_read)
    since = await _frames(page)
    await _drop(board)
    assert await asyncio.to_thread(reading.wait, 10)
    edit = {"edittime": "1789466500000"}
    await board.on_event(
        _event("MessageUpdate", T, **_m("1", _at(1), "<p>patched while loading</p>", properties=edit))
    )
    release.set()
    await _handled(page, "history", since)
    assert "patched while loading" in await page.locator('[data-id="1"] .html').inner_text()


async def test_newer_page_reloads_the_tab_and_keeps_the_draft(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer chat.html reloads the open tab; the composer draft, quotes included, survives it (no AC)."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.locator('[data-id="1"] [data-act=reply]').dispatch_event("click")
    await page.locator("#reply").fill("half-written\nreply")
    await page.evaluate("window.__same = 1")
    monkeypatch.setattr(W, "page_version", lambda name="widget.html": "newer")
    async with page.expect_event("load"):
        await _drop(board)
    assert await page.evaluate("window.__same") is None
    assert await page.locator("#reply").input_value() == "half-written\nreply"
    assert await page.locator("#quotes .quote").evaluate_all("qs => qs.map(q => q.dataset.quote)") == ["1"]
    assert await page.evaluate("sessionStorage.length") == 0


async def test_channel_page_has_no_composer(chat: Pages) -> None:
    """Spec 011 Composer bullet: an archived team channel opens read-only."""
    page = await chat.open(_chat(C))
    await _handled(page, "follow")
    assert await page.locator("#reply").is_disabled()
    assert await page.locator("#reply").get_attribute("placeholder") == "read-only: team channel"


async def test_malformed_chat_address_is_guarded(chat: Pages) -> None:
    """The page refuses an address it cannot decode (no AC; served by a stub: the server 404s it)."""
    html = W._page("chat.html")
    headers = {"Content-Type": "text/html", "Content-Security-Policy": W._chat_csp(html)}
    page = await chat.open("/")

    async def stub(route: Route) -> None:
        await route.fulfill(status=200, body=html, headers=headers)

    await page.route("**/chat/%E0%A4%A", stub)
    await page.goto(f"http://{chat.host}/chat/%E0%A4%A")
    assert await page.locator("#note").inner_text() == "malformed chat address"
    assert await page.locator("#reply").is_disabled()


async def test_follow_error_is_not_overwritten_by_the_history_error(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC9: a refused follow keeps its reason on screen and Open in Teams stays disabled."""
    nope = "19:yyyy@thread.v2"
    board.rows[nope] = {"id": nope, "label": "N", "last_activity": _at(1), "typing": []}

    async def refuse(ws: Any, thread_id: str) -> dict[str, Any]:
        return {"follow": thread_id, "error": "unknown chat"}

    def offline(*args: Any) -> Any:
        raise RuntimeError("no API in tests")

    monkeypatch.setattr(board, "follow", refuse)
    monkeypatch.setattr(W, "fetch_history", offline)
    page = await chat.open(_chat(nope))
    await _handled(page, "follow")
    await _handled(page, "history")
    assert await page.locator("#note").inner_text() == "unknown chat"
    assert await page.locator("#teams").is_disabled()


# --- message actions (011 phase 4) ---


def _act(page: Page, msg_id: str, act: str) -> Any:
    return page.locator(f'[data-id="{msg_id}"] [data-act={act}]')


async def _quote_ids(page: Page) -> list[str]:
    return list(await page.locator("#quotes .quote").evaluate_all("qs => qs.map(q => q.dataset.quote)"))


async def test_reply_builds_the_quote_list_and_sends_it(
    board: W.Board, chat: Pages, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC11: Reply adds author + first line above the composer, once per message, ✕ drops one;
    Enter sends the ids in the order picked and clears the list on success; the echo shows the quotes."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    for msg_id in ("3", "1", "3"):  # quoting one already there does nothing
        await page.locator(f'[data-id="{msg_id}"]').hover()
        await _act(page, msg_id, "reply").click()
    assert await _quote_ids(page) == ["3", "1"]
    first = page.locator("#quotes .quote").nth(1)
    assert await first.locator("b").inner_text() == "Ann Lee"
    assert await first.locator("span").inner_text() == "hello there"
    await page.locator("#quotes .quote").first.locator("button").click()
    assert await _quote_ids(page) == ["1"]
    await page.locator('[data-id="2"]').hover()
    await _act(page, "2", "reply").click()
    await page.locator("#reply").fill("answer")
    await page.locator("#reply").press("Enter")
    await page.wait_for_function("document.getElementById('quotes').hidden")
    assert [c[:2] for c in calls] == [("send", T)]
    assert calls[0][2].count("<blockquote") == 2
    assert calls[0][2].index('itemid="1"') < calls[0][2].index('itemid="2"')
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Self"}
    await board.on_event(_event("NewMessage", T, **_m("9", _at(9), calls[0][2], **mine)))
    quoted = page.locator('[data-id="9"] .html blockquote.reply')
    await quoted.first.wait_for()
    assert await quoted.locator("strong").all_inner_texts() == ["Ann Lee", "Ann Lee"]


async def test_quote_list_stops_at_ten(chat: Pages) -> None:
    """011-AC11: an eleventh quote is not added and says why."""
    page = await chat.open(_chat(L))
    await _handled(page, "history")
    for i in range(10, 21):
        await _act(page, str(i), "reply").dispatch_event("click")
    assert await _quote_ids(page) == [str(i) for i in range(10, 20)]
    assert await page.locator("#err").inner_text() == "10 quotes at most"


async def test_react_toggles_my_reaction(board: W.Board, chat: Pages, calls: list[tuple[Any, ...]]) -> None:
    """011-AC12: the picker offers the 8 keys; one I already gave is sent with `remove: true`."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    picker = page.locator('[data-id="1"] .picker')
    assert await picker.locator("button").evaluate_all("bs => bs.map(b => b.dataset.key)") == list(W.EMOJI)
    await picker.locator("[data-key=like]").click()
    await _until(calls, 1)
    mine = {"emotions": [{"key": "like", "users": [{"mri": ME}]}]}
    partial = {k: v for k, v in _m("1", _at(1)).items() if k != "content"}
    await board.on_event(_event("MessageUpdate", T, **{**partial, "properties": mine}))
    await page.wait_for_selector('[data-id="1"] .reacts span.mine')
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    await page.locator('[data-id="1"] .picker [data-key=like]').click()
    await _until(calls, 2)
    assert calls == [("react", T, "1", "like", False), ("react", T, "1", "like", True)]


async def test_edit_and_delete_only_on_my_messages(chat: Pages) -> None:
    """011-AC13 + 011-AC14: Edit and Delete show on my message only; Reply and React on every one."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    for act, owners in (
        ("edit", ["3"]),
        ("delete", ["3"]),
        ("reply", ["1", "2", "3"]),
        ("react", ["1", "2", "3"]),
    ):
        found = await page.locator(f".msg:has([data-act={act}])").evaluate_all(
            "ns => ns.map(n => n.dataset.id)"
        )
        assert found == owners, act


async def test_edit_escape_cancels_enter_saves_errors_stay_inline(
    chat: Pages, calls: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC13: the body turns into its plain text; Escape restores it, Enter saves once, a refusal
    shows under the field with the text kept."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.locator('[data-id="3"]').hover()
    await _act(page, "3", "edit").click()
    box = page.locator('[data-id="3"] .editor')
    assert await box.input_value() == "mine"
    await page.locator("#reply").hover()
    assert await page.locator('[data-id="3"] .actions').is_hidden()  # no bar over an open editor
    await box.press("Escape")
    assert await page.locator('[data-id="3"] .editor').count() == 0
    assert await page.locator('[data-id="3"] .html').inner_text() == "mine"
    await page.locator('[data-id="3"]').hover()
    await _act(page, "3", "edit").click()
    await box.fill("mine v2")
    await box.press("Shift+Enter")
    await box.press_sequentially("more")
    await box.press("Enter")
    await page.wait_for_selector('[data-id="3"] .html')  # the ack closes the editor
    assert calls == [("edit", T, "3", "mine v2\nmore")]

    monkeypatch.setattr(W, "edit_message", _refused)
    await page.locator('[data-id="3"]').hover()
    await _act(page, "3", "edit").click()
    await box.fill("kept")
    await box.press("Enter")
    await page.wait_for_function("document.querySelector('[data-id=\"3\"] .edit-err')?.textContent")
    assert await page.locator('[data-id="3"] .edit-err').inner_text() == "Teams answered 403"
    assert await box.input_value() == "kept"
    assert not await box.is_disabled()


async def test_delete_asks_first(chat: Pages, calls: list[tuple[Any, ...]]) -> None:
    """011-AC14: a dismissed confirm sends nothing; an accepted one sends `delete` once."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    for accept in (False, True):
        page.once("dialog", lambda d, accept=accept: d.accept() if accept else d.dismiss())
        since = await _frames(page)
        await page.locator('[data-id="3"]').hover()
        await _act(page, "3", "delete").click()
        if accept:
            await _handled(page, "delete", since)
    assert calls == [("delete", T, "3")]


_MINE = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Self"}


def _refused(*args: Any) -> None:
    request = httpx.Request("PUT", "https://x")
    raise httpx.HTTPStatusError("no", request=request, response=httpx.Response(403, request=request))


async def _edit(page: Page, msg_id: str) -> Any:
    await page.locator(f'[data-id="{msg_id}"]').hover()
    await _act(page, msg_id, "edit").click()
    return page.locator(f'[data-id="{msg_id}"] .editor')


async def test_edit_starts_from_the_text_without_its_reply_quote(board: W.Board, chat: Pages) -> None:
    """011-AC13: quotes are lost on edit: the field holds only what I wrote."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    body = W.reply_quote(_m("1", _at(1), "<p>hello there</p>"), "Ann Lee") + "<p>my answer</p><p>second</p>"
    await board.on_event(_event("NewMessage", T, **_m("9", _at(9), body, **_MINE)))
    await page.wait_for_selector('[data-id="9"] blockquote.reply')
    assert await (await _edit(page, "9")).input_value() == "my answer\n\nsecond"


async def test_live_frames_during_an_edit(board: W.Board, chat: Pages) -> None:
    """011-AC7 + 011-AC13: an edit frame neither closes the editor nor reads as its ack, Escape then shows
    the new body; a delete echo closes the editor on the deleted message."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    box = await _edit(page, "3")
    await box.fill("draft")
    since = await _frames(page)
    edited = {"edittime": "1789466500000"}
    await board.on_event(
        _event("MessageUpdate", T, **_m("3", _at(3), "<p>mine v3</p>", properties=edited, **_MINE))
    )
    await _handled(page, "edit", since)
    assert await box.input_value() == "draft"
    assert (await page.locator("#err").inner_text(), await page.locator(".edit-err").inner_text()) == ("", "")
    await box.press("Escape")
    assert await page.locator('[data-id="3"] .html').inner_text() == "mine v3"
    assert await page.locator('[data-id="3"] .edited').count() == 1
    box = await _edit(page, "3")
    gone = {"deletetime": "1789466600000"}
    await board.on_event(
        _event("MessageUpdate", T, **{**_m("3", _at(3), "x", **_MINE), "content": "", "properties": gone})
    )
    await page.wait_for_selector('[data-id="3"].deleted')
    assert await page.locator(".editor").count() == 0
    assert await _act(page, "3", "edit").count() == 0


async def test_an_open_editor_survives_a_reload_of_the_page_of_messages(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC13: after a reconnect the editor comes back with its text, or says it is lost."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await (await _edit(page, "3")).fill("kept across")
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "history", since)
    assert await page.locator('[data-id="3"] .editor').input_value() == "kept across"
    real_read = W.read_page
    monkeypatch.setattr(W, "read_page", lambda *a: ([m for m in real_read(*a)[0] if m["id"] != "3"], False))
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "history", since)
    assert await page.locator(".editor").count() == 0
    assert await page.locator("#err").inner_text() == "edit lost: the message is no longer shown"


async def test_a_save_cut_by_a_reconnect_can_be_retried_and_the_composer_keeps_focus(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC13: a save whose answer died with the socket says so and can be sent again; restoring the
    editor does not take the focus from the composer."""
    entered, release = threading.Event(), threading.Event()
    saved: list[str] = []

    def first_held(*args: Any) -> dict[str, Any]:
        saved.append(args[-1])
        if len(saved) == 1:
            entered.set()
            release.wait(5)
        return {"status": 200}

    monkeypatch.setattr(W, "edit_message", first_held)
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    box = await _edit(page, "3")
    await box.fill("first try")
    await box.press("Enter")
    assert await asyncio.to_thread(entered.wait, 5)
    await page.locator("#reply").focus()
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "history", since)
    release.set()
    assert await page.evaluate("document.activeElement.id") == "reply"
    assert await box.input_value() == "first try"
    assert await page.locator('[data-id="3"] .edit-err').inner_text() == (
        "connection lost: check the message before saving again"
    )
    await box.fill("second try")
    await box.press("Enter")
    await page.wait_for_selector('[data-id="3"] .html')
    assert saved == ["first try", "second try"]


async def test_escape_during_a_save_then_its_failure_lands_on_the_composer_line(
    chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC13: the field stays usable while saving; a failure with no editor left still shows."""
    entered, release = threading.Event(), threading.Event()

    def held(*args: Any) -> None:
        entered.set()
        release.wait(5)
        _refused()

    monkeypatch.setattr(W, "edit_message", held)
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    box = await _edit(page, "3")
    await box.fill("try")
    await box.press("Enter")
    assert await asyncio.to_thread(entered.wait, 5)
    assert not await box.is_disabled()
    await box.press("Escape")
    assert await page.locator(".editor").count() == 0
    release.set()
    await page.wait_for_function("document.getElementById('err').textContent !== ''")
    assert await page.locator("#err").inner_text() == "edit: Teams answered 403"


async def test_actions_reachable_from_the_keyboard_and_picker_closes(chat: Pages) -> None:
    """011-AC12: a message reached by Tab shows its actions, Tab reaches labelled buttons, a click does
    not pin the bar; the picker closes when the pointer leaves the message and on Escape."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    for _ in range(5):  # Tab, not focus(): the bar follows keyboard focus (focus-visible) only
        await page.keyboard.press("Tab")
        if await page.evaluate("document.activeElement.dataset.id") == "1":
            break
    assert await page.locator('[data-id="1"] .actions').is_visible()
    await page.keyboard.press("Tab")
    focused = await page.evaluate("[document.activeElement.dataset.act, document.activeElement.ariaLabel]")
    assert focused == ["reply", "reply with quote"]
    await page.keyboard.press("Tab")
    await page.keyboard.press("Enter")
    picker = page.locator('[data-id="1"] .picker')
    assert await picker.is_visible()
    assert await picker.locator("[data-key=like]").get_attribute("aria-label") == "like"
    await page.keyboard.press("Escape")
    assert await picker.is_hidden()
    await page.locator("#reply").click()  # keyboard focus left the bar; the mouse path starts clean
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    assert await picker.is_visible()
    await page.locator("#reply").hover()
    assert await picker.is_hidden()
    assert await page.locator('[data-id="1"] .actions').is_hidden()  # a clicked button does not pin it
    await page.locator('[data-id="2"] .html').click()  # a click focuses the message, not its bar
    await page.locator("#reply").hover()
    assert await page.locator('[data-id="2"] .actions').is_hidden()


# --- widget middle click (011 phase 3) ---


@dataclass
class Widget:
    board: W.Board
    pages: Pages
    verbs: list[Verb]
    page: Page | None = None

    @property
    def base(self) -> str:
        return f"http://{self.pages.host}"

    @property
    def ctx(self) -> BrowserContext:
        assert self.page is not None
        return self.page.context

    async def load(self, browser: bool, path: str = "/", midclick: str = "") -> Page:
        self.board.browser = ["firefox"] if browser else None
        if self.page is None:
            self.page = await self.pages.open(path)
        if midclick:
            await self.page.evaluate(f"localStorage.setItem('midclick', {json.dumps(midclick)})")
        await self.page.goto(self.base + path)
        await self.page.wait_for_selector("a.name")  # rows, browser and opener arrive in one frame
        return self.page

    async def act(self, click: Callable[[], Awaitable[None]]) -> tuple[list[Verb], list[str]]:
        """Verbs sent and tabs opened by `click`. A barrier verb closes the window: the socket is
        ordered, so once it arrived, any verb `click` sent did too."""
        assert self.page is not None
        self.verbs.clear()
        await click()
        await self.page.evaluate("window.__ws.send(JSON.stringify({open: 'barrier'}))")

        async def barrier() -> None:
            while _BARRIER not in self.verbs:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(barrier(), 5)
        assert self.verbs[-1] == _BARRIER
        tabs = []
        for tab in self.ctx.pages[1:]:
            tabs.append(tab.url)
            await tab.close()
        return self.verbs[:-1], tabs

    async def opens_tab(self, url: str, **click: Any) -> list[Verb]:
        """Verbs sent by a click that must open a tab on `url` (an anchor's own tab skips ctx.route)."""
        assert self.page is not None
        page = self.page

        async def clicked() -> None:
            async with self.ctx.expect_page() as tab:
                await page.locator("a.name").click(**click)
            await (await tab.value).wait_for_url(url)

        verbs, tabs = await self.act(clicked)
        assert tabs == [url]
        return verbs


@pytest.fixture
async def widget(settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Widget]:
    row = {
        "id": T,
        "label": "Fictional chat",
        "last_activity": _at(1),
        "last_id": "1789466400000",
        "typing": [],
    }
    board = W.Board({T: row}, None)
    board.opener = ["xdg-open"]
    verbs: list[Verb] = []

    async def spy(
        thread_id: str, web: bool = False, ws: Any = None, msg_id: str = "", chat: bool = False
    ) -> bool:
        verbs.append((thread_id, "chat" if chat else web))
        return True

    monkeypatch.setattr(board, "open_row", spy)
    async with _served(board, settings) as host:
        pages.host = host
        # The row's Teams link becomes a 404 of our own server: an anchor's default tab stays local.
        monkeypatch.setattr(W, "deep_link", lambda thread_id, msg_id="", scheme="https": f"/teams/{msg_id}")
        yield Widget(board, pages, verbs)
    assert pages.external == []


def _teams_tab(widget: Widget) -> str:
    return f"{widget.base}/teams/1789466400000"


@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_middle_click_default_is_unchanged(widget: Widget, browser: bool) -> None:
    """011-AC1: with the setting at `teams` (default), `open`+`web` with --browser, else the plain link."""
    page = await widget.load(browser)
    if browser:
        assert await widget.act(lambda: page.locator("a.name").click(button="middle")) == ([(T, True)], [])
    else:
        assert await widget.opens_tab(_teams_tab(widget), button="middle") == []


@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_middle_click_chat_setting_opens_the_chat_page(widget: Widget, browser: bool) -> None:
    """011-AC1: the ⚙ `chat` setting persists; middle click sends `open`+`chat` through --browser
    when set, else opens /chat/<id> in a tab and sends no verb."""
    page = await widget.load(browser)
    await page.click("#gear")
    await page.get_by_role("button", name="Chat here").click()
    assert await page.evaluate("localStorage.getItem('midclick')") == "chat"
    await widget.load(browser)
    if browser:
        assert await widget.act(lambda: page.locator("a.name").click(button="middle")) == ([(T, "chat")], [])
    else:
        assert await widget.opens_tab(widget.base + _chat(T), button="middle") == []
    # A browser that also fires `click` for button 1 must not add a verb or a second tab. dispatchEvent
    # returns false only when the default (the link's own tab) was prevented: no tab can come later.
    script = "new MouseEvent('click', {button: 1, bubbles: true, cancelable: true})"
    dispatch = f"document.querySelector('a.name').dispatchEvent({script})"
    dispatched: list[bool] = []

    async def click() -> None:
        dispatched.append(await page.evaluate(dispatch))

    assert await widget.act(click) == ([], [])
    assert dispatched == [False]


@pytest.mark.parametrize("setting", ["teams", "chat"])
@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_ctrl_and_plain_click_are_unchanged(widget: Widget, setting: str, browser: bool) -> None:
    """011-AC2: Ctrl click and plain click do the same whatever the middle-click setting."""
    page = await widget.load(browser, midclick=setting)
    if browser:
        ctrl = await widget.act(lambda: page.locator("a.name").click(modifiers=["Control"]))
        assert ctrl == ([(T, True)], [])
    else:
        assert await widget.opens_tab(_teams_tab(widget), modifiers=["Control"]) == []
    assert await widget.act(lambda: page.locator("a.name").click()) == ([(T, False)], [])


async def test_midclick_url_param_overrides_the_stored_choice(widget: Widget) -> None:
    """011-AC1: `?midclick=teams` wins over a stored `chat`, without replacing it."""
    page = await widget.load(True, "/?midclick=teams", midclick="chat")
    assert await widget.act(lambda: page.locator("a.name").click(button="middle")) == ([(T, True)], [])
    assert await page.evaluate("localStorage.getItem('midclick')") == "chat"
