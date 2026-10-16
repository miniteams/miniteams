"""Mark a chat read in Teams when it is shown (spec 020). A fake chat socket serves synthetic rows and
history; no network, no Teams. Visibility and focus are the test's: an init script stands in for
`document.visibilityState` and `document.hasFocus()`, and `_set` dispatches the event a change fires.

Run: `uv run --with playwright pytest tests/test_browser_autoread.py`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import browser_helpers
import pytest
import test_browser_split_ux as ux
from browser_helpers import Page, Pages, past, rendered, until, wait  # skips without playwright
from test_browser_split_ux import _board, _id

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
both = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)

# Headless engines disagree on focus: the page sees what the test sets.
_SHOWN = """window.__vis = "visible"; window.__focus = true;
Object.defineProperty(document, "visibilityState", { get: () => window.__vis, configurable: true });
document.hasFocus = () => window.__focus;"""
READ_MS = 1000  # the page's READ_MS


def _msg(i: int, *, me: bool = False) -> dict[str, Any]:
    at = f"2026-10-09T10:{i // 60:02d}:{i % 60:02d}.000Z"
    who = {"from": "8:orgid:me", "name": "Me"} if me else {"from": "8:orgid:ann", "name": "Ann Lee"}
    body = {"type": "text", "html": f"<p>m{i}</p>", "reactions": []}
    return {"id": str(1000 + i), "at": at, **who, "me": me, **body}


@dataclass
class Fake(ux.Fake):
    """The split fake with a latest page of `size` messages (60 scroll away from the latest one), held
    while `hold` is set; a search hits message 10, the page around it has newer ones below."""

    size: int = 60
    mine: bool = False  # the history is all the reader's own messages
    lag: int = 0  # latest pages still answered `behind`: the page asks again (a catch-up)
    older: bool = False  # the latest page has older ones above, served by `before`
    after: bool = False  # `after` pages are answered (else never): the rest, and one message more
    busy: int = 0  # latest pages answered `busy`; the retry after the last one is held if `busy_hold`
    busy_hold: bool = True
    jump_fails: bool = False  # `around` pages answer an error
    fails: int = 0  # latest pages answering an error

    def on_verb(self, msg: dict[str, Any]) -> None:
        thread = msg.get("history")
        if thread is None:
            if "search" in msg:
                hit = {"id": "1010", "name": "Ann Lee", "at": _msg(10)["at"], "text": "m10"}
                self.send({"search": msg["search"], "q": msg["q"], "hits": [hit]})
            else:
                super().on_verb(msg)
        elif "around" in msg and self.jump_fails:
            self.send({"history": thread, "around": msg["around"], "error": "history unavailable"})
        elif "around" in msg:
            page = [_msg(i) for i in range(30)]
            self.send({"history": thread, "around": msg["around"], "messages": page, "newer": True})
        elif msg.get("before"):
            old = [{**_msg(i), "id": str(900 + i), "at": f"2026-10-09T09:00:{i:02d}.000Z"} for i in range(5)]
            self.send({"history": thread, "before": msg["before"], "messages": old, "source": "archive"})
        elif "after" in msg:
            if self.after:
                rest = [_msg(i) for i in range(30, self.size + 1)]
                self.send({"history": thread, "after": msg["after"], "messages": rest, "source": "archive"})
        elif self.fails:
            self.fails -= 1
            self.send({"history": thread, "error": "history unavailable"})
        elif self.busy:
            self.busy -= 1
            self.hold = self.busy_hold and not self.busy
            self.send({"history": thread, "error": "busy"})
        else:
            messages = [_msg(i, me=self.mine) for i in range(self.size)]
            frame = {"history": thread, "messages": messages, "source": "archive", "older": self.older}
            if self.lag:
                self.lag -= 1
                frame["behind"] = "9999"
            self.answer(frame)

    def reads(self) -> list[str]:
        return [v["read"] for v in self.of("read")]


@dataclass
class Auto(ux.Split):
    fake: Fake

    async def open(self, at: int = 2, query: str = "?chatnav=split", **ctx: Any) -> Page:
        ctx.setdefault("viewport", {"width": 1300, "height": 700})
        return await super().open(at, query, **ctx)

    def live(self, i: int, *, me: bool = False, at: int = 2) -> None:
        self.fake.send({"msg": _msg(100 + i, me=me), "thread": _id(at)})


@pytest.fixture
async def auto(pages: Pages, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Auto]:
    real = browser_helpers.open_fake

    async def shown(*a: Any, init: str = "", **kw: Any) -> Page:
        return await real(*a, init=_SHOWN + init, **kw)

    monkeypatch.setattr(ux, "open_fake", shown)
    found = Auto(pages, Fake(rows=_board(12)))
    yield found
    await pages.close()
    assert pages.external == []


_VIS = "v => { window.__vis = v; document.dispatchEvent(new Event('visibilitychange')); }"
_FOCUS = "f => { window.__focus = f; window.dispatchEvent(new Event(f ? 'focus' : 'blur')); }"


async def _set(page: Page, *, vis: str | None = None, focus: bool | None = None) -> None:
    if vis is not None:
        await page.evaluate(_VIS, vis)
    if focus is not None:
        await page.evaluate(_FOCUS, focus)


async def _reads(auto: Auto, past_ms: int = READ_MS + 100) -> list[str]:
    """The `read` verbs sent so far, once every trailing one is due."""
    await auto.fake.handled()
    await auto.fake.settled(past_ms)
    return auto.fake.reads()


async def _drop(auto: Auto) -> None:
    """The socket closes from the server's side; the page dials again (1 s) and follows."""
    asked = len(auto.fake.of("history"))
    await auto.fake.ws.close()
    await wait(lambda: len(auto.fake.of("history")) > asked, "the latest page asked again")


async def _scroll(page: Page, top: bool) -> None:
    js = "t => { const l = document.getElementById('list'); l.scrollTop = t ? 0 : l.scrollHeight; }"
    await page.evaluate(js, top)
    await rendered(page)


@both
async def test_setting_is_on_by_default_and_off_sends_no_read(auto: Auto) -> None:
    """020-AC1: ⚙ "Mark read in Teams when shown" is checked by default; unchecked it is stored, kept
    across a reload, and no `read` goes out on open or on a message from someone else."""
    page = await auto.open()
    await ux._settings(page)
    box = page.get_by_role("checkbox", name="Mark read in Teams when shown")
    assert await box.is_checked()
    await box.uncheck()
    assert await ux._stored(page, "autoread") == "0"
    await auto.reload(page)
    await ux._settings(page)
    assert not await page.get_by_role("checkbox", name="Mark read in Teams when shown").is_checked()
    auto.live(1)
    await page.locator(f"#rows li[data-id='{_id(4)}'] .name").click()
    await wait(lambda: auto.fake.follows()[-1:] == [_id(4)])
    assert await _reads(auto) == []


@both
async def test_unchecking_drops_a_trailing_read(auto: Auto) -> None:
    """020-AC1: a `read` waiting out the per-second limit is not sent once the setting is unchecked."""
    page = await auto.open()
    await wait(lambda: auto.fake.reads() == [_id(2)])
    auto.live(1)  # within the second: trails
    await auto.fake.handled()
    await ux._settings(page)
    await page.get_by_role("checkbox", name="Mark read in Teams when shown").uncheck()
    assert await _reads(auto) == [_id(2)]


@both
async def test_checking_again_marks_the_shown_chat(auto: Auto) -> None:
    """020-AC1: a message landing while unchecked is owed: checking the setting again marks the chat."""
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    await ux._settings(page)
    box = page.get_by_role("checkbox", name="Mark read in Teams when shown")
    await box.uncheck()
    auto.live(1)
    assert await _reads(auto) == [_id(2)]
    await box.check()
    assert await _reads(auto) == [_id(2), _id(2)]


@both
@pytest.mark.parametrize("how", ["load", "click", "enter", "back", "forward"])
async def test_opening_a_chat_sends_one_read(auto: Auto, how: str) -> None:
    """020-AC2: opening a chat (first load, a click, Enter in the list, Back, Forward) sends one `read`
    for it once its history is shown, never before."""
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]

    async def step(go: Any, target: str) -> None:
        auto.fake.verbs.clear()
        shown = await page.evaluate("window.__frames.filter(f => 'history' in f).length")
        await go()
        await wait(lambda: auto.fake.follows()[-1:] == [target])
        await until(page, f"window.__frames.filter(f => 'history' in f).length > {shown}")
        await _reads(auto)
        verbs = [v for v in auto.fake.verbs if "history" in v or "read" in v]
        assert [v["read"] for v in verbs if "read" in v] == [target]
        assert "history" in verbs[0]  # asked first, read once shown

    async def enter() -> None:
        await page.locator("#filter").fill("chat 007")
        await page.locator("#filter").press("Enter")

    if how == "click":
        await step(page.locator(f"#rows li[data-id='{_id(6)}'] .name").click, _id(6))
    if how in ("enter", "back", "forward"):
        await step(enter, _id(7))
    if how in ("back", "forward"):
        await step(page.go_back, _id(2))
    if how == "forward":
        await step(page.go_forward, _id(7))


@both
async def test_opening_a_chat_of_my_own_messages_sends_one_read(auto: Auto) -> None:
    """020-AC2: the open itself owes the `read`, whoever wrote what the chat shows."""
    auto.fake.mine = True
    await auto.open()
    assert await _reads(auto) == [_id(2)]


@both
@pytest.mark.parametrize("size", [60, 1], ids=["scrolling", "short"])
async def test_someone_elses_message_sends_read_mine_does_not(auto: Auto, size: int) -> None:
    """020-AC3: a message from someone else in the shown chat, visible, focused, at the latest message,
    sends `read`, in a chat that scrolls as in one too short to; one of mine sends none."""
    auto.fake.size = size
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    auto.fake.verbs.clear()
    auto.live(1, me=True)
    assert await _reads(auto) == []
    auto.live(2)
    assert await _reads(auto) == [_id(2)]
    assert await page.locator(".msg").last.inner_text() != ""


@both
@pytest.mark.parametrize("away", ["hidden", "unfocused", "detached"])
async def test_no_read_while_not_shown_then_one_when_back(auto: Auto, away: str) -> None:
    """020-AC4: hidden, unfocused or scrolled up into older history, an arriving message sends nothing;
    back in view, focused, at the latest message, one `read` goes out."""
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    auto.fake.verbs.clear()
    leave: dict[str, dict[str, Any]] = {"hidden": {"vis": "hidden"}, "unfocused": {"focus": False}}
    back: dict[str, dict[str, Any]] = {"hidden": {"vis": "visible"}, "unfocused": {"focus": True}}
    if away == "detached":
        await _scroll(page, top=True)
    else:
        await _set(page, **leave[away])
    auto.live(1)
    auto.live(2)
    assert await _reads(auto) == []
    if away == "detached":
        await _scroll(page, top=False)
    else:
        await _set(page, **back[away])
    assert await _reads(auto) == [_id(2)]
    await _set(page, vis="visible", focus=True)  # back again with nothing new: no read owed
    assert await _reads(auto) == [_id(2)]


@both
async def test_burst_sends_one_now_and_one_trailing(auto: Auto) -> None:
    """020-AC5: a burst of messages sends at most one `read` per second per chat: the one in flight and
    one pending, which goes out after the burst."""
    page = await auto.open()
    await _reads(auto)  # the open's read is over a second old
    auto.fake.verbs.clear()
    for i in range(10):
        auto.live(i)
    await auto.fake.handled()
    await auto.fake.settled()
    assert auto.fake.reads() == [_id(2)]  # the first goes at once, the rest wait
    await past(page, READ_MS // 2)
    for i in range(10, 20):
        auto.live(i)
    await auto.fake.handled()
    await auto.fake.settled()
    assert auto.fake.reads() == [_id(2)]
    assert await _reads(auto) == [_id(2), _id(2)]  # the pending one, after the second
    assert await _reads(auto, 2 * READ_MS + 100) == [_id(2), _id(2)]  # and no other behind it


@both
async def test_read_only_chat_sends_no_read(auto: Auto) -> None:
    """020-AC2: a chat Teams will not mark (read-only: a channel, never on the board) gets no `read`."""
    real = auto.fake.on_verb

    def on_verb(msg: dict[str, Any]) -> None:
        if "follow" in msg:
            follow = {"follow": msg["follow"], "label": "x", "page": "1", "opener": False}
            auto.fake.send({**follow, "read_only": True})
        else:
            real(msg)

    auto.fake.on_verb = on_verb  # type: ignore[method-assign]
    await auto.open()
    auto.live(1)
    assert await _reads(auto) == []


@both
async def test_trailing_read_is_dropped_once_the_chat_is_left(auto: Auto) -> None:
    """020-AC5: the trailing `read` checks again when due: switched to another chat within the second,
    the left chat gets none (it would mark what landed after the reader left)."""
    page = await auto.open()
    assert await _reads(auto, 0) == [_id(2)]  # within the second still
    auto.live(1)
    await auto.fake.handled()
    await page.locator(f"#rows li[data-id='{_id(6)}'] .name").click()
    await wait(lambda: auto.fake.follows()[-1:] == [_id(6)])
    assert await _reads(auto) == [_id(2), _id(6)]


@both
async def test_trailing_read_waits_while_hidden(auto: Auto) -> None:
    """020-AC5: hidden before the trailing `read` is due, it is not sent; visible again, it goes."""
    page = await auto.open()
    assert await _reads(auto, 0) == [_id(2)]
    auto.live(1)
    await auto.fake.handled()
    await _set(page, vis="hidden")
    assert await _reads(auto) == [_id(2)]
    await _set(page, vis="visible")
    assert await _reads(auto) == [_id(2), _id(2)]


@both
async def test_a_read_that_could_not_be_sent_is_still_owed(auto: Auto) -> None:
    """020-AC4: back in view while the socket is closed, the `read` cannot go: it goes once the page
    has reconnected and shown the latest page again."""
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    await _set(page, vis="hidden")
    auto.live(1)
    assert await _reads(auto) == [_id(2)]
    follows = len(auto.fake.follows())
    await page.evaluate("window.__ws.close(); " + _VIS.replace("v =>", "(v =>") + ")('visible')")
    await wait(lambda: len(auto.fake.follows()) > follows)
    assert await _reads(auto) == [_id(2), _id(2)]


@both
async def test_messages_landed_during_a_drop_are_read(auto: Auto) -> None:
    """020-AC3: someone else's messages the reconnect's page brings (they landed while the socket was
    down, never relayed) send `read`; a page with nothing new sends none."""
    await auto.open()
    assert await _reads(auto) == [_id(2)]
    await _drop(auto)
    assert await _reads(auto) == [_id(2)]  # the same page again
    auto.fake.size += 2
    await _drop(auto)
    assert await _reads(auto) == [_id(2), _id(2)]


@both
async def test_an_owed_read_waits_for_the_reconnect_page(auto: Auto) -> None:
    """020-AC4: back in view while the reconnect's page is still asked, the old screen lacks what landed
    meanwhile: the owed `read` waits for that page."""
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    await _set(page, vis="hidden")
    auto.live(1)
    await auto.fake.handled()  # shown before the drop: a frame sent right before a close may be lost
    auto.fake.size += 2
    auto.fake.hold = True
    await _drop(auto)
    await _set(page, vis="visible")
    assert await _reads(auto) == [_id(2)]
    auto.fake.release(-1)
    assert await _reads(auto) == [_id(2), _id(2)]


@both
async def test_a_page_reached_by_a_jump_is_not_the_latest(auto: Auto) -> None:
    """020-AC4: detached by a jump (search hit), at the bottom of that page, a message landing sends
    nothing: newer messages lie between it and the latest."""
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    await page.locator("#find").fill("m10")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()
    await until(page, "window.__frames.some(f => 'around' in f)")
    auto.live(1)
    await _scroll(page, top=False)
    assert await _reads(auto) == [_id(2)]


async def _asked(auto: Auto, n: int) -> None:
    await wait(lambda: len(auto.fake.of("history")) >= n, f"{n} history asks")


@both
async def test_no_read_while_the_archive_lags(auto: Auto) -> None:
    """020-AC2: while the archive lags (its page lacks the row's last message, "loading latest messages…"
    shown), nothing is read: the server would mark that unseen message too."""
    auto.fake.lag = 5
    page = await auto.open()
    auto.fake.hold = True
    await _asked(auto, 2)  # the catch-up, held
    await page.locator("#lag").wait_for()
    auto.live(1)
    await _set(page, vis="visible", focus=True)
    assert await _reads(auto) == []
    assert await page.locator("#lag").is_visible()


@both
async def test_a_message_relayed_during_a_catch_up_is_read_once_it_lands(auto: Auto) -> None:
    """020-AC2, AC3: a chat opened while the archive lags, and a message from someone else relayed
    meanwhile, are read once the catch-up lands, though it adds nothing new: one `read`."""
    auto.fake.lag = 1
    await auto.open()
    auto.fake.hold = True
    await _asked(auto, 2)  # the catch-up, held
    auto.live(1)
    assert await _reads(auto) == []
    auto.fake.release(-1)
    assert await _reads(auto) == [_id(2)]


@both
async def test_a_catch_up_bringing_someone_elses_message_is_read(auto: Auto) -> None:
    """020-AC3: a reconnect's page lagging, its catch-up brings a message from someone else the relay
    never did: read once it lands."""
    await auto.open()
    assert await _reads(auto) == [_id(2)]
    auto.fake.lag = 1
    auto.fake.hold = True
    await _drop(auto)
    auto.fake.release(-1)  # the reconnect's page: `behind`
    auto.fake.size += 1
    await _asked(auto, 3)  # the catch-up, held
    assert await _reads(auto) == [_id(2)]
    auto.fake.release(-1)
    assert await _reads(auto) == [_id(2), _id(2)]


@both
async def test_no_read_while_a_busy_page_is_asked_again(auto: Auto) -> None:
    """020-AC4: the reconnect's page answered `busy`, a message relayed before it is asked again waits
    for that page: the screen is still the old one."""
    await auto.open()
    assert await _reads(auto) == [_id(2)]
    auto.fake.busy = 1
    await _drop(auto)
    auto.live(1)
    assert await _reads(auto) == [_id(2)]
    await _asked(auto, 3)  # the retry, held
    auto.fake.release(-1)
    assert await _reads(auto) == [_id(2), _id(2)]


@both
async def test_older_pages_scrolled_through_send_no_read(auto: Auto) -> None:
    """020-AC3: messages of an older page, loaded scrolling up, were there before the read marker:
    back at the latest message, no `read`."""
    auto.fake.older = True
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    await _scroll(page, top=True)
    await until(page, "window.__frames.some(f => 'before' in f && f.messages)")
    await _scroll(page, top=False)
    assert await _reads(auto) == [_id(2)]


@both
async def test_newer_pages_after_a_jump_are_read_at_the_latest(auto: Auto) -> None:
    """020-AC4: after a jump, the pages below it up to the latest message bring one from someone else
    newer than what the open showed: read once the reader reaches it."""
    auto.fake.after = True
    page = await auto.open()
    assert await _reads(auto) == [_id(2)]
    await page.locator("#find").fill("m10")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()
    await until(page, "window.__frames.some(f => 'around' in f)")
    await _scroll(page, top=False)
    await until(page, "window.__frames.some(f => 'after' in f)")
    await _scroll(page, top=False)
    assert await _reads(auto) == [_id(2), _id(2)]


async def _jump(page: Page) -> None:
    await page.locator("#find").fill("m10")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()


@both
async def test_a_failed_jump_while_lagging_keeps_catching_up(auto: Auto) -> None:
    """020-AC2: a jump that fails while the archive lags (it dropped the catch-up) asks the catch-up
    again: once it lands, the chat is read."""
    auto.fake.lag, auto.fake.jump_fails = 1, True
    page = await auto.open()
    await _jump(page)
    await until(page, "window.__frames.some(f => 'around' in f && f.error)")
    assert auto.fake.reads() == []
    await wait(lambda: auto.fake.reads() == [_id(2)], "read once the catch-up lands")
    assert await page.locator("#lag").is_hidden()


@both
async def test_a_busy_retry_of_a_chat_left_is_dropped(auto: Auto) -> None:
    """020-AC2: a `busy` latest page retried after the chat was left and opened again within the
    retry's delay: the reopening's own ask is the only one."""
    auto.fake.busy_hold = False
    page = await auto.open()
    six = page.locator(f"#rows li[data-id='{_id(6)}'] .name")
    two = page.locator(f"#rows li[data-id='{_id(2)}'] .name")
    await six.click()
    await wait(lambda: auto.fake.follows()[-1:] == [_id(6)])
    auto.fake.busy = 1
    await two.click()
    await wait(lambda: auto.fake.busy == 0, "the busy answer")
    await six.click()
    n = len(auto.fake.verbs)
    await two.click()
    await auto.fake.settled(700)  # past the dropped retry
    asks = [v for v in auto.fake.verbs[n:] if v == {"history": _id(2), "before": None}]
    assert len(asks) == 1


def _side(verb: dict[str, Any]) -> str:
    side = next((k for k in ("around", "after", "before") if k in verb), "")
    return "latest" if side == "before" and verb["before"] is None else side


def _asks(auto: Auto, since: int) -> list[str]:
    """The history asks since verb `since`: `latest`, or the side asked."""
    return [_side(v) for v in auto.fake.verbs[since:] if "history" in v]


@both
async def test_a_busy_retry_does_not_undo_a_jump(auto: Auto) -> None:
    """020-AC4: the reconnect's latest page answered `busy`, a jump within the retry's delay replaces
    it: no latest page is asked after the jump, which stays where it landed."""
    page = await auto.open()
    auto.fake.busy, auto.fake.busy_hold = 1, False
    n = len(auto.fake.verbs)
    await _drop(auto)
    await wait(lambda: auto.fake.busy == 0, "the busy answer")
    await _jump(page)
    await until(page, "window.__frames.some(f => 'around' in f)")
    await auto.fake.settled(700)  # past the dropped retry
    assert _asks(auto, n) == ["latest", "around"]
    assert await page.locator(".msg.hit").count() == 1
    await _scroll(page, top=False)  # the dropped retry's key is not left asked: the next page goes
    await wait(lambda: _asks(auto, n)[2:] == ["after"], "the page below the jump asked")


@both
async def test_a_failed_reconnect_page_while_lagging_keeps_catching_up(auto: Auto) -> None:
    """020-AC2: the archive lagging, a reconnect (which drops the catch-up) whose latest page fails asks
    the catch-up again: once it lands, the chat is read."""
    auto.fake.lag = 1
    page = await auto.open()
    auto.fake.fails = 1
    await _drop(auto)
    await until(page, "window.__frames.some(f => 'history' in f && f.error)")
    assert auto.fake.reads() == []
    await wait(lambda: auto.fake.reads() == [_id(2)], "read once the catch-up lands")
