"""The chat page over an archive behind the stream, in a headless browser: the board row holds message 7,
the archive ends at 5, the API holds 4 to 7 once it answers.

Run: `uv run --with playwright pytest tests/test_browser_lag.py`. Synthetic data, no network."""

import re
from collections.abc import AsyncIterator
from typing import Any

import browser_helpers
import httpx
import pytest
import test_web_chat
from browser_helpers import FRAMES, Page, Pages, past, until  # skips without playwright
from test_web_chat import T, _at, _event, _m, _served

from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.config import Settings

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, data_dir, quiet_directory = test_web_chat.board, test_web_chat.data_dir, test_web_chat.quiet_directory

# window.__lag: [time, hidden] per change of #lag; window.__answers: [time, frame] per history answer,
# taken before the page's own handler runs.
SPY = """(() => { window.__lag = []; window.__answers = [];
  const Prev = window.WebSocket;
  window.WebSocket = class extends Prev { constructor(...a) { super(...a);
    this.addEventListener("message", (e) => { const m = JSON.parse(e.data);
      if ("history" in m) window.__answers.push([performance.now(), m]); }); } };
  addEventListener("DOMContentLoaded", () => { const n = document.getElementById("lag");
    new MutationObserver(() => window.__lag.push([performance.now(), n.hidden]))
      .observe(n, { attributes: true, attributeFilter: ["hidden"] }); });
})();"""
IDS = "[...document.querySelectorAll('#msgs > .msg')].map(n => n.dataset.id)"
ASKS = "window.__out.filter(f => 'history' in f).length"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """fetch_history fails while the list's head is True (popped per call), then answers 4 to 7."""
    down: list[bool] = [True]

    def fake_history(*args: Any) -> list[dict[str, Any]]:
        if down.pop(0) if down else False:
            raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://x"))
        return [_m(str(i), _at(i)) for i in (4, 5, 6, 7)]

    monkeypatch.setattr(CV, "fetch_history", fake_history)
    return down


@pytest.fixture
async def served(board: W.Board, settings: Settings, pages: Pages, api: list[bool]) -> AsyncIterator[Pages]:
    board.rows[T] = {"id": T, "label": "T", "last_activity": _at(7), "last_id": "7", "typing": []}
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield pages
        await pages.close()
    assert pages.external == []


async def _open(pages: Pages, **ctx: Any) -> Page:
    context = await pages.context(bypass_csp=True, **ctx)
    assert pages.cookie is not None
    await context.add_cookies([pages.cookie])  # type: ignore[list-item]
    await context.add_init_script(FRAMES)
    await context.add_init_script(SPY)
    await context.route(re.compile(r"^(?!http://127\.0\.0\.1:)"), lambda route: route.abort())
    page = await context.new_page()
    pages.watch(page)
    await page.goto(f"http://{pages.host}/chat/{T}")
    await until(page, "window.__answers.length > 0")
    return page


async def test_indicator_shows_after_the_delay_and_hides_once_caught_up(served: Pages) -> None:
    """Offline at first: the archive's page, the indicator 600 ms later, kept 400 ms once shown; the
    retried ask merges 6 and 7 in order and hides it."""
    chat = await _open(served)
    await until(chat, f"{IDS}.length === 7 && window.__lag.length === 2", tries=1000)
    (answered, first), (_, second) = await chat.evaluate("window.__answers")
    assert (first["behind"], "behind" in second) == ("7", False)
    [[shown, hidden_then], [gone, hidden_now]] = await chat.evaluate("window.__lag")
    assert (hidden_then, hidden_now) == (False, True)
    assert shown - answered >= 600
    assert gone - shown >= 395  # Date.now()'s whole milliseconds against performance.now()
    assert await chat.evaluate(IDS) == ["1", "2", "3", "4", "5", "6", "7"]


async def test_no_flash_when_the_relay_brings_the_latest_message_first(
    board: W.Board, served: Pages, api: list[bool]
) -> None:
    """The row's newest message relayed live before the delay: never shown, never asked again."""
    api[:] = [True] * 10
    chat = await _open(served)
    await board.on_event(_event("NewMessage", T, **_m("7", _at(7))))
    await until(chat, f"{IDS}.includes('7')")
    await past(chat, 1500)  # past the delay and the first retry
    assert (await chat.evaluate("window.__lag"), await chat.evaluate(ASKS)) == ([], 1)


async def test_indicator_once_shown_stays_its_minimum(board: W.Board, served: Pages, api: list[bool]) -> None:
    """Caught up by the relay just after it showed: it still stays 400 ms."""
    api[:] = [True] * 10
    chat = await _open(served)
    await until(chat, "window.__lag.length === 1")
    await board.on_event(_event("NewMessage", T, **_m("7", _at(7))))
    await until(chat, "window.__lag.length === 2")
    [[shown, _], [gone, hidden]] = await chat.evaluate("window.__lag")
    assert hidden and gone - shown >= 395


async def test_a_message_both_relayed_and_merged_shows_once(
    board: W.Board, served: Pages, api: list[bool]
) -> None:
    """6 arrives live while the archive lags; the retried page brings 6 and 7: 6 stays single."""
    chat = await _open(served)
    await board.on_event(_event("NewMessage", T, **_m("6", _at(6))))
    await until(chat, f"{IDS}.length === 7", tries=1000)
    assert await chat.evaluate(IDS) == ["1", "2", "3", "4", "5", "6", "7"]
    await until(chat, "document.getElementById('lag').hidden")


async def test_the_merge_does_not_move_a_reader_scrolled_up(served: Pages, api: list[bool]) -> None:
    api[:] = [True, True]  # caught up on the second retry, 3 s in: time to scroll up
    chat = await _open(served, viewport={"width": 700, "height": 260})
    await until(chat, f"{IDS}.length === 5 && document.getElementById('list').scrollTop > 0")
    await chat.evaluate(
        "(l => { l.scrollTop = 0; l.dispatchEvent(new Event('scroll')); })(document.getElementById('list'))"
    )
    await until(chat, f"{IDS}.length === 7", tries=1000)
    assert await chat.evaluate("document.getElementById('list').scrollTop") == 0
    assert await chat.evaluate("document.getElementById('latest').textContent") == "↓ latest (2)"
