"""Chat page and widget in a headless browser (spec 011). Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser.py` (playwright stays out of the project deps).
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import browser_helpers
import httpx
import pytest
import test_web_chat
from browser_helpers import (  # skips without playwright
    CONTRAST,
    PNG,
    BrowserContext,
    Page,
    Pages,
    Route,
    rendered,
    row,
    until,
    wait,
)
from test_web_chat import ANN, ME, OBJ, C, T, _archive, _at, _event, _m, _served, _until

from miniteams import archive_store, compose
from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.archive_store import ChatStore, find_chat_dir
from miniteams.config import Settings
from miniteams.messages import EMOJI

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
# test_web_chat's fixtures; `board` builds on this module's `data_dir`.
board, calls, launched, quiet_directory, sent = (
    test_web_chat.board,
    test_web_chat.calls,
    test_web_chat.launched,
    test_web_chat.quiet_directory,
    test_web_chat.sent,
)

L = "19:llll@thread.v2"  # archived, long enough for the list to scroll
X = "19:xxxx@thread.v2"  # archived, several pages long
LONG = 250
D = "19:dddd@thread.v2"  # archived, messages on several days
K = "19:kkkk@thread.v2"  # archived, a bot card
R = "19:rrrr@thread.v2"  # archived, deleted runs and reaction chips
GONE = {"deletetime": "1789466600000"}
# An `open` the spy records, not browser_helpers.BARRIER: the real board drops an unknown verb unseen.
_BARRIER = ("barrier", False)
# (thread, web) for an `open` verb; (thread, "chat") when it asks for our own chat page.
Verb = tuple[str, bool | str]
_LIST = "document.getElementById('list')"
_AT_BOTTOM = f"(l => l.scrollHeight - l.scrollTop - l.clientHeight < 40)({_LIST})"


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
    far = {5, 245}  # two search hits more than a page apart
    long = [_m(str(i), _at(i), f"<p>x{i:03d}{' far' if i in far else ''}</p>") for i in range(LONG)]
    long[160] = _m(
        "160", _at(160), "<p>x160</p>", **mine
    )  # an editable message near the top of the latest page
    _archive(path, X, long)
    days = [
        ("a", "2026-09-10T12:00:00.000Z"),
        ("e", "2026-09-10T22:30:00.000Z"),
        ("b", "2026-09-11T08:00:00.000Z"),
    ]
    _archive(path, D, [_m(i, t) for i, t in days])
    card = {"contentType": "application/vnd.microsoft.card.adaptive", "content": test_web_chat._BUILD_CARD}
    my_card = {
        **test_web_chat._swift_message({"attachments": [card]}),
        "id": "2",
        "composetime": _at(2),
        **mine,
    }
    text_card = {"cards": [{"content": test_web_chat._card({"type": "TextBlock", "text": "CARDTEXT"})}]}
    mine_html = _m("3", _at(3), "<p>hello</p>", properties=text_card, **mine)
    _archive(path, K, [test_web_chat._swift_message({"attachments": [card]}), my_card, mine_html])
    reacts = {
        "emotions": [
            {"key": "like", "users": [{"mri": ME}]},
            {"key": "heart", "users": [{"mri": ANN}]},
            {"key": "party", "users": [{"mri": ANN}]},
            {"key": "toString", "users": [{"mri": ANN}]},
        ]
    }
    _archive(path, C, [_m("1", _at(1), properties=reacts)])
    deleted = [_m(i, _at(int(i)), properties=GONE, **(mine if i == "3" else {})) for i in "234"]
    others = [_m(str(i), _at(i), properties=GONE if i == 7 else {}) for i in range(5, 21)]
    _archive(path, R, [_m("1", _at(1), properties=reacts), *deleted, *others])
    return path


@pytest.fixture
async def chat(
    board: W.Board, settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Pages]:
    # Live messages the archive lacks make the latest page ask the API (test_browser_lag): it has none.
    monkeypatch.setattr(CV, "fetch_history", lambda *a: [])
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield pages
        await pages.close()
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
    assert await page.locator(".msg.me .reacts button").inner_text() == "👍 1"
    assert await page.locator("#back").get_attribute("href") == "/?nav=chat"  # 011-AC20, list mode


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
    await page.wait_for_selector('[data-id="4"] .reacts button.mine')
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


async def test_a_system_line_shows_who_and_its_time(board: W.Board, chat: Pages) -> None:
    """A live thread activity line names who did what and shows its time, the full timestamp on hover."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    added = f"<addmember><initiator>{ME}</initiator><target>{ANN}</target></addmember>"
    activity = {"messagetype": "ThreadActivity/AddMember", "from": "", "imdisplayname": ""}
    await board.on_event(_event("NewMessage", T, **_m("4", _at(4), added, **activity)))
    line = page.locator('[data-id="4"].system')
    await line.wait_for()
    assert await line.locator(".html").inner_text() == f"👥 You added {ANN.split(':')[-1]}"
    when = line.locator(".when")
    assert await when.is_visible() and await when.get_attribute("title") == _at(4)
    assert await when.inner_text() == await page.locator(".msg:not(.system) .when").first.inner_text()


async def test_times_read_24h_whatever_the_locale(board: W.Board, chat: Pages) -> None:
    """The widget runs Chromium in en-US: a 15:04 message must not read "03:04 PM"."""
    page = await chat.open(_chat(T), locale="en-US", timezone_id="UTC")
    await _handled(page, "history")
    await board.on_event(_event("NewMessage", T, **_m("4", "2026-09-15T15:04:00.000Z", "late")))
    when = page.locator('[data-id="4"] .when')
    await when.wait_for()
    text = await when.inner_text()
    assert text.endswith("15:04") and "M" not in text.upper().replace("SEP", ""), text


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

    def failing(*args: Any, **kw: Any) -> None:
        raise httpx.ConnectError("boom", request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(CV, "send_message", failing)
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
    monkeypatch.setattr(CV, "sanitize", lambda content, *a: content)
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
    real_read = CV.read_page

    def held_read(*args: Any, **kwargs: Any) -> Any:
        reading.set()
        release.wait(5)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(CV, "read_page", held_read)
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
    assert await page.locator("#reply").get_attribute("placeholder") == "read-only chat"
    await page.wait_for_selector('[data-id="1"] .reacts')
    assert await page.locator(".reacts button").count() == 0  # 011-AC26: no react on a channel
    assert await page.locator(".reacts span").count() == 4


async def test_malformed_chat_address_is_guarded(chat: Pages) -> None:
    """The page refuses an address it cannot decode (no AC; served by a stub: the server 404s it)."""
    html = W._page("chat.html")
    headers = {"Content-Type": "text/html", "Content-Security-Policy": W._page_csp(html)}
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
    monkeypatch.setattr(CV, "fetch_history", offline)
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
    """011-AC12, 016-AC2: the picker offers Teams' 6 defaults plus 🙏 and 🙃, sent with Teams' names;
    one I already gave is sent with `remove: true`."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    picker = page.locator('[data-id="1"] .picker')
    keys = ["like", "heart", "laugh", "surprised", "sad", "angry", "praying", "upsidedownface"]
    assert await picker.locator("button").all_inner_texts() == [*(EMOJI[k] for k in keys), "…"]
    await picker.locator('[data-fav="👍"]').click()
    await _until(calls, 1)
    mine = {"emotions": [{"key": "like", "users": [{"mri": ME}]}]}
    partial = {k: v for k, v in _m("1", _at(1)).items() if k != "content"}
    await board.on_event(_event("MessageUpdate", T, **{**partial, "properties": mine}))
    await page.wait_for_selector('[data-id="1"] .reacts button.mine')
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    await page.locator('[data-id="1"] .picker [data-fav="👍"]').click()
    await _until(calls, 2)
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    await page.locator('[data-id="1"] .picker [data-fav="🙏"]').click()
    await _until(calls, 3)
    assert calls == [
        ("react", T, "1", "like", False),
        ("react", T, "1", "like", True),
        ("react", T, "1", "praying", False),
    ]


async def test_the_page_knows_every_accepted_reaction_key(
    board: W.Board, chat: Pages, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC12, 016-AC4: every messages.EMOJI key is a toggling chip, 🙏 included."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    theirs = {"emotions": [{"key": k, "users": [{"mri": "8:orgid:someone"}]} for k in EMOJI]}
    partial = {k: v for k, v in _m("1", _at(1)).items() if k != "content"}
    await board.on_event(_event("MessageUpdate", T, **{**partial, "properties": theirs}))
    chips = page.locator('[data-id="1"] .reacts button')
    shown = "document.querySelectorAll('[data-id=\"1\"] .reacts button').length"
    await page.wait_for_function(f"{shown} === {len(EMOJI)}")
    assert await chips.all_inner_texts() == [f"{EMOJI[k]} 1" for k in EMOJI]
    await page.locator('[data-id="1"] .reacts button', has_text="🙏").click()
    await _until(calls, 1)
    assert calls == [("react", T, "1", "praying", False)]


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

    monkeypatch.setattr(CV, "edit_message", _refused)
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
    body = (
        compose.reply_quote(_m("1", _at(1), "<p>hello there</p>"), "Ann Lee")
        + "<p>my answer</p><p>second</p>"
    )
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
    real_read = CV.read_page
    without_3 = lambda *a, **k: [m for m in real_read(*a, **k).messages if m["id"] != "3"]  # noqa: E731
    monkeypatch.setattr(CV, "read_page", lambda *a, **k: archive_store.Page(without_3(*a, **k), False, False))
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

    monkeypatch.setattr(CV, "edit_message", first_held)
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

    monkeypatch.setattr(CV, "edit_message", held)
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
    for _ in range(12):  # Tab, not focus(): the bar follows keyboard focus (focus-visible) only
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
    assert await picker.locator('[data-fav="👍"]').get_attribute("aria-label") == "👍"
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


# --- scroll, latest button, search, jump (011 phase 5) ---

# The page's outgoing verbs land in window.__sent (its current socket only).
_SPY = """(() => { const ws = window.__ws, real = ws.send.bind(ws); window.__sent = [];
  ws.send = (d) => { window.__sent.push(JSON.parse(d)); real(d); }; })()"""
_OFFSET = """id => { const l = document.getElementById('list'),
  n = document.querySelector(`#msgs [data-id="${id}"]`);
  return n ? n.getBoundingClientRect().top - l.getBoundingClientRect().top : null; }"""
_VIEW = {"viewport": {"width": 700, "height": 400}}


async def _scroll(page: Page, to: str) -> None:
    """Scroll the list; the event is dispatched so the page reads its position in the handler."""
    await page.evaluate(f"(l => {{ l.scrollTop = {to}; l.dispatchEvent(new Event('scroll')); }})({_LIST})")


async def _history_sent(page: Page) -> list[dict[str, Any]]:
    return list(await page.evaluate("window.__sent.filter(v => 'history' in v)"))


async def _ids(page: Page) -> list[str]:
    return list(await page.locator("#msgs .msg").evaluate_all("ns => ns.map(n => n.dataset.id)"))


def _slow_pages(monkeypatch: pytest.MonkeyPatch, wait: float = 0.3) -> None:
    real = CV.read_page

    def slow(*args: Any, **kwargs: Any) -> Any:
        threading.Event().wait(wait)
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "read_page", slow)


async def _open_long(chat: Pages) -> Page:
    page = await chat.open(_chat(X), **_VIEW)
    await _handled(page, "history")
    await page.evaluate(_SPY)
    return page


async def test_scroll_to_top_loads_older_once_and_keeps_the_place(
    chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC15: the top of the list asks the previous page once while it is read, the message in view
    stays where it was, and the page stops asking at `older: false`."""
    page = await _open_long(chat)
    assert await _ids(page) == [str(i) for i in range(150, LONG)]
    _slow_pages(monkeypatch)
    await _scroll(page, "0")
    before = await page.evaluate(_OFFSET, "150")
    for _ in range(3):  # still at the top while the page is read: no second request
        await _scroll(page, "0")
    since = await _frames(page)
    await _handled(page, "history", since)
    assert await _history_sent(page) == [{"history": X, "before": _at(150)}]
    assert len(await _ids(page)) == 200
    assert abs(await page.evaluate(_OFFSET, "150") - before) < 2
    await _scroll(page, "0")
    await page.wait_for_function("document.querySelectorAll('#msgs .msg').length === 250")
    assert (await _ids(page))[0] == "0"
    await _scroll(page, "0")  # the page's scroll handler runs inside this call
    assert len(await _history_sent(page)) == 2  # older: false, nothing more to ask


async def test_latest_button_counts_live_messages_and_returns_to_the_bottom(
    board: W.Board, chat: Pages
) -> None:
    """011-AC16: scrolled up, "↓ latest" shows with the count of live messages; a click lands on the
    newest."""
    page = await _open_long(chat)
    assert await page.locator("#latest").is_hidden()
    await _scroll(page, "l.scrollHeight / 2")
    await page.wait_for_selector("#latest:visible")
    assert await page.locator("#latest").inner_text() == "↓ latest"
    for i in (900, 901):
        await board.on_event(_event("NewMessage", X, **_m(str(i), _at(i))))
    await page.wait_for_function("document.getElementById('latest').textContent === '↓ latest (2)'")
    assert (await _ids(page))[-1] == "901"  # attached: they are on the page, below the reader
    await page.click("#latest")
    await page.wait_for_function(_AT_BOTTOM)
    assert await page.locator("#latest").is_hidden()
    assert await _history_sent(page) == []  # attached: no reload needed


async def _search(page: Page, q: str) -> None:
    await page.fill("#find", q)
    await page.press("#find", "Enter")
    await page.wait_for_selector("#hits:visible")


async def _in_view(page: Page, msg_id: str) -> bool:
    return bool(
        await page.evaluate(
            """id => { const l = document.getElementById('list').getBoundingClientRect(),
              n = document.querySelector(`#msgs [data-id="${id}"]`).getBoundingClientRect();
              return n.top >= l.top - 1 && n.bottom <= l.bottom + 1; }""",
            msg_id,
        )
    )


async def test_search_hit_shows_the_message_highlighted_with_its_neighbours(chat: Pages) -> None:
    """011-AC18: a hit, clicked, loads the page around it and shows it highlighted, in view."""
    page = await _open_long(chat)
    await _search(page, "X02")  # case folded: x020..x029
    hits = page.locator("#hits button")
    assert await hits.evaluate_all("bs => bs.map(b => b.dataset.id)") == [str(i) for i in range(29, 19, -1)]
    since = await _frames(page)
    await page.locator('#hits button[data-id="20"]').click()
    await _handled(page, "history", since)
    assert await page.locator("#hits").is_hidden()
    assert (await _history_sent(page))[-1] == {"history": X, "around": _at(20)}
    assert await page.locator("#msgs .msg.hit").evaluate_all("ns => ns.map(n => n.dataset.id)") == ["20"]
    assert await _in_view(page, "20")
    ids = await _ids(page)
    assert (ids[0], ids[-1]) == ("0", "99")  # 20 before (all there is), the rest of the page from the hit on
    assert await page.locator("#latest").is_visible()  # detached: the latest message is not loaded


async def test_search_errors_show_in_the_results(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC18: a chat without archive says so where the results would be."""
    b = test_web_chat.B
    board.rows[b] = {"id": b, "label": "B", "last_activity": _at(1), "typing": []}
    monkeypatch.setattr(CV, "fetch_history", lambda *a: [])
    page = await chat.open(_chat(b))
    await _handled(page, "history")
    await _search(page, "x")
    assert await page.locator("#hits .err").inner_text() == "no archive for this chat"


async def _jump_to(page: Page, msg_id: str) -> None:
    await _search(page, f"x{int(msg_id):03d}")
    since = await _frames(page)
    await page.locator(f'#hits button[data-id="{msg_id}"]').click()
    await _handled(page, "history", since)


async def test_after_a_jump_scrolling_down_loads_newer(board: W.Board, chat: Pages) -> None:
    """011-AC16 + 011-AC17: detached by a jump, the bottom asks `after` until `newer` is false; a live
    message meanwhile only bumps the count, then follows the latest message once it is reached."""
    page = await _open_long(chat)
    await _jump_to(page, "20")
    await board.on_event(_event("NewMessage", X, **_m("900", _at(900))))
    await page.wait_for_function("document.getElementById('latest').textContent === '↓ latest (1)'")
    assert "900" not in await _ids(page)
    for last in ("199", "900"):  # the last page reaches the latest message: the live one follows it
        await _scroll(page, "l.scrollHeight")
        await page.wait_for_function(
            f"document.querySelector('#msgs').lastElementChild.dataset.id === {json.dumps(last)}"
        )
    afters = [v["after"] for v in await _history_sent(page) if "after" in v]
    assert afters == [_at(99), _at(199)]
    ids = await _ids(page)
    assert ids == [*(str(i) for i in range(LONG)), "900"]  # no overlap, no gap
    await _scroll(page, "l.scrollHeight")
    await page.wait_for_selector("#latest", state="hidden")


async def test_latest_button_after_a_jump_reloads_the_latest_page(board: W.Board, chat: Pages) -> None:
    """011-AC16: after a jump, "↓ latest" loads the latest page and shows the newest message at the bottom,
    live ones received meanwhile included."""
    page = await _open_long(chat)
    await _jump_to(page, "20")
    await board.on_event(_event("NewMessage", X, **_m("900", _at(900))))
    await page.wait_for_function("document.getElementById('latest').textContent === '↓ latest (1)'")
    since = await _frames(page)
    await page.click("#latest")
    await _handled(page, "history", since)
    await page.wait_for_function(_AT_BOTTOM)
    assert (await _history_sent(page))[-1] == {"history": X, "before": None}
    ids = await _ids(page)
    assert (ids[0], ids[-2], ids[-1]) == ("150", "249", "900")
    assert await page.locator("#latest").is_hidden()
    await board.on_event(_event("NewMessage", X, **_m("901", _at(901))))
    await page.wait_for_function("document.querySelector('#msgs').lastElementChild.dataset.id === '901'")


async def test_jump_without_archive_says_so(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC17: a chat read from the API cannot jump; the page says why and keeps its messages."""
    b = test_web_chat.B
    board.rows[b] = {"id": b, "label": "B", "last_activity": _at(1), "typing": []}
    monkeypatch.setattr(CV, "fetch_history", lambda *a: [_m("1", _at(1))])
    page = await chat.open(_chat(b))
    await _handled(page, "history")
    await _search(page, "x")  # answered "no archive": a hit offered anyway still cannot jump
    hit = {"id": "1", "at": _at(1), "name": "Ann Lee", "text": "x"}
    await _inject(page, [{"search": b, "q": "x", "hits": [hit]}])
    await page.locator("#hits button").click()
    await page.wait_for_function("document.getElementById('err').textContent !== ''")
    assert await page.locator("#err").inner_text() == "jump: not available without archive"
    assert await _ids(page) == ["1"]


async def test_a_long_open_busy_chat_keeps_the_page_bounded(chat: Pages) -> None:
    """Deferred to phase 5: at the bottom, past 600 messages the oldest leave the page; scrolling up
    reloads them."""
    page = await _open_long(chat)
    # Straight into the page's handler: 520 relays through the server would only slow the test down.
    await _inject(page, [{"msg": _live(i), "thread": X} for i in range(520)])
    ids = await _ids(page)
    assert (len(ids), ids[0], ids[-1]) == (600, "170", "n519")
    assert await page.evaluate(_AT_BOTTOM)
    since = await _frames(page)
    await _scroll(page, "0")
    await _handled(page, "history", since)
    assert (await _history_sent(page))[-1] == {"history": X, "before": _at(170)}
    assert (await _ids(page))[:2] == ["70", "71"]


async def test_reconnect_anchors_on_the_message_in_view(board: W.Board, chat: Pages) -> None:
    """Deferred to phase 5: after a drop the message at the top of the view stays there, even when it
    came from an older page the latest one does not hold."""
    page = await _open_long(chat)
    await _scroll(page, "0")
    await page.wait_for_function("document.querySelectorAll('#msgs .msg').length === 200")
    await page.locator('#msgs [data-id="60"]').evaluate("n => n.scrollIntoView({ block: 'start' })")
    await page.evaluate(f"{_LIST}.scrollTop -= 7")
    await _scroll(page, "l.scrollTop")
    before = await page.evaluate(_OFFSET, "60")
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "history", since)
    assert abs(await page.evaluate(_OFFSET, "60") - before) < 2
    # The page around the first message in view (59, 7px above 60), not the latest one.
    assert (await _ids(page))[0] == "9"


async def _inject(page: Page, frames: list[dict[str, Any]]) -> None:
    """Frames straight into the page's socket handler, as if the server had sent them."""
    await page.evaluate(
        "fs => { for (const f of fs)"
        " window.__ws.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(f) })); }",
        frames,
    )


async def test_two_quick_jumps_land_on_the_second(chat: Pages, monkeypatch: pytest.MonkeyPatch) -> None:
    """011-AC18: the answer to a superseded jump is dropped; the last hit clicked lands highlighted."""
    page = await _open_long(chat)
    await _search(page, "far")
    _slow_pages(monkeypatch)
    since = await _frames(page)
    await page.evaluate(
        "for (const id of ['5', '245']) document.querySelector(`#hits button[data-id=\"${id}\"]`).click()"
    )
    await page.wait_for_function(
        f"window.__frames.slice({since}).some(f => f.around === {json.dumps(_at(245))} && f.messages)"
    )
    await page.wait_for_selector("#msgs .msg.hit")
    assert await page.locator("#msgs .msg.hit").evaluate_all("ns => ns.map(n => n.dataset.id)") == ["245"]
    assert await _in_view(page, "245")
    await rendered(page)  # the landing's scroll event, handled
    assert [v for v in await _history_sent(page) if "after" in v or "before" in v] == []


async def test_a_short_page_asks_for_older_without_a_scroll(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC15: an API page too short to scroll still loads what is before it, until `older` is false."""
    b = test_web_chat.B
    board.rows[b] = {"id": b, "label": "B", "last_activity": _at(5), "typing": []}
    asked: list[int | None] = []

    def three(settings: Any, token: str, thread: str, size: int, pages: int, end: int | None = None) -> Any:
        asked.append(end)
        return [_m(str(i), _at(i)) for i in (3, 4, 5)]

    monkeypatch.setattr(CV, "fetch_history", three)
    page = await chat.open(_chat(b))
    await page.wait_for_function("window.__frames.filter(f => 'history' in f).length === 2")
    assert len(asked) == 2 and asked[1] is not None  # the latest page, then `before` its first message
    await rendered(page)  # a third ask would go out while the second answer is handled, or on a scroll
    assert len(await page.evaluate("window.__out.filter(v => 'history' in v)")) == 2  # older false, no more


async def test_a_failed_older_page_is_asked_again_after_a_reconnect(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC15: on a page too short to scroll, one failed `before` does not stop older history for
    good: the next latest page (a reconnect) asks it again and the older message shows."""
    b = test_web_chat.B
    board.rows[b] = {"id": b, "label": "B", "last_activity": _at(5), "typing": []}
    befores: list[int] = []

    def flaky(settings: Any, token: str, thread: str, size: int, pages: int, end: int | None = None) -> Any:
        if end is None:
            return [_m(str(i), _at(i)) for i in (2, 3)]
        befores.append(end)
        if len(befores) == 1:
            raise RuntimeError("odd")
        return [_m("1", _at(1))]

    monkeypatch.setattr(CV, "fetch_history", flaky)
    page = await chat.open(_chat(b))
    await page.wait_for_function("window.__frames.some(f => f.error === 'odd')")
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "follow", since)
    await page.wait_for_selector('[data-id="1"]', timeout=3000)
    assert befores[1] == befores[0]  # the failed edge, asked again


async def test_an_empty_page_that_says_older_is_asked_once(
    chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC15: a short page whose older page comes back empty (yet `older`) does not ask it again."""
    real = CV.read_page
    befores: list[str] = []

    def lying(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("before"):
            befores.append(kwargs["before"])
            return archive_store.Page([], True, False)
        messages, _, newer, _ = real(*args, **kwargs)
        return archive_store.Page(messages, True, newer)

    monkeypatch.setattr(CV, "read_page", lying)
    page = await chat.open(_chat(T))
    await page.wait_for_function("window.__frames.filter(f => 'history' in f).length === 2")
    await rendered(page)  # a third ask would go out while the second answer is handled, or on a scroll
    assert len(await page.evaluate("window.__out.filter(v => 'history' in v)")) == 2
    assert befores == [_at(1)]


async def test_a_failed_reconnect_page_falls_back_to_the_latest(
    board: W.Board, chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred to phase 5: when the page around the reader's message fails, the latest page loads."""
    page = await _open_long(chat)
    await _scroll(page, "l.scrollHeight / 2")
    real = CV.read_page

    def no_around(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("around"):
            raise ValueError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "read_page", no_around)
    since = await _frames(page)
    await _drop(board)
    await page.wait_for_function(
        f"window.__frames.slice({since}).some(f => 'history' in f && f.before === null && f.messages)"
    )
    await page.wait_for_function(_AT_BOTTOM)
    assert (await _ids(page))[-1] == "249"


async def test_an_edit_while_detached_reaches_the_held_message(board: W.Board, chat: Pages) -> None:
    """011-AC7 + 011-AC16: a live message held while detached shows its edit once the latest page loads."""
    page = await _open_long(chat)
    await _jump_to(page, "20")
    await board.on_event(_event("NewMessage", X, **_m("900", _at(900), "<p>first</p>")))
    edit = {"edittime": "1789466500000"}
    await board.on_event(_event("MessageUpdate", X, **_m("900", _at(900), "<p>second</p>", properties=edit)))
    await page.wait_for_function("document.getElementById('latest').textContent === '↓ latest (1)'")
    await page.click("#latest")
    await page.wait_for_selector('[data-id="900"] .edited')
    assert await page.locator('[data-id="900"] .html').inner_text() == "second"


async def test_a_search_answer_after_escape_stays_closed(
    chat: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC18: closing the results before the answer drops that search."""
    real = CV.search_chat

    def slow(*args: Any, **kwargs: Any) -> Any:
        threading.Event().wait(1.0)  # Escape must land first, even on a loaded host
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "search_chat", slow)
    page = await _open_long(chat)
    since = await _frames(page)
    await page.fill("#find", "x02")
    await page.press("#find", "Enter")
    await page.press("#find", "Escape")
    await _handled(page, "search", since)  # recorded once the page's handler ran
    assert await page.locator("#hits").is_hidden()


async def test_trim_keeps_an_open_editor(chat: Pages) -> None:
    """Deferred to phase 5: the trim stops at a message being edited, its text intact."""
    page = await _open_long(chat)
    await (await _edit(page, "160")).fill("half-way")
    await _scroll(page, "l.scrollHeight")
    await _inject(page, [{"msg": _live(i), "thread": X} for i in range(520)])
    ids = await _ids(page)
    assert (ids[0], len(ids)) == ("160", 610)
    assert await page.locator('[data-id="160"] .editor').input_value() == "half-way"


def _live(i: int) -> dict[str, Any]:
    return {
        "id": f"n{i}",
        "at": f"2026-09-15T11:{i // 60:02d}:{i % 60:02d}.000Z",
        "from": ANN,
        "name": "Ann Lee",
        "type": "text",
        "html": f"<p>n{i}</p>",
        "reactions": [],
    }


async def test_a_stale_after_answer_is_ignored(chat: Pages) -> None:
    """011-AC17: an `after` page for an edge the screen no longer has does not join it."""
    page = await _open_long(chat)
    await _jump_to(page, "20")
    before = await _ids(page)
    stale = {"history": X, "after": _at(10), "messages": [_live(1)], "older": True, "newer": False}
    await _inject(page, [stale])
    assert await _ids(page) == before


async def test_reconnect_while_detached_keeps_the_place_and_stays_detached(
    board: W.Board, chat: Pages
) -> None:
    """Deferred to phase 5: after a jump and a drop, the page around the reader's message comes back."""
    page = await _open_long(chat)
    await _jump_to(page, "20")
    await page.locator('#msgs [data-id="30"]').evaluate("n => n.scrollIntoView({ block: 'start' })")
    await _scroll(page, "l.scrollTop")
    before = await page.evaluate(_OFFSET, "30")
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "history", since)
    assert abs(await page.evaluate(_OFFSET, "30") - before) < 2
    assert "249" not in await _ids(page)
    assert await page.locator("#latest").is_visible()


# --- widget middle click (011 phase 3) ---


@dataclass
class Widget:
    board: W.Board
    pages: Pages
    verbs: list[Verb]
    page: Page | None = None
    part: str = "a.name"  # what the tests click: the row's title, or its preview line (the same target)

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

        await wait(lambda: _BARRIER in self.verbs, "barrier")
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
                await page.locator(self.part).click(**click)
            await (await tab.value).wait_for_url(url)

        verbs, tabs = await self.act(clicked)
        assert tabs == [url]
        return verbs


@pytest.fixture
async def widget(settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Widget]:
    board = W.Board({T: row(T, "Fictional chat", _at(1), last_id="1789466400000")}, None)
    board.opener = ["xdg-open"]
    verbs: list[Verb] = []

    async def spy(
        thread_id: str,
        target: str = "app",
        ws: Any = None,
        msg_id: str = "",
    ) -> bool:
        verbs.append((thread_id, "chat" if target in ("chat", "split") else target == "web"))
        return True

    monkeypatch.setattr(board, "open_row", spy)
    async with _served(board, settings) as host:
        pages.serve(host, board)
        # The row's Teams link becomes a 404 of our own server: an anchor's default tab stays local.
        monkeypatch.setattr(W, "deep_link", lambda thread_id, msg_id="", scheme="https": f"/teams/{msg_id}")
        yield Widget(board, pages, verbs)
        await pages.close()
    assert pages.external == []


async def test_widget_row_shows_read_and_more_and_its_menu_marks_seen(widget: Widget) -> None:
    """The widget's row: ✎ 👁 ⋯ inline; ⋯ opens a menu that a broadcast keeps, focus included; Mark seen
    reaches the board and closes it."""
    page = await widget.load(False)
    row = page.locator(f'li[data-id="{T}"]')
    await row.hover()
    labels = await row.locator(".meta button").evaluate_all("bs => bs.map(b => b.textContent)")
    assert labels == ["✎", "👁", "⋯"]
    await row.locator('[data-k="more"]').click()
    frames = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
    widget.board.broadcast()
    await page.wait_for_function(f"window.__frames.filter(f => 'rows' in f).length > {frames}")
    assert await page.evaluate("document.activeElement.dataset.k") == "seen"
    await row.locator('[data-k="seen"]').click()
    assert await page.locator(".rowmenu").count() == 0
    await wait(lambda: widget.board.rows[T]["seen_at"] == _at(1), "seen")


async def test_widget_row_menu_never_opens_under_the_sticky_header(widget: Widget) -> None:
    """L1: the window scrolls under a sticky header: a row just below it, with little room under it,
    opens its menu downwards, clickable, rather than up under the header."""
    for i in range(2, 14):
        widget.board.rows[f"19:r{i}@thread.v2"] = row(
            f"19:r{i}@thread.v2", f"Row {i}", _at(i), last_id=str(i)
        )
    page = await widget.load(False)
    li = page.locator("#rows > li").nth(6)
    hdr, top, height = await li.evaluate(
        "li => [document.querySelector('header').getBoundingClientRect().bottom,"
        " li.getBoundingClientRect().top + scrollY, li.offsetHeight]"
    )
    # 10px between the header and the row, 40px under it: neither side fits the menu, below has more.
    await page.set_viewport_size({"width": 600, "height": int(hdr + height + 50)})
    await page.evaluate(f"scrollTo(0, {top - hdr - 10})")
    await li.hover()
    await li.locator('[data-k="more"]').click()
    first = li.locator(".rowmenu button").first
    await first.wait_for()
    hit = "b => { const r = b.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;"
    hit += " return document.elementFromPoint(x, y) === b; }"
    assert await first.evaluate(hit)


async def test_widget_last_row_menu_opens_upwards_in_sight(widget: Widget) -> None:
    """The window scrolls the widget's list: the bottom row's menu opens upwards, inside the viewport,
    anchored above its ⋯."""
    for i in range(2, 14):
        widget.board.rows[f"19:r{i}@thread.v2"] = row(
            f"19:r{i}@thread.v2", f"Row {i}", _at(i), last_id=str(i)
        )
    page = await widget.load(False)
    await page.set_viewport_size({"width": 600, "height": 400})
    li = page.locator("#rows > li").last
    await li.scroll_into_view_if_needed()
    await li.hover()
    await li.locator('[data-k="more"]').click()
    await li.locator(".rowmenu button").first.wait_for()
    assert await li.evaluate("""li => {
      const menu = li.querySelector('.rowmenu'), m = menu.getBoundingClientRect(),
        b = li.querySelector('[data-k=more]').getBoundingClientRect();
      return menu.classList.contains('up') && m.bottom <= innerHeight && Math.abs(m.right - b.right) <= 1
        && Math.abs(b.top - m.bottom - 4) <= 1;
    }""")


@pytest.mark.parametrize("view", ["widget", "split"])
async def test_row_menu_stays_anchored_to_its_visible_trigger(
    split: Pages, board: W.Board, view: str
) -> None:
    """An open menu stays beside its trigger, including after hover/focus leave and a broadcast."""
    page = await split.open("/") if view == "widget" else await _open_split(split)
    await page.wait_for_selector("#rows > li")
    li = page.locator("#rows > li").first
    await li.hover()
    await li.locator('[data-k="more"]').click()
    await page.mouse.move(0, 0)
    # Removing focus must not hide the trigger while the menu still exists.
    assert await li.evaluate("""li => {
      const focused = document.activeElement;
      focused.blur();
      const visible = getComputedStyle(li.querySelector('[data-k=more]')).display !== 'none';
      focused.focus();
      return visible;
    }""")
    geometry = """li => {
      const menu = li.querySelector('.rowmenu'), more = li.querySelector('[data-k=more]');
      const m = menu.getBoundingClientRect(), b = more.getBoundingClientRect();
      return {right: m.right - b.right, gap: menu.classList.contains('up')
        ? b.top - m.bottom : m.top - b.bottom};
    }"""
    for broadcast in (False, True):
        if broadcast:
            frames = await page.evaluate("window.__frames.filter(f => 'rows' in f).length")
            board.broadcast()
            await page.wait_for_function(f"window.__frames.filter(f => 'rows' in f).length > {frames}")
        bounds = await li.evaluate(geometry)
        assert abs(bounds["right"]) <= 1, bounds
        assert 3 <= bounds["gap"] <= 5, bounds
    await li.locator('[data-k="more"]').click()
    await page.mouse.move(0, 0)
    assert await li.locator(".rowmenu").count() == 0
    assert not await li.locator('[data-k="more"]').is_visible()


def _teams_tab(widget: Widget) -> str:
    return f"{widget.base}/teams/1789466400000"


_PARTS = pytest.mark.parametrize("part", ["a.name", "li .prev"], ids=["title", "preview"])


@_PARTS
@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_middle_click_default_is_unchanged(widget: Widget, browser: bool, part: str) -> None:
    """011-AC1: with the setting at `teams` (default), `open`+`web` with --browser, else the plain link;
    the preview line does the same as the title, a native middle-click tab included."""
    page = await widget.load(browser)
    widget.part = part
    if browser:
        assert await widget.act(lambda: page.locator(part).click(button="middle")) == ([(T, True)], [])
    else:
        assert await widget.opens_tab(_teams_tab(widget), button="middle") == []


@_PARTS
@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_middle_click_chat_setting_opens_the_chat_page(
    widget: Widget, browser: bool, part: str
) -> None:
    """011-AC1: the ⚙ `chat` setting persists; middle click (on the title or the preview line) sends
    `open`+`chat` through --browser when set, else opens /chat/<id> in a tab and sends no verb."""
    page = await widget.load(browser)
    await page.click("#gear")
    await page.get_by_role("button", name="Chat here").click()
    assert await page.evaluate("localStorage.getItem('midclick')") == "chat"
    await widget.load(browser)
    widget.part = part
    if browser:
        assert await widget.act(lambda: page.locator(part).click(button="middle")) == ([(T, "chat")], [])
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


@_PARTS
@pytest.mark.parametrize("key", ["Control", "Meta"])
@pytest.mark.parametrize("setting", ["teams", "chat"])
@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_ctrl_click_is_middle_click(
    widget: Widget, setting: str, browser: bool, part: str, key: str
) -> None:
    """011-AC2: Ctrl/⌘ click does what middle click does for either setting, Shift click opens Teams web,
    plain click is unchanged; on the title and on the preview line."""
    page = await widget.load(browser, midclick=setting)
    widget.part = part
    if browser:
        ctrl = await widget.act(lambda: page.locator(part).click(modifiers=[key]))
        assert ctrl == ([(T, "chat" if setting == "chat" else True)], [])
        shift = await widget.act(lambda: page.locator(part).click(modifiers=["Shift"]))
        assert shift == ([(T, True)], [])
    else:
        url = widget.base + _chat(T) if setting == "chat" else _teams_tab(widget)
        assert await widget.opens_tab(url, modifiers=[key]) == []
        assert await widget.opens_tab(_teams_tab(widget), modifiers=["Shift"]) == []
    assert await widget.act(lambda: page.locator(part).click()) == ([(T, False)], [])


async def test_midclick_url_param_overrides_the_stored_choice(widget: Widget) -> None:
    """011-AC1: `?midclick=teams` wins over a stored `chat`, without replacing it."""
    page = await widget.load(True, "/?midclick=teams", midclick="chat")
    assert await widget.act(lambda: page.locator("a.name").click(button="middle")) == ([(T, True)], [])
    assert await page.evaluate("localStorage.getItem('midclick')") == "chat"


# --- cards, deleted runs, reaction chips (011 phase 7) ---


async def test_a_card_renders_its_title_facts_and_links(chat: Pages) -> None:
    """011-AC24: a bot card shows its title, its facts as a table and its OpenUrl action as a link;
    its images that are not archived show their alt text, nothing is fetched (chat fixture)."""
    page = await chat.open(_chat(K))
    await _handled(page, "history")
    card = page.locator('[data-id="1"] .html .card')
    assert await card.locator(".tb.w-bolder").inner_text() == "Build failed"
    assert await card.locator("table.facts th").all_inner_texts() == ["Branch"]
    assert await card.locator("table.facts td").all_inner_texts() == ["main"]
    link = card.locator("a.act")
    assert await link.inner_text() == "Open"
    assert await link.get_attribute("href") == "https://ci.example.org/1"
    assert await card.locator("img").count() == 0
    assert "logo" in await card.inner_text()
    assert await card.evaluate("c => getComputedStyle(c).borderTopStyle") == "solid"
    assert await _act(page, "2", "delete").count() == 1
    assert await _act(page, "2", "edit").count() == 0  # my own card: the server refuses its edit


async def test_deleted_runs_fold_into_one_line_that_expands(chat: Pages) -> None:
    """011-AC25: three consecutive deleted messages (two senders) are one toggle line; a lone
    deleted message stays as itself."""
    page = await chat.open(_chat(R))
    await _handled(page, "history")
    toggle = page.locator("#msgs .run")
    assert await toggle.count() == 1
    assert (await toggle.inner_text(), await toggle.get_attribute("aria-expanded")) == (
        "3 messages deleted",
        "false",
    )
    for i in "234":
        assert await page.locator(f'[data-id="{i}"] .html').is_hidden()
    assert await page.locator('[data-id="7"] .html').inner_text() == "message deleted"
    # Same sender within 5 minutes as the run's last message, yet it shows its own name.
    assert "cont" not in (await page.locator('[data-id="5"]').get_attribute("class") or "").split()
    await toggle.click()
    assert await toggle.get_attribute("aria-expanded") == "true"
    assert await page.evaluate("document.activeElement.classList.contains('run')")
    for i in "234":
        assert await page.locator(f'[data-id="{i}"] .html').inner_text() == "message deleted"
    await page.keyboard.press("Enter")
    assert await toggle.get_attribute("aria-expanded") == "false"
    assert await page.locator('[data-id="3"]').is_hidden()


async def test_a_live_delete_after_a_run_grows_it(board: W.Board, chat: Pages) -> None:
    """011-AC25: deleting the message right after a run joins it; the composer's quotes stay."""
    page = await chat.open(_chat(R))
    await _handled(page, "history")
    await _act(page, "5", "reply").dispatch_event("click")
    await board.on_event(_event("MessageUpdate", R, **{**_m("5", _at(5)), "content": "", "properties": GONE}))
    await page.wait_for_function("document.querySelector('#msgs .run')?.textContent === '4 messages deleted'")
    assert await page.locator("#msgs .run").count() == 1
    assert await page.locator('[data-id="5"]').is_hidden()
    assert await _quote_ids(page) == ["5"]


async def test_reaction_chips_add_or_remove_mine(chat: Pages, calls: list[tuple[Any, ...]]) -> None:
    """011-AC26, 016-AC4: a chip I gave removes my reaction, another adds it; chips are labelled, marked
    when mine, reachable by Tab; every chip is a button, unknown names too."""
    page = await chat.open(_chat(R))
    await _handled(page, "history")
    chips = page.locator('[data-id="1"] .reacts button')
    labels = await chips.evaluate_all("bs => bs.map(b => [b.ariaLabel, b.classList.contains('mine')])")
    assert labels == [
        ["👍 1, remove", True],
        ["❤️ 1, add", False],
        [":party: 1, add", False],
        [":toString: 1, add", False],
    ]
    assert await page.locator('[data-id="1"] .reacts span').count() == 0
    await chips.first.click()
    await _until(calls, 1)
    await chips.nth(1).click()
    await _until(calls, 2)
    await page.locator('[data-id="1"]').focus()
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.ariaLabel") == "👍 1, remove"
    await page.keyboard.press("Tab")
    await page.keyboard.press("Enter")
    await _until(calls, 3)
    heart = ("react", R, "1", "heart", False)
    assert calls == [("react", R, "1", "like", True), heart, heart]


async def test_an_open_run_stays_open_as_it_grows_at_either_end(board: W.Board, chat: Pages) -> None:
    """011-AC25: an expanded run keeps expanded when a live delete joins it above or below."""
    page = await chat.open(_chat(R))
    await _handled(page, "history")
    toggle = page.locator("#msgs .run")
    await toggle.click()
    for msg_id, count in (("1", 4), ("5", 5)):
        update = {**_m(msg_id, _at(int(msg_id))), "content": "", "properties": GONE}
        await board.on_event(_event("MessageUpdate", R, **update))
        await page.wait_for_function(
            f"document.querySelector('#msgs .run')?.textContent === '{count} messages deleted'"
        )
        assert await toggle.get_attribute("aria-expanded") == "true"
        assert await page.locator('[data-id="3"] .html').is_visible()
    await toggle.click()
    assert await page.locator('[data-id="3"]').is_hidden()


async def test_a_search_hit_inside_a_folded_run_lands_on_its_line(chat: Pages) -> None:
    """011-AC25: a jump to a folded message highlights the run's visible line."""
    page = await chat.open(_chat(R), **_VIEW)
    await _handled(page, "history")
    await _search(page, "m3")
    await page.locator('#hits button[data-id="3"]').click()
    await page.wait_for_selector('#msgs [data-id="2"].hit')
    assert await page.locator('[data-id="2"] .run').is_visible()


async def test_reconnect_on_a_message_folded_meanwhile_keeps_the_place(
    board: W.Board, chat: Pages, data_dir: Path
) -> None:
    """011-AC25: the reader's message folded into a run during a drop: its run's line takes its place."""
    page = await chat.open(_chat(R), viewport={"width": 700, "height": 300})
    await _handled(page, "history")
    await page.locator('#msgs [data-id="7"]').evaluate("n => n.scrollIntoView({ block: 'start' })")
    await _scroll(page, "l.scrollTop")
    before = await page.evaluate(_OFFSET, "7")
    store = ChatStore(data_dir, R)
    store.apply_message({**_m("6", _at(6)), "version": "9", "properties": GONE}, _at(30))
    store.close()
    since = await _frames(page)
    await _drop(board)
    await _handled(page, "history", since)
    assert await page.locator('[data-id="7"]').is_hidden()
    assert abs(await page.evaluate(_OFFSET, "6") - before) < 2


async def test_editing_my_message_leaves_its_card_out(chat: Pages) -> None:
    """011-AC13: the editor starts from my text only, not from the card the message carries."""
    page = await chat.open(_chat(K))
    await _handled(page, "history")
    assert "CARDTEXT" in await page.locator('[data-id="3"] .html .card').inner_text()
    box = await _edit(page, "3")
    assert await box.input_value() == "hello"


# --- split navigation (011 phase 6) ---


def _row(thread_id: str, label: str, i: int) -> dict[str, Any]:
    return row(thread_id, label, _at(i), last_id=str(i), read_id=str(i))


@pytest.fixture
async def split(board: W.Board, chat: Pages) -> Pages:
    """The chat fixture with three archived chats on the board (B, not archived, stays off it)."""
    board.rows.clear()
    board.rows.update(
        {T: _row(T, "Fictional T", 3), L: _row(L, "Fictional L", 39), R: _row(R, "Fictional R", 1)}
    )
    return chat


async def _open_split(pages: Pages, thread_id: str = T, **ctx: Any) -> Page:
    page = await pages.open(_chat(thread_id) + "?chatnav=split", **ctx)
    await _handled(page, "history")
    await page.wait_for_selector("#rows li", state="attached")  # a narrow window keeps the list folded
    return page


async def _row_ids(page: Page) -> list[str]:
    return list(await page.locator("#rows li").evaluate_all("ls => ls.map(l => l.dataset.id)"))


async def _switch(page: Page, thread_id: str) -> None:
    since = await _frames(page)
    await page.locator(f'#rows li[data-id="{thread_id}"] a.name').click()
    await _handled(page, "history", since)


async def test_split_shows_the_list_and_switches_in_place(board: W.Board, split: Pages) -> None:
    """011-AC21: the live list beside the chat, newest first, no ◀; a click follows the other chat, shows
    its history, changes URL and title without a page load; Back returns to the previous chat."""
    page = await _open_split(split)
    assert await _row_ids(page) == [L, T, R]
    assert await page.locator("#rows li.cur").get_attribute("data-id") == T
    assert (
        await page.locator("#rows li").first.locator(".prev").inner_text() == "Ann Lee: snippet Fictional L"
    )
    assert not await page.locator("#back").is_visible()
    assert await page.locator("#menu").is_visible()  # folds the docked list (011-AC29)
    await page.evaluate("window.__same = 1")
    await _switch(page, L)
    assert page.url.endswith(_chat(L) + "?chatnav=split")
    assert await page.title() == "Fictional L · miniteams"
    assert (await _ids(page))[-1] == "39"
    assert await page.locator("#rows li.cur").get_attribute("data-id") == L
    assert [tid for tid, _ in board._follows.values()] == [L]
    since = await _frames(page)
    await page.go_back()
    await _handled(page, "history", since)
    assert page.url.endswith(_chat(T) + "?chatnav=split")
    assert await _ids(page) == ["1", "2", "3"]
    await page.wait_for_function("document.title === 'Fictional T · miniteams'")
    assert await page.evaluate("window.__same") == 1  # never reloaded
    since = await _frames(page)
    await page.go_forward()
    await _handled(page, "history", since)
    assert (await _ids(page))[-1] == "39"


async def test_split_setting_is_read_from_storage(split: Pages) -> None:
    """011-AC21: the widget's stored `chatnav` turns the split on, without the URL param."""
    page = await split.open(_chat(T))
    await page.evaluate("localStorage.setItem('chatnav', 'split')")
    await page.reload()
    await page.wait_for_selector("#rows li")
    assert await _row_ids(page) == [L, T, R]


async def test_split_keeps_drafts_quotes_and_editors_per_chat(board: W.Board, split: Pages) -> None:
    """011-AC22: each chat keeps its unsent draft, quotes and open editor across switches; the left chat's
    live events no longer render."""
    page = await _open_split(split)
    await page.locator('[data-id="1"] [data-act=reply]').dispatch_event("click")
    await page.locator("#reply").fill("draft for T")
    await page.locator('[data-id="3"] [data-act=edit]').dispatch_event("click")
    await page.locator(".editor").fill("edit in T")
    await _switch(page, L)
    assert await page.locator("#reply").input_value() == ""
    assert await _quote_ids(page) == []
    await page.locator("#reply").fill("draft for L")
    await board.on_event(_event("NewMessage", T, **_m("90", _at(90), "<p>old chat</p>")))
    await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
    await board.on_event(_event("NewMessage", L, **_m("91", _at(91), "<p>this chat</p>")))
    await page.wait_for_selector('[data-id="91"]')
    assert await page.locator('[data-id="90"]').count() == 0
    assert await page.locator("#typing").inner_text() == ""
    await _switch(page, T)
    assert await page.locator("#reply").input_value() == "draft for T"
    assert await _quote_ids(page) == ["1"]
    assert await page.locator('[data-id="3"] .editor').input_value() == "edit in T"
    await _switch(page, L)
    assert await page.locator("#reply").input_value() == "draft for L"


async def test_split_drops_frames_and_answers_of_the_left_chat(split: Pages) -> None:
    """011-AC22: once switched, frames and answers naming the left chat change nothing on screen, while a
    frame of the new chat relayed before its follow answer still renders."""
    page = await _open_split(split)
    await page.locator("#reply").fill("draft for T")
    await page.evaluate("window.__ws.send = () => {}")  # the server never hears of the switch
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await _inject(page, [{"msg": _live(95), "thread": T}, {"typing": "Ann Lee", "on": True, "thread": T}])
    await _inject(page, [{"msg": _live(98), "thread": L}])
    page_of = {"before": None, "older": False, "newer": False, "source": "archive"}
    await _inject(page, [{"history": L, "messages": [_live(96)], **page_of}])
    assert await _ids(page) == ["n96", "n98"]
    await _inject(page, [{"history": T, "messages": [_live(97)], **page_of}])
    await _inject(page, [{"follow": T, "ok": True, "label": "Stale"}])
    await page.locator("#find").fill("x")
    await page.locator("#find").press("Enter")
    hit = {"id": "1", "at": _at(1), "name": "Ann Lee", "text": "x"}
    await _inject(page, [{"search": T, "q": "x", "hits": [hit]}, {"sent": T, "ok": True}])
    assert await _ids(page) == ["n96", "n98"]
    assert await page.locator("#typing").inner_text() == ""
    assert await page.title() == "Fictional L · miniteams"
    assert await page.locator("#hits").is_hidden()
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    assert await page.locator("#reply").input_value() == "draft for T"


def _swallow(page: Page, verb: str) -> Awaitable[None]:
    """The page's `verb` frames never reach the server; every other one does."""
    script = (
        "v => { const s = window.__ws.send.bind(window.__ws);"
        " window.__ws.send = (d) => { if (!(v in JSON.parse(d))) s(d); }; }"
    )
    return page.evaluate(script, verb)


async def test_split_a_save_in_flight_is_not_reopened_and_its_failure_waits(split: Pages) -> None:
    """011-AC22: an edit saving when its chat is left is not stashed with stale text; its failure, answered
    while away, shows on that chat's line on return."""
    page = await _open_split(split)
    await _swallow(page, "edit")
    await (await _edit(page, "3")).fill("saving this")
    await page.locator(".editor").press("Enter")
    await _switch(page, L)
    await _inject(page, [{"edit": T, "msg": "3", "error": "boom"}])
    assert await page.locator("#err").inner_text() == ""
    await _switch(page, T)
    assert await page.locator(".editor").count() == 0
    assert await page.locator("#err").inner_text() == "edit: boom"


async def test_split_busy_answers_say_why(split: Pages) -> None:
    """011-AC22: a send or action refused as busy (another one runs on the socket) says so in words."""
    page = await _open_split(split)
    await _swallow(page, "send")
    await page.locator("#reply").fill("hold on")
    await page.locator("#reply").press("Enter")
    await _inject(page, [{"sent": T, "error": "busy"}])
    assert await page.locator("#err").inner_text() == "another action is still running, try again"
    assert await page.locator("#reply").input_value() == "hold on"
    await _inject(page, [{"react": T, "msg": "1", "error": "busy"}])
    assert await page.locator("#err").inner_text() == "reaction: another action is still running, try again"


async def test_split_list_is_reachable_by_keyboard_and_keeps_focus(board: W.Board, split: Pages) -> None:
    """011-AC23: Tab from a row's name reaches its 👁 and ⋯, shown, and ⋯ its mute; a broadcast or a tab
    switch keeps the focus where it was; the shown tab and the current row are announced."""
    page = await _open_split(split)
    await page.locator(f'#rows li[data-id="{L}"] a.name').focus()
    await page.keyboard.press("Tab")
    read = page.locator(f'#rows li[data-id="{L}"] [data-k="read"]')
    assert await read.evaluate("b => b === document.activeElement && getComputedStyle(b).opacity === '1'")
    board.broadcast()
    await page.wait_for_function("window.__frames.filter(f => 'rows' in f).length >= 2")
    assert await read.evaluate("b => b === document.activeElement")
    await page.keyboard.press("Tab")
    await page.keyboard.press("Enter")  # ⋯: the focus lands on Mark seen
    await page.keyboard.press("Tab")
    await page.keyboard.press("Enter")
    await page.wait_for_function(f"!document.querySelector('#rows li[data-id={json.dumps(L)}]')")
    assert board.rows[L]["muted"] is True
    unread = page.get_by_role("button", name="Unread")
    await unread.focus()
    await page.keyboard.press("Enter")
    assert await page.locator('#tabs [data-k="tab:unread"]').evaluate("b => b === document.activeElement")
    assert await unread.get_attribute("aria-pressed") == "true"
    await page.get_by_role("button", name="Inbox").click()
    assert await page.locator(f'#rows li[data-id="{T}"]').get_attribute("aria-current") == "page"
    assert await page.locator("#rows li[aria-current]").count() == 1


async def test_split_list_updates_live_and_its_actions_work(board: W.Board, split: Pages) -> None:
    """011-AC23: a new message moves its row up with the unread mark, a mention shows its tag; seen and
    mute from the list reach the board."""
    page = await _open_split(split)
    await board.on_event(_event("NewMessage", R, **_m("200", _at(200), "<p>fresh news</p>")))
    await page.wait_for_function(f"document.querySelector('#rows li').dataset.id === {json.dumps(R)}")
    top = page.locator("#rows li").first
    assert "unread" in (await top.get_attribute("class") or "")
    assert await top.locator(".dot").count() == 1
    assert "fresh news" in await top.locator(".prev").inner_text()
    mention = json.dumps([{"mentionType": "person", "mri": ME}])
    await board.on_event(
        _event("NewMessage", L, **_m("201", _at(201), "<p>hey</p>", properties={"mentions": mention}))
    )
    await page.wait_for_selector(f'#rows li.mention[data-id="{L}"] .tag')
    await page.locator(f'#rows li[data-id="{R}"]').hover()
    await page.locator(f'#rows li[data-id="{R}"] [data-k="more"]').click()
    await page.locator(f'#rows li[data-id="{R}"] [data-k="seen"]').click()
    await page.wait_for_function(f"!document.querySelector('#rows li[data-id={json.dumps(R)}]')")
    assert board.rows[R]["seen_at"] == _at(200)
    await page.locator(f'#rows li[data-id="{T}"]').hover()
    await page.locator(f'#rows li[data-id="{T}"] [data-k="more"]').click()
    await page.locator(f'#rows li[data-id="{T}"] [data-k="mute"]').click()
    await page.wait_for_function(f"!document.querySelector('#rows li[data-id={json.dumps(T)}]')")
    assert board.rows[T]["muted"] is True
    await page.locator("#tabs .more").click()  # Muted sits behind ⋯ in a 300px list (011-AC28)
    await page.get_by_role("menuitemradio", name="Muted").click()
    assert await _row_ids(page) == [T]
    await page.locator("#filter").fill("zzz")
    assert await page.locator("#nochat").inner_text() == "no chat matches «zzz»"


async def test_split_narrow_window_folds_the_list(split: Pages) -> None:
    """011-AC21: below ~700px the list hides behind ☰, which stays clickable while it is open; Escape, a
    click beside it and picking a chat each fold it."""
    page = await _open_split(split, viewport={"width": 600, "height": 700})
    side = page.locator("#side")
    assert not await side.is_visible()
    await page.click("#menu")
    assert await side.is_visible()
    await page.click("#menu")  # a real click: fails if the open list covers ☰
    assert not await side.is_visible()
    await page.click("#menu")
    await page.get_by_role("button", name="Unread").click()  # re-renders the list under the click
    assert await side.is_visible()
    await page.get_by_role("button", name="Inbox").click()
    await page.keyboard.press("Escape")
    assert not await side.is_visible()
    await page.click("#menu")
    await page.mouse.click(550, 400)
    assert not await side.is_visible()
    assert page.url.endswith(_chat(T) + "?chatnav=split")
    await page.click("#menu")
    await _switch(page, L)
    assert not await side.is_visible()
    assert page.url.endswith(_chat(L) + "?chatnav=split")


@_PARTS
@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_split_middle_click_opens_a_new_tab(
    board: W.Board, split: Pages, monkeypatch: pytest.MonkeyPatch, browser: bool, part: str
) -> None:
    """011-AC21: middle click on a row (its title or its preview line) opens that chat in a new tab, in
    split too, through --browser when set, and so does Ctrl/⌘ click; Shift click opens Teams through it.
    The shown chat stays."""
    opened: list[Verb] = []

    async def spy(
        thread_id: str,
        target: str = "app",
        ws: Any = None,
        msg_id: str = "",
    ) -> bool:
        opened.append((thread_id, "split" if target == "split" else target == "chat"))
        return True

    monkeypatch.setattr(board, "open_row", spy)
    board.browser = ["firefox"] if browser else None
    page = await _open_split(split)
    name = page.locator(f'#rows li[data-id="{L}"] {part.removeprefix("li ")}')
    if browser:
        await name.click(button="middle")
        await name.click(modifiers=["Control"])
        await name.click(modifiers=["Meta"])
        await name.click(modifiers=["Shift"])
        await _until(opened, 4)
        assert opened == [(L, "split"), (L, "split"), (L, "split"), (L, False)]
    else:
        for click in ({"button": "middle"}, {"modifiers": ["Control"]}, {"modifiers": ["Meta"]}):
            async with page.context.expect_page() as tab:
                await name.click(**click)  # type: ignore[arg-type]
            new = await tab.value
            await new.wait_for_url(f"http://{split.host}{_chat(L)}?chatnav=split")
            await new.close()
    assert page.url.endswith(_chat(T) + "?chatnav=split")


async def test_list_mode_has_no_side_pane(split: Pages) -> None:
    """011-AC21: with `chatnav` at `list` (default) the page is unchanged: ◀, no list, no board rows."""
    page = await split.open(_chat(T))
    await _handled(page, "history")
    assert await page.locator("#back").is_visible()
    assert not await page.locator("#side").is_visible()
    assert not await page.locator("#menu").is_visible()
    assert await page.evaluate("window.__frames.some(f => 'rows' in f)") is False


# --- review fixes ---


async def test_reply_is_offered_where_the_server_takes_a_quote(
    chat: Pages, calls: list[tuple[Any, ...]]
) -> None:
    """011 Message actions: no ↩ on a card post (`send` cannot quote a Media_Card); a text message
    carrying a card has one, and quoting it posts."""
    page = await chat.open(_chat(K))
    await _handled(page, "history")
    offered = await page.locator("#msgs .msg").evaluate_all(
        "ns => ns.map(n => [n.dataset.id, !!n.querySelector('[data-act=reply]')])"
    )
    assert offered == [["1", False], ["2", False], ["3", True]]
    await page.locator('[data-id="3"]').hover()
    await _act(page, "3", "reply").click()
    await page.locator("#reply").fill("answer")
    await page.locator("#reply").press("Enter")
    await page.wait_for_function("document.getElementById('quotes').hidden")
    assert [c[:2] for c in calls] == [("send", K)]
    assert 'itemid="3"' in calls[0][2]


async def test_open_in_teams_after_a_jump_opens_the_chat(
    launched: list[tuple[str, ...]], board: W.Board, chat: Pages
) -> None:
    """011-AC19: detached by a jump, the header's Open in Teams opens the chat, not the last message shown."""
    board.opener = ["xdg-open"]
    page = await _open_long(chat)
    await _jump_to(page, "20")
    await page.click("#teams")
    await _until(launched, 1)
    assert launched == [("xdg-open", W.deep_link(X, "", board.open_scheme))]


async def test_pages_redial_a_restarted_server_with_their_cookie(
    board: W.Board, settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: after a restart, open pages reconnect on their `mt` cookie alone: no reload, nothing fetched
    but the sign-in probe of a refused dial (a plain GET of /ws)."""
    port = test_web_chat._free_port()
    monkeypatch.setattr(test_web_chat, "_free_port", lambda: port)  # the restart binds the same port
    live = "document.getElementById('status').title === 'live'"
    async with _served(board, settings) as host:
        pages.serve(host, board)
        opened = [await pages.open("/"), await pages.open(_chat(T))]
        requests: list[str] = []
        for page in opened:
            await until(page, live)
            await until(page, "[...document.images].every(i => i.complete)")  # a late image is not a redial
            await page.evaluate("window.__same = 1")
            page.on("request", lambda r: requests.append(r.url))
    for page in opened:
        await until(page, "document.getElementById('status').title === 'disconnected'")
    async with _served(board, settings):
        for page in opened:
            await until(page, live, tries=1500)  # backoff 1 s, then 2 s if the first dial was early
            assert await page.evaluate("window.__same") == 1
        await pages.close()
    assert [u for u in requests if not u.startswith("ws") and not u.endswith("/ws")] == []
    assert pages.external == []


_LIVE = "document.getElementById('status').title === 'live'"


@pytest.mark.parametrize("path", ["/", f"{_chat(T)}?chatnav=split"])
async def test_a_launch_url_signs_a_fresh_browser_in(
    board: W.Board, settings: Settings, pages: Pages, path: str
) -> None:
    """A fresh profile loading `<page>#<token>` (web.json's launch URL, a --browser chat open) trades the
    token for the cookie under the real CSP: the socket connects, the fragment leaves the address bar."""
    async with _served(board, settings) as host:
        pages.host = host  # no cookie: the token is the way in
        token = board.token
        page = await pages.open(f"{path}#{token}", csp=True)
        await until(page, _LIVE)
        assert await page.evaluate("location.href") == f"http://{host}{path}"
        assert board.token != token and await page.locator("#signin").count() == 0
        await pages.close()
    assert pages.external == []


@pytest.mark.parametrize("path", ["/", _chat(T)])
async def test_a_page_without_the_cookie_says_how_to_sign_in(
    board: W.Board, settings: Settings, pages: Pages, path: str
) -> None:
    """No cookie, no token: the refused socket shows how to sign in, never the secret nor the token."""
    async with _served(board, settings) as host:
        pages.host = host
        page = await pages.open(path, csp=True)
        hint = page.locator("#signin")
        await hint.wait_for()
        expected = "open the launch file (~/.config/miniteams/open.html by default) or web.json's URL"
        expected += " (host, port and #token)"
        assert await hint.text_content() == f"Not signed in: {expected}"
        # M3: a banner over the top, not a flex column eating the chat page's row layout.
        box = await hint.bounding_box()
        assert box is not None and box["y"] == 0 and box["height"] < 80
        assert box["width"] == await page.evaluate("document.documentElement.clientWidth")
        assert board.secret not in await page.content() and board.token not in await page.content()
        await pages.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_both_settings_menus_share_one_style(chat: Pages, theme: str) -> None:
    """H6: one ⚙ menu style (ui.css) in both pages: the same layer, headings at 4.5:1 or more."""
    for path in (f"/?theme={theme}", f"{_chat(T)}?theme={theme}"):
        page = await chat.open(path)
        await page.locator("#gear").click()
        assert await page.locator(".menu").evaluate("m => getComputedStyle(m).zIndex") == "6", path
        ratios = await page.locator(".menu h3").evaluate_all(f"hs => hs.map({CONTRAST})")
        assert ratios and min(ratios) >= 4.5, (path, ratios)


# --- custom reaction picker (spec 016) ---

_OID = "0-wus-d1-0123456789abcdef"  # the image's documentId
_PARROT = "parrot;0-wus-d1-fedcba9876543210"  # Teams' emoticon id: its tail is not the documentId
_FAV = f"{_PARROT} {_OID}"  # a stored custom favorite: key, then image
_DEFAULT = ["👍", "❤️", "😆", "😮", "😢", "😡", "🙏", "🙃"]


@pytest.fixture
def parrot(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """One org custom emoji the completion offers, its image served without a download."""

    async def image(oid: str) -> tuple[bytes, str] | None:
        return (PNG, "image/png") if oid == _OID else None

    board.emojis.names, board.emojis.keys = {"parrot": _OID}, {"parrot": _PARROT}
    monkeypatch.setattr(board.emojis, "image", image)


async def _picker(page: Page, msg_id: str = "1") -> list[str]:
    """The open picker's faces: the emoji, or a custom one's image path."""
    await page.locator(f'[data-id="{msg_id}"]').hover()
    await _act(page, msg_id, "react").click()
    faces = await page.locator(f'[data-id="{msg_id}"] .picker button').evaluate_all(
        "bs => bs.map(b => b.querySelector('img')?.getAttribute('src') || b.textContent)"
    )
    await page.keyboard.press("Escape")
    return list(faces)


async def _slots(page: Page) -> list[str]:
    return list(
        await page.locator("#settings .favs button").evaluate_all(
            "bs => bs.map(b => b.querySelector('img')?.getAttribute('src') || b.textContent)"
        )
    )


async def _search_pick(page: Page, q: str) -> None:
    """Type `q` in the open emoji search and take its first item."""
    search = page.locator(".esearch input")
    assert await search.input_value() == ":"
    await search.press_sequentially(q)
    await page.wait_for_selector("#epop:not([hidden])")
    await search.press("Enter")


async def test_reaction_slots_replace_reorder_reset_and_survive_a_reload(chat: Pages, parrot: None) -> None:
    """016-AC1: the ⚙ Reactions row shows the 8 slots; a slot replaced through the emoji completion,
    moved with the arrows, or reset, changes the picker at once and after a reload."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.locator("#gear").click()
    assert await _slots(page) == _DEFAULT
    await page.locator("#settings .favs button").nth(0).click()
    await _search_pick(page, "fox")
    await page.locator("#settings .favs button").nth(7).click()
    await _search_pick(page, "parr")
    assert await _slots(page) == ["🦊", *_DEFAULT[1:7], f"/emoji/{_OID}"]
    assert await page.locator("#settings .favs button").nth(7).evaluate("b => b === document.activeElement")
    await page.locator("#settings .favs button").nth(0).click()
    await page.keyboard.press("Escape")  # closes the search, not the ⚙ menu
    assert await page.locator(".esearch").count() == 0
    await page.locator("#settings [aria-label='move the chosen slot right']").click()
    moved = [_DEFAULT[1], "🦊", *_DEFAULT[2:7], f"/emoji/{_OID}"]
    assert await _slots(page) == moved
    await page.keyboard.press("Escape")
    assert await _picker(page) == [*moved, "…"]
    await page.wait_for_function("document.querySelector('.picker img')?.naturalWidth > 0")
    await page.reload()
    await _handled(page, "history")
    assert await _picker(page) == [*moved, "…"]
    await page.locator("#gear").click()
    assert await _slots(page) == moved
    await page.locator("#settings [aria-label='back to the default reactions']").click()
    assert await _slots(page) == _DEFAULT
    await page.keyboard.press("Escape")
    assert await _picker(page) == [*_DEFAULT, "…"]


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        json.dumps(["🦊"] * 7),  # partial
        json.dumps(["🦊"] * 9),
        json.dumps([*_DEFAULT[:7], "<img src=x onerror=alert(1)>"]),
        json.dumps([*_DEFAULT[:7], "abc"]),
        json.dumps([*_DEFAULT[:7], "x;../../ws"]),
        json.dumps([*_DEFAULT[:7], f"{_PARROT} ../ws"]),
        json.dumps([*_DEFAULT[:7], _PARROT]),  # no image id
        json.dumps([*_DEFAULT[:7], 7]),
        json.dumps({"0": "🦊"}),
        "null",
    ],
)
async def test_a_corrupt_stored_list_falls_back_to_the_default(chat: Pages, stored: str) -> None:
    """016-AC5: an unreadable or partial stored list gives the default picker and slots, no page error."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.evaluate("v => localStorage.setItem('reactions', v)", stored)
    await page.reload()
    await _handled(page, "history")
    assert await _picker(page) == [*_DEFAULT, "…"]
    await page.locator("#gear").click()
    assert await _slots(page) == _DEFAULT


async def test_any_emoji_reacts_at_once_and_leaves_the_favorites(
    chat: Pages, parrot: None, calls: list[tuple[Any, ...]]
) -> None:
    """016-AC6, 016-AC2: "…" opens the emoji search; a Unicode pick Teams does not name goes as its hex
    key, a custom one as `<name>;<id>`; the favorites stay; Escape closes the search."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    for q in ("fox", "parr"):
        await page.locator('[data-id="1"]').hover()
        await _act(page, "1", "react").click()
        await page.locator('[data-id="1"] .picker .more').click()
        assert await page.locator('[data-id="1"] .picker').is_hidden()
        await _search_pick(page, q)
        assert await page.locator(".esearch").count() == 0
        assert await page.evaluate("document.activeElement.dataset.id") == "1"  # not <body>
    await _until(calls, 2)
    assert calls == [("react", T, "1", "1f98a_foxface", False), ("react", T, "1", _PARROT, False)]
    assert await page.evaluate("localStorage.getItem('reactions')") is None
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    await page.locator('[data-id="1"] .picker .more').click()
    await page.locator(".esearch input").press_sequentially("pr")
    await page.wait_for_selector("#epop:not([hidden])")
    await page.keyboard.press("Escape")
    assert await page.locator(".esearch").count() == 0
    assert await page.evaluate("document.activeElement.dataset.id") == "1"
    assert len(calls) == 2


async def test_a_keyless_custom_emoji_is_refused_with_a_reason(
    board: W.Board, chat: Pages, parrot: None, calls: list[tuple[Any, ...]]
) -> None:
    """016-AC6: a custom emoji Teams gave no reaction key can't be sent: the page says so and the focus
    goes back to the message instead of falling to <body>."""
    board.emojis.keys = {}
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    await page.locator('[data-id="1"]').hover()
    await _act(page, "1", "react").click()
    await page.locator('[data-id="1"] .picker .more').click()
    await _search_pick(page, "parr")
    assert await page.locator("#err").inner_text() == "this emoji can't be used as a reaction"
    assert await page.evaluate("document.activeElement.dataset.id") == "1"
    assert calls == []


async def test_every_chip_toggles_with_its_own_key(
    board: W.Board, chat: Pages, parrot: None, calls: list[tuple[Any, ...]]
) -> None:
    """016-AC4: custom, toned, Unicode and unknown-name chips each send their own key; mine removes it.
    A custom favorite joins the chip of the same key, and a Unicode one the chip of the same emoji."""
    page = await chat.open(_chat(T))
    await _handled(page, "history")
    keys = [_PARROT, "yes-tone1", "1f98a_foxface", "whew", "like"]
    users = {_PARROT: ME, "like": ME}
    emotions = [{"key": k, "users": [{"mri": users.get(k, ANN)}]} for k in keys]
    partial = {k: v for k, v in _m("1", _at(1)).items() if k != "content"}
    await board.on_event(_event("MessageUpdate", T, **{**partial, "properties": {"emotions": emotions}}))
    chips = page.locator('[data-id="1"] .reacts button')
    await page.wait_for_function("document.querySelectorAll('[data-id=\"1\"] .reacts button').length === 5")
    for i in range(len(keys)):
        await chips.nth(i).click()
        await _until(calls, i + 1)
    assert calls == [("react", T, "1", k, k in users) for k in keys]
    await page.evaluate("v => localStorage.setItem('reactions', v)", json.dumps([*_DEFAULT[:6], "🦊", _FAV]))
    for fav in (_FAV, "🦊"):
        await page.locator('[data-id="1"]').hover()
        await _act(page, "1", "react").click()
        await page.locator(f'[data-id="1"] .picker [data-fav="{fav}"]').click()
    await _until(calls, len(keys) + 2)
    assert calls[len(keys) :] == [("react", T, "1", _PARROT, True), ("react", T, "1", "1f98a_foxface", False)]
