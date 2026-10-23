"""Burst sends through the shared send queue (spec 017) against a fake socket: the widget's reply
stays open while its messages go out one at a time; a reload (a newer page, F5, or after a server
restart) keeps the queue in order, the message in flight going again with its id and the check, the
composer keeping only the draft. The queue shows (phase 2): bubbles on the chat page, which Teams'
echo replaces, lines under the widget's reply; a failure stays in it with ↻ ✎ ✕. Synthetic data, no
network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_queue.py`.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import browser_helpers
import pytest
import test_browser_chat_compose as chat_page
import test_browser_images as images
from browser_helpers import (  # skips without playwright
    ANN,
    ORIGIN,
    Page,
    Pages,
    T,
    act,
    open_fake,
    queue_is,
    rendered,
    row,
    shows,
    until,
    wait,
)
from test_browser_chat_compose import MINE, L
from test_browser_chat_compose import _open as open_chat
from test_browser_images import FREED, Chat, Widget, _error_is, _open_widget, _paste, _thumbs
from test_images import png

from miniteams import web as W

pytestmark = pytest.mark.browser
pages = browser_helpers.pages

OPEN = "document.getElementById('reply')?.isConnected && document.activeElement.id === 'reply'"


async def _burst(page: Page, *texts: str) -> None:
    """Each Enter empties the box at once, the reply open and focused (017-AC1)."""
    box = page.locator("#reply")
    for text in texts:
        await box.press_sequentially(text)
        await box.press("Enter")
        await until(page, f"{OPEN} && document.getElementById('reply').value === ''")


async def test_widget_burst_sends_one_at_a_time_and_stays_open(pages: Pages) -> None:
    """017-AC1, 017-AC2: with a message in flight, Enter queues the next one; they reach the server
    one at a time, in order, each with its own id; after the last ack the reply is open and empty."""
    fake = Widget(replies=[None] * 3)
    page = await _open_widget(pages, fake)
    await _burst(page, "one", "two", "three")
    await fake.settled(50)
    assert [v["text"] for v in fake.of("send")] == ["one"]
    fake.send({"sent": T, "ok": True})
    await wait(lambda: len(fake.of("send")) == 2)
    await fake.settled(50)
    assert len(fake.of("send")) == 2
    fake.send({"sent": T, "ok": True})
    await wait(lambda: len(fake.of("send")) == 3)
    fake.send({"sent": T, "ok": True})
    await until(page, "!document.querySelector('.imgs').classList.contains('busy')")
    await until(page, f"{OPEN} && document.getElementById('reply').value === ''")
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["one", "two", "three"]
    assert len(set(fake.cmids)) == 3 and all("check" not in v for v in sent)
    await pages.close()


WS = re.compile(r"^ws://127\.0\.0\.1:8/ws$")
LOST = "attachments not kept across the reload: add them again"


async def _queue_then_reload(fake: Any, page: Page, reload: dict[str, Any], *, pics: bool = False) -> None:
    """A message in flight (unanswered), one queued (`pics`: with an image, then an image alone), a typed
    draft, then the version reload."""
    box = page.locator("#reply")
    await box.focus()
    await box.press_sequentially("one")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    if pics:
        await _paste(page, png())
        await _thumbs(page, 1)
    await box.press_sequentially("two")
    await box.press("Enter")
    if pics:
        await _paste(page, png(3, 2))
        await _thumbs(page, 1)
        await box.press("Enter")
        await _thumbs(page, 0)
    await box.press_sequentially("draft")
    await page.evaluate("window.__same = 1")
    async with page.expect_event("load"):
        fake.send(reload)
    assert await page.evaluate("window.__same") is None


async def _went_out_once_in_order(fake: Any) -> None:
    """The one in flight again, same id and checked, then the queued one: nothing else."""
    await wait(lambda: len(fake.of("send")) == 3)
    await fake.settled(50)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["one", "one", "two"]
    assert [v.get("check", False) for v in sent] == [False, True, False]
    assert fake.cmids[0] == fake.cmids[1] != fake.cmids[2]
    assert all("images" not in v for v in sent[1:])


async def _widget_reopened(page: Page) -> Any:
    await page.wait_for_selector("a.name")
    await page.locator("button.reply").dispatch_event("click")
    await until(page, OPEN)
    return page.locator("#reply")


@pytest.mark.parametrize("pics", [False, True], ids=["text", "images"])
async def test_widget_reload_keeps_the_queue_and_the_draft_apart(pages: Pages, pics: bool) -> None:
    """017-AC12: a newer widget.html reloads the page with one message in flight and one queued: they
    go out again in order, the one in flight with its id and the check; the reply keeps only the
    draft. Images do not survive: their text goes with a note, an image alone is dropped."""
    fake = Widget(replies=[None])
    page = await _open_widget(pages, fake)
    await _queue_then_reload(fake, page, {"page": "2"}, pics=pics)
    box = await _widget_reopened(page)
    await _went_out_once_in_order(fake)
    assert await box.input_value() == "draft"
    assert await page.locator(".composer > small").inner_text() == (LOST if pics else "")
    assert await page.evaluate("sessionStorage.length") == 0
    await pages.close()


@pytest.mark.parametrize("pics", [False, True], ids=["text", "images"])
async def test_chat_page_reload_keeps_the_queue_and_the_draft_apart(pages: Pages, pics: bool) -> None:
    """017-AC12: chat page: the same reload, the same order and check, the draft alone in the box."""
    fake = Chat(replies=[None])
    page = await open_chat(pages, fake, init=FREED)
    await _queue_then_reload(fake, page, {"follow": T, "label": "Fictional T · 3p", "page": "2"}, pics=pics)
    await page.wait_for_selector('[data-id="1"]')
    await _went_out_once_in_order(fake)
    assert await page.locator("#reply").input_value() == "draft"
    assert await page.locator("#err").inner_text() == (LOST if pics else "")
    await page.context.close()


async def open_fake_widget(pages: Pages, fake: Any, init: str) -> Page:
    """The widget with `init` run before its script, its reply open."""
    page = await open_fake(
        pages, fake, ORIGIN + "/", images.WIDGET, page_route=ORIGIN + "/", ws_route=WS, init=FREED + init
    )
    await page.wait_for_selector("a.name")
    await page.locator("button.reply").dispatch_event("click")
    await until(page, OPEN)
    return page


def _verb(**over: Any) -> str:
    """A stored queue of one item whose verb is a valid one with `over` changed."""
    verb = {"send": T, "text": "x", "mentions": [], "format": "inline", **over}
    return json.dumps({"items": [{"verb": verb}]})


@pytest.mark.parametrize(
    "stored",
    ["{", _verb(send="19:other@thread.v2"), _verb(text=1), _verb(html="<b>"), _verb(mentions={"name": 1})],
    ids=["json", "other-chat", "text-type", "extra-key", "mentions-type"],
)
async def test_a_malformed_stored_queue_is_dropped(pages: Pages, stored: str) -> None:
    """017-AC12: what sessionStorage holds is checked like any input: a malformed entry sends nothing and
    breaks nothing; the page still sends. (A malformed pick alone leaves only itself out, spec 032.)"""
    fake = Widget()
    init = f"sessionStorage.setItem({json.dumps('queue:' + T)}, {json.dumps(stored)});"
    page = await open_fake_widget(pages, fake, init)
    await page.locator("#reply").press_sequentially("ok")
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    await fake.settled(50)
    assert [v["text"] for v in fake.of("send")] == ["ok"]
    await pages.close()


@dataclass
class Muted(Widget):
    """Leaves every `send` unanswered while `mute`."""

    mute: bool = True

    def on_verb(self, msg: dict[str, Any]) -> None:
        if not ("send" in msg and self.mute):
            super().on_verb(msg)


@dataclass
class MutedChat(Chat):
    mute: bool = True

    def on_verb(self, msg: dict[str, Any]) -> None:
        if not ("send" in msg and self.mute):
            super().on_verb(msg)


async def _restart(fake: Any, page: Page, reload: dict[str, Any]) -> None:
    """A message in flight, one queued, a draft; the socket drops: no error, the box untouched; the
    reconnect sends "one" again (unanswered), then a newer version reloads the page."""
    box = page.locator("#reply")
    await box.focus()
    for text in ("one", "two"):
        await box.press_sequentially(text)
        await box.press("Enter")
    await box.press_sequentially("draft")
    await wait(lambda: len(fake.of("send")) == 1)
    await fake.ws.close()
    await until(page, "window.__ws.readyState === 3")
    assert await page.locator(".composer > small, #err").inner_text() == ""
    assert await box.input_value() == "draft"
    await wait(lambda: len(fake.of("send")) == 2)
    fake.mute = False
    async with page.expect_event("load"):
        fake.send(reload)


async def _resent_checked_then_the_rest(fake: Any) -> None:
    """Every send of "one" after the first: its id, checked; then "two", once; nothing else."""
    await wait(lambda: bool(fake.of("send")) and fake.of("send")[-1]["text"] == "two")
    await fake.settled(50)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["one", "one", "one", "two"]
    assert [v.get("check", False) for v in sent] == [False, True, True, False]
    assert len(set(fake.cmids[:3])) == 1 and fake.cmids[3] != fake.cmids[0]


async def test_widget_restart_then_reload_keeps_the_queue_and_checks(pages: Pages) -> None:
    """017-AC13, 017-AC12: a deploy drops the socket (the message in flight requeued, no error), the
    reconnect resends it checked, a newer widget.html reloads: it goes once more with its id and the
    check, then the queued one; the reply keeps only the draft."""
    fake = Muted()
    page = await _open_widget(pages, fake)
    await _restart(fake, page, {"page": "2"})
    box = await _widget_reopened(page)
    await _resent_checked_then_the_rest(fake)
    assert await box.input_value() == "draft"
    await pages.close()


async def test_chat_page_restart_then_reload_keeps_the_queue_and_checks(pages: Pages) -> None:
    """017-AC13, 017-AC12: chat page: the same restart and reload, the same order and checks."""
    fake = MutedChat()
    page = await open_chat(pages, fake)
    await _restart(fake, page, {"follow": T, "label": "Fictional T · 3p", "page": "2"})
    await page.wait_for_selector('[data-id="1"]')
    await _resent_checked_then_the_rest(fake)
    assert await page.locator("#reply").input_value() == "draft"
    assert await page.locator("#err").inner_text() == ""
    await page.context.close()


@dataclass
class Versioned(Widget):
    """Leaves every `send` unanswered while `mute`; tells its `version` on every socket and records the
    socket (1, 2, …) each `send` arrived on."""

    mute: bool = True
    version: str = "1"
    sockets: int = 0
    on: list[int] = field(default_factory=list)

    def attach(self, ws: Any) -> None:
        browser_helpers.Fake.attach(self, ws)
        self.sockets += 1
        self.send(
            {"rows": [row(T, "Fictional chat")], "page": self.version, "opener": False, "browser": False}
        )

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "send" in msg:
            self.on.append(self.sockets)
        if not ("send" in msg and self.mute):
            super().on_verb(msg)


@dataclass
class VersionedChat(Chat):
    mute: bool = True
    sockets: int = 0
    on: list[int] = field(default_factory=list)

    def attach(self, ws: Any) -> None:
        super().attach(ws)
        self.sockets += 1

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "send" in msg:
            self.on.append(self.sockets)
        if not ("send" in msg and self.mute):
            super().on_verb(msg)


async def _in_flight_and_queued(fake: Any, page: Page) -> None:
    box = page.locator("#reply")
    await box.focus()
    for text in ("one", "two"):
        await box.press_sequentially(text)
        await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)


async def test_widget_newer_version_on_reconnect_reloads_before_any_resend(pages: Pages) -> None:
    """017-AC13, 017-AC12: the socket comes back with a newer widget.html: the stale page reloads
    without resending (nothing on the second socket); the new page sends "one" checked, then "two"."""
    fake = Versioned()
    page = await _open_widget(pages, fake)
    await _in_flight_and_queued(fake, page)
    fake.version, fake.mute = "2", False
    async with page.expect_event("load", timeout=10000):
        await fake.ws.close()
    await _resent_checked_then_two(fake)
    assert fake.on == [1, 3, 3]
    await pages.close()


async def test_chat_page_newer_version_on_reconnect_reloads_before_any_resend(pages: Pages) -> None:
    """017-AC13, 017-AC12: chat page: the follow answer names a newer chat.html: no resend before the
    reload, then "one" checked and "two"."""
    fake = VersionedChat()
    page = await open_chat(pages, fake)
    await _in_flight_and_queued(fake, page)
    fake.version, fake.mute = "2", False
    async with page.expect_event("load", timeout=10000):
        await fake.ws.close()
    await _resent_checked_then_two(fake)
    assert fake.on == [1, 3, 3]
    await page.context.close()


async def _resent_checked_then_two(fake: Any) -> None:
    await wait(lambda: len(fake.of("send")) == 3)
    await fake.settled(50)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["one", "one", "two"]
    assert [v.get("check", False) for v in sent] == [False, True, False]
    assert fake.cmids[0] == fake.cmids[1] != fake.cmids[2]


async def test_widget_resend_waiting_on_the_first_try_shows_nothing(pages: Pages) -> None:
    """017-AC13: the server holds a checked resend until its first try settles (a send outlives its
    socket): meanwhile no error, the box untouched; then "two" follows, nothing else."""
    fake = Versioned()
    page = await _open_widget(pages, fake)
    await _in_flight_and_queued(fake, page)
    await page.locator("#reply").press_sequentially("draft")
    await fake.ws.close()
    await wait(lambda: len(fake.of("send")) == 2)  # the checked resend, held
    await fake.settled(200)
    assert await page.locator(".composer > small").inner_text() == ""
    assert await page.locator("#reply").input_value() == "draft"
    fake.mute = False
    fake.send({"sent": T, "ok": True})  # the first try's outcome, answered to the resend
    await wait(lambda: len(fake.of("send")) == 3)
    await fake.settled(50)
    assert [v["text"] for v in fake.of("send")] == ["one", "one", "two"]
    assert await page.locator(".composer > small").inner_text() == ""
    await pages.close()


async def test_widget_f5_keeps_the_queue(pages: Pages) -> None:
    """017-AC12: a plain reload (F5): "one" goes again with its id and the check, then "two"."""
    fake = Widget(replies=[None])
    page = await _open_widget(pages, fake)
    await _in_flight_and_queued(fake, page)
    await page.reload()
    await _resent_checked_then_two(fake)
    await pages.close()


async def test_chat_page_f5_keeps_the_queue(pages: Pages) -> None:
    """017-AC12: chat page: the same plain reload."""
    fake = Chat(replies=[None])
    page = await open_chat(pages, fake)
    await _in_flight_and_queued(fake, page)
    await page.reload()
    await _resent_checked_then_two(fake)
    await page.context.close()


async def test_split_a_stored_queue_note_keeps_the_other_chats_stored_draft(pages: Pages) -> None:
    """017-AC12: split, after a reload: another chat's stored queue (an image dropped: a note) must
    not cost that chat its stored draft; switched to, it shows both."""
    stored = {
        "draft:" + L: json.dumps({"text": "keep me", "quotes": []}),
        "queue:" + L: json.dumps(
            {"items": [{"verb": {"send": L, "text": "two", "mentions": [], "format": "full"}, "pics": True}]}
        ),
    }
    init = "".join(f"sessionStorage.setItem({json.dumps(k)}, {json.dumps(v)});" for k, v in stored.items())
    init = f"if (!sessionStorage.getItem('seeded')) {{ sessionStorage.setItem('seeded', '1'); {init} }}"
    fake = Chat()
    page = await open_chat(pages, fake, "?chatnav=split", init=init)
    await page.wait_for_selector(f'#rows li[data-id="{L}"]')
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    assert await page.locator("#reply").input_value() == "keep me"
    assert await page.locator("#err").inner_text() == LOST
    await page.context.close()


async def test_a_stored_paused_queue_says_enter_sends_it(pages: Pages) -> None:
    """017-AC12: a queue stored paused (behind a failure) comes back paused, and says so; Enter sends it."""
    record = {
        "paused": True,
        "items": [{"verb": {"send": T, "text": "two", "mentions": [], "format": "inline"}}],
    }
    fake = Widget()
    page = await open_fake_widget(
        pages, fake, f"sessionStorage.setItem('queue:{T}', {json.dumps(json.dumps(record))});"
    )
    await _error_is(page, "1 queued: Enter sends them")
    await fake.settled(100)
    assert fake.of("send") == []
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "two"
    await pages.close()


@pytest.mark.parametrize(
    ("saved", "sent"),
    [
        ([{"name": "Ann Lee", "mri": "8:orgid:ann"}], []),  # saved before anchors
        (
            [{"name": "Ann Lee", "mri": "8:orgid:ann"}, {"name": "Bob Roe", "mri": "8:orgid:bob", "at": 8}],
            [8],
        ),
        ([{"name": "Ann Lee", "mri": 5, "at": 0}], []),
    ],
)
async def test_a_stored_message_keeps_its_text_when_a_pick_is_not_one(
    pages: Pages, saved: Any, sent: Any
) -> None:
    """032 review L2: a queue stored before anchors (or tampered with) keeps its message: only the picks that
    are not picks are left out, the rest go as stored, and its `@`s then resolve on the roster."""
    record = {
        "paused": True,
        "items": [{"verb": {"send": T, "text": "@Ann Lee @Bob Roe", "mentions": saved, "format": "inline"}}],
    }
    fake = Widget()
    page = await open_fake_widget(
        pages, fake, f"sessionStorage.setItem('queue:{T}', {json.dumps(json.dumps(record))});"
    )
    await _error_is(page, "1 queued: Enter sends them")
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "@Ann Lee @Bob Roe"
    assert [m["at"] for m in fake.of("send")[0]["mentions"]] == sent
    await pages.close()


# Image measuring held until `window.__release()`: a send whose images are still measured.
HOLD = """(() => { const Real = window.Image, held = [];
  window.__release = () => held.splice(0).forEach((f) => f());
  window.Image = class extends Real {
    set onload(f) { super.onload = () => held.push(() => f.call(this)); }
    get onload() { return super.onload; } }; })();"""


async def test_a_socket_back_while_images_are_measured_sends_once(pages: Pages) -> None:
    """017-AC13: the socket drops and comes back while the message's image is still measured: when the
    measure ends, it goes once, not once per pump."""
    fake = Widget()
    page = await open_fake_widget(pages, fake, HOLD)
    await _paste(page, png())
    await page.locator("#reply").press("Enter")
    await until(page, "document.querySelector('.imgs').classList.contains('busy')")
    await fake.ws.close()
    await until(page, "window.__ws.readyState === 3")
    await until(page, "window.__ws.readyState === 1", tries=500)
    await page.wait_for_selector("a.name")
    await page.evaluate("window.__release()")
    await wait(lambda: len(fake.of("send")) >= 1)
    await fake.settled(100)
    assert len(fake.of("send")) == 1 and len(fake.frames) == 1
    await pages.close()


# A stored draft with no picks, and an attempt a page before 017 phase 2 stored with it: ignored.
STALE = {"text": "kept", "quotes": [], "picked": [], "sent": {"cmid": "1", "json": "{}"}}


def _seed(stored: dict[str, Any]) -> str:
    """An init script storing `stored` (key → JSON value) once per tab, as a reload would have."""
    sets = "".join(
        f"sessionStorage.setItem({json.dumps(k)}, {json.dumps(json.dumps(v))});" for k, v in stored.items()
    )
    return f"if (!sessionStorage.getItem('seeded')) {{ sessionStorage.setItem('seeded', '1'); {sets} }}"


async def test_split_a_failure_for_a_chat_with_a_stored_draft_keeps_mentions_and_check(pages: Pages) -> None:
    """017-AC12, 017-AC3, 017-AC6: split, after a reload: another chat's queued message fails while its
    stored draft is not shown yet; switched to, its bubble says so, the draft untouched; ↻ retries it
    as it was: its mentions, its id, the check."""
    verb = {"send": L, "text": "@Ann Lee hi", "mentions": [{**ANN, "at": 0}], "format": "full"}
    init = _seed({"draft:" + L: STALE, "queue:" + L: {"items": [{"verb": verb}]}})
    fake = Chat(replies=[{"error": "boom"}])
    page = await open_chat(pages, fake, "?chatnav=split", init=init)
    await wait(lambda: len(fake.of("send")) == 1)
    await queue_is(page)  # another chat's bubble is never shown
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    await queue_is(page, ("@Ann Lee hi", "failed: boom"))
    assert await page.locator("#reply").input_value() == "kept"
    await act(page, "retry")
    await wait(lambda: len(fake.of("send")) == 2)
    retry = fake.of("send")[1]
    assert (
        retry["mentions"] == [{**ANN, "at": 0}] and retry["check"] is True and fake.cmids[0] == fake.cmids[1]
    )
    await page.context.close()


@dataclass
class TwoChats(Widget):
    """Two rows; leaves every `send` unanswered while `mute`."""

    mute: bool = True

    def attach(self, ws: Any) -> None:
        browser_helpers.Fake.attach(self, ws)
        rows = [row(T, "Fictional T"), row(L, "Fictional L")]
        self.send({"rows": rows, "page": "1", "opener": False, "browser": False})

    def on_verb(self, msg: dict[str, Any]) -> None:
        if not ("send" in msg and self.mute):
            super().on_verb(msg)


async def test_a_chat_page_opened_from_the_widget_takes_only_its_chats_queue(pages: Pages) -> None:
    """017-AC12: the widget's tab moves to T's chat page (non-split) with a message in flight for T and
    one for L: the chat page sends T's only (checked); L's stays stored and goes, checked, once the
    tab is back on the widget."""
    widget = TwoChats()
    page = await open_fake(
        pages, widget, ORIGIN + "/", images.WIDGET, page_route=ORIGIN + "/", ws_route=WS, init=FREED
    )
    await page.wait_for_selector(f'li[data-id="{L}"]')
    await page.locator(f'li[data-id="{T}"] button.reply').dispatch_event("click")
    await until(page, OPEN)
    box = page.locator("#reply")
    await box.press_sequentially("for T")
    await box.press("Enter")
    await page.locator(f'li[data-id="{L}"] button.reply').dispatch_event("click")
    await box.press_sequentially("for L")
    await box.press("Enter")
    await wait(lambda: len(widget.of("send")) == 2)
    chat = Chat()
    csp = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": W._page_csp(chat_page.PAGE)}

    async def chat_html(route: Any) -> None:
        await route.fulfill(body=chat_page.PAGE, headers=csp)

    await page.context.route_web_socket(re.compile(r"^ws://127\.0\.0\.1:8/ws\?chat$"), chat.attach)
    await page.context.route(re.compile(r"^http://127\.0\.0\.1:8/chat/[^/]+$"), chat_html)
    chat.page = page
    await page.goto(f"{ORIGIN}/chat/{quote(T, safe='')}")
    await wait(lambda: len(chat.of("send")) == 1)
    await chat.settled(100)
    assert [(v["send"], v["text"], v.get("check")) for v in chat.of("send")] == [(T, "for T", True)]
    assert await page.evaluate(f"sessionStorage.getItem({json.dumps('queue:' + L)}) !== null")
    widget.mute = False
    await page.goto(ORIGIN + "/")
    await wait(lambda: len(widget.of("send")) == 3)
    await widget.settled(100)
    last = widget.of("send")[2]
    assert (last["send"], last["text"], last.get("check")) == (L, "for L", True)
    assert widget.cmids[2] == widget.cmids[1]
    await pages.close()


# --- showing the queue (phase 2): bubbles, lines, a failure kept with ↻ ✎ ✕ ---

BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)
MARKUP = "<b>two</b> & <img src=x>"
BOTTOM = "(l => l.scrollHeight - l.scrollTop - l.clientHeight < 2)(document.getElementById('list'))"


async def _send(page: Page, *texts: str) -> None:
    box = page.locator("#reply")
    await box.focus()
    for text in texts:
        await box.press_sequentially(text)
        await box.press("Enter")


@BOTH
async def test_chat_page_bubbles_show_the_queue_in_order(pages: Pages) -> None:
    """017-AC4, 017-AC10: each message queued or in flight shows as a greyed bubble after the last
    message, in send order, labelled; its text is plain: markup typed in it never renders."""
    fake = Chat(replies=[None] * 3)
    page = await open_chat(pages, fake)
    await _send(page, "one", MARKUP, "three")
    await queue_is(page, ("one", "sending…"), (MARKUP, "queued"), ("three", "queued"))
    assert await page.evaluate(
        "(([m, q]) => !q.hidden && m.nextElementSibling === q"
        " && q.getBoundingClientRect().top >= m.lastElementChild.getBoundingClientRect().bottom)"
        "([document.getElementById('msgs'), document.getElementById('outq')])"
    )
    assert await page.locator("#outq b, #outq img").count() == 0
    assert await page.locator("#outq button:visible").count() == 0  # actions only on a failure
    assert float(await page.locator("#outq .qtext").first.evaluate("e => getComputedStyle(e).opacity")) < 1
    fake.send({"sent": T, "ok": True})
    await queue_is(page, ("one", "sent"), (MARKUP, "sending…"), ("three", "queued"))
    await page.context.close()


# Counts, after every change under #list, the bubbles plus the echo (id 9): one, never zero or two.
WATCH = """() => { window.__seen = [];
  const n = () => document.querySelectorAll('#outq .q, #msgs [data-id="9"]').length;
  new MutationObserver(() => window.__seen.push(n()))
    .observe(document.getElementById('list'), { childList: true, subtree: true, characterData: true }); }"""


@BOTH
@pytest.mark.parametrize("order", ["ack-then-echo", "echo-then-ack", "history"])
async def test_chat_page_echo_replaces_its_bubble(pages: Pages, order: str) -> None:
    """017-AC5: Teams' echo of a sent message (same clientmessageid), live or in a history page, before
    or after the server's ack, replaces its bubble: at every step exactly one of the two shows."""
    fake = Chat(replies=[None])
    page = await open_chat(pages, fake)
    await _send(page, "hi")
    await wait(lambda: len(fake.of("send")) == 1)
    await queue_is(page, ("hi", "sending…"))
    await page.evaluate(WATCH)
    echo = {**MINE, "id": "9", "at": "2026-10-09T10:05:00.000Z", "html": "<p>hi</p>", "cmid": fake.cmids[0]}
    if order == "echo-then-ack":
        fake.send({"thread": T, "msg": echo})
        await page.wait_for_selector('[data-id="9"]')
        await queue_is(page)
        fake.send({"sent": T, "ok": True})
    else:
        fake.send({"sent": T, "ok": True})
        await queue_is(page, ("hi", "sent"))
        if order == "history":
            fake.history = [*fake.history, echo]
            fake.send({"gap": T})
        else:
            fake.send({"thread": T, "msg": echo})
        await page.wait_for_selector('[data-id="9"]')
    await fake.handled()
    await queue_is(page)
    seen = await page.evaluate("window.__seen")
    assert seen and set(seen) == {1}
    await page.context.close()


async def test_chat_page_bubble_of_an_ack_without_echo_leaves_after_30_s(pages: Pages) -> None:
    """017-AC5: a send acknowledged whose echo never comes keeps its bubble 30 s, then it leaves: the
    message went out, the next history read shows it."""
    fake = Chat()
    page = await open_chat(pages, fake)
    await page.clock.install()
    await _send(page, "hi")
    await queue_is(page, ("hi", "sent"))
    await page.clock.fast_forward(29_000)
    await fake.handled()
    await queue_is(page, ("hi", "sent"))
    await page.clock.fast_forward(2_000)
    await queue_is(page)
    await page.context.close()


async def _failure_stays(fake: Any, page: Page) -> None:
    """ "one" in flight, "two" queued, a draft typed; "one" fails."""
    await _send(page, "one", "two")
    box = page.locator("#reply")
    await box.press_sequentially("draft")
    await wait(lambda: len(fake.of("send")) == 1)
    fake.send({"sent": T, "error": "boom"})
    await queue_is(page, ("one", "failed: boom"), ("two", "paused"))
    assert await box.input_value() == "draft"
    assert await page.locator(".composer > small, #err").inner_text() == ""


async def _stays_paused(fake: Any, page: Page, reload: bool) -> None:
    """017-AC3, 017-AC12: a failed send keeps its place, labelled with its reason, the draft untouched,
    across a reload too (its ↻ ✎ ✕ back); what is behind it, and a message sent meanwhile, waits
    paused; Enter on an empty box does not resume it."""
    await _failure_stays(fake, page)
    box = page.locator("#reply")
    if reload:
        await page.reload()
        if await page.evaluate("location.pathname === '/'"):
            box = await _widget_reopened(page)
        else:
            await page.wait_for_selector('[data-id="1"]')
        await queue_is(page, ("one", "failed: boom"), ("two", "paused"))
        assert await page.locator(".q.failed button:visible").evaluate_all(
            "bs => bs.map((b) => b.title)"
        ) == [
            "retry",
            "edit",
            "discard",
        ]
        assert await box.input_value() == "draft"
        assert await page.locator(".composer > small, #err").inner_text() == ""
    await box.press("Enter")
    await queue_is(page, ("one", "failed: boom"), ("two", "paused"), ("draft", "paused"))
    await box.press("Enter")
    await fake.settled(100)
    assert [v["text"] for v in fake.of("send")] == ["one"]


RELOAD = pytest.mark.parametrize("reload", [False, True], ids=["live", "reloaded"])


@BOTH
@RELOAD
async def test_chat_page_failure_stays_in_place_and_pauses_the_rest(pages: Pages, reload: bool) -> None:
    """017-AC3: chat page."""
    fake = Chat(replies=[None] * 3)
    page = await open_chat(pages, fake)
    await _stays_paused(fake, page, reload)
    await page.context.close()


@RELOAD
async def test_widget_failure_stays_in_place_and_pauses_the_rest(pages: Pages, reload: bool) -> None:
    """017-AC3: widget."""
    fake = Widget(replies=[None] * 3)
    page = await _open_widget(pages, fake)
    await _stays_paused(fake, page, reload)
    await pages.close()


async def _tab_order(page: Page, widget: bool) -> None:
    """The failed item's actions are buttons in the Tab order, ↻ ✎ ✕."""
    if widget:  # right after the reply's box
        await page.locator("#reply").focus()
        keys = ["Tab"] * 3
    else:  # right before the formatting bar
        await page.locator("#tools [data-fmt=bold]").focus()
        keys = ["Shift+Tab"] * 3
    seen = []
    for key in keys:
        await page.keyboard.press(key)
        seen.append(await page.evaluate("document.activeElement.title"))
    assert seen == (["retry", "edit", "discard"] if widget else ["discard", "edit", "retry"])


async def _acts(fake: Any, page: Page, action: str, widget: bool) -> None:
    """017-AC6: on the failed "one" (a mention picked, an image unless edited in place), ↻ retries it
    (same id, checked), ✕ drops it (its image freed), ✎ takes it into the empty composer, images too
    (sent unchanged, even after a reload: a retry) or, the composer holding a draft, edits it in place
    (Escape or Enter on an emptied text cancels, Enter sends the edit as a new message, unchanged as a
    retry); then "two" goes, in order."""
    box = page.locator("#reply")
    await box.focus()
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await box.press_sequentially("one")
    # Firefox takes no file in a synthetic paste: the images' path is the same script, run in Chrome.
    pic = not action.startswith("edit-in-place") and page.context.browser.browser_type.name == "chromium"
    if pic:
        await _paste(page, png())
        [url] = await _thumbs(page, 1)
    await box.press("Enter")
    await _send(page, "two")
    if action.startswith("edit-in-place"):
        await box.press_sequentially("draft")
    await wait(lambda: len(fake.of("send")) == 1)
    fake.send({"sent": T, "error": "boom"})
    await queue_is(page, ("@Ann Lee one", "failed: boom"), ("two", "paused"))
    await _tab_order(page, widget)
    first = ("@Ann Lee one", [{**ANN, "at": 0}])
    if action == "retry":
        await act(page, "retry")
        expected = [(first, True)]
    elif action == "discard":
        await act(page, "discard")
        await queue_is(page, ("two", "sending…"))
        if pic:
            await until(page, f"window.__freed.includes({json.dumps(url)})")
        expected = []
    elif action.startswith("edit-empty"):
        await act(page, "edit")
        await queue_is(page, ("two", "paused"))
        await until(page, "(e => e.id === 'reply' && e.value === '@Ann Lee one')(document.activeElement)")
        await _thumbs(page, int(pic))
        await fake.settled(50)
        assert len(fake.of("send")) == 1
        if action == "edit-empty-reload":  # its image is lost, its attempt kept with the draft
            await page.reload()
            if widget:
                box = await _widget_reopened(page)
            else:
                await page.wait_for_selector('[data-id="1"]')
            assert await box.input_value() == "@Ann Lee one"
            await box.focus()
        await box.press("Enter")
        expected = [(first, True)]
    else:
        await act(page, "edit")
        editor = page.locator(".q textarea")
        await until(page, "document.activeElement.matches('.q textarea')")
        assert await editor.input_value() == "@Ann Lee one"
        await editor.press("Escape")
        await queue_is(page, ("@Ann Lee one", "failed: boom"), ("two", "paused"))
        assert await page.evaluate("document.activeElement.title") == "edit"
        await act(page, "edit")
        await editor.press("Control+a")
        await editor.press("Backspace")
        await editor.press("Enter")  # nothing left to send: cancelled
        await queue_is(page, ("@Ann Lee one", "failed: boom"), ("two", "paused"))
        assert await page.locator(".q textarea").count() == 0
        await act(page, "edit")
        if action == "edit-in-place-same":  # unchanged: a retry
            await editor.press("Enter")
            expected = [(first, True)]
        else:
            await editor.press("End")
            await editor.press_sequentially(" fixed")
            await editor.press("Enter")
            expected = [(("@Ann Lee one fixed", [{**ANN, "at": 0}]), False)]
    await wait(lambda: len(fake.of("send")) == 2)  # the retry, the edit, or "two" once "one" is dropped
    if expected:
        fake.send({"sent": T, "ok": True})
        await wait(lambda: len(fake.of("send")) == 3)
    await fake.settled(50)
    sent = fake.of("send")
    assert [(v["text"], v["mentions"]) for v in sent] == [first, *(e for e, _ in expected), ("two", [])]
    assert [v.get("check", False) for v in sent] == [False, *(c for _, c in expected), False]
    assert (fake.cmids[0] == fake.cmids[1]) == (bool(expected) and expected[0][1])
    assert len(set(fake.cmids)) == len(sent) - (1 if expected and expected[0][1] else 0)
    assert ["images" in v for v in sent[:2]] == [pic, pic and action in ("retry", "edit-empty")]
    assert await box.input_value() == ("draft" if action.startswith("edit-in-place") else "")


ACTIONS = pytest.mark.parametrize(
    "action", ["retry", "discard", "edit-empty", "edit-empty-reload", "edit-in-place", "edit-in-place-same"]
)


@BOTH
@ACTIONS
async def test_chat_page_failed_bubble_actions(pages: Pages, action: str) -> None:
    """017-AC6: chat page."""
    fake = Chat(replies=[None] * 4)
    page = await open_chat(pages, fake, init=FREED)
    await _acts(fake, page, action, widget=False)
    await page.context.close()


@ACTIONS
async def test_widget_failed_line_actions(pages: Pages, action: str) -> None:
    """017-AC6: widget."""
    fake = Widget(replies=[None] * 4)
    page = await _open_widget(pages, fake)
    await _acts(fake, page, action, widget=True)
    await pages.close()


async def test_widget_lines_show_the_queue_and_leave_on_ack(pages: Pages) -> None:
    """017-AC7, 017-AC10: under the open reply, one truncated line per message queued or in flight,
    labelled like the bubbles, its text plain; a line leaves when its send is acknowledged."""
    fake = Widget(replies=[None] * 3)
    page = await _open_widget(pages, fake)
    long = "three " * 100
    await _burst(page, "one", MARKUP, long)
    await queue_is(page, ("one", "sending…"), (MARKUP, "queued"), (long, "queued"))
    assert await page.locator("li.composer .qlines .q").count() == 3
    assert await page.locator(".qlines b, .qlines img").count() == 0
    assert await page.locator(".qlines button:visible").count() == 0
    assert await page.locator(".qlines .q .qtext").nth(2).evaluate("e => e.scrollWidth > e.clientWidth")
    fake.send({"sent": T, "ok": True})
    await queue_is(page, (MARKUP, "sending…"), (long, "queued"))
    await pages.close()


@BOTH
async def test_chat_page_reader_at_the_bottom_stays_there(pages: Pages) -> None:
    """017-AC11: a reader at the bottom stays there when bubbles appear, change (a long failure
    reason, an editor resized) and leave."""
    fake = Chat(replies=[None] * 2)
    at = "2026-10-09T10:{:02d}:00.000Z"
    fake.history = [{**MINE, "id": str(i), "at": at.format(i), "html": f"<p>m{i}</p>"} for i in range(1, 60)]
    page = await open_chat(pages, fake, wait_for='[data-id="59"]')
    assert await page.evaluate("(l => l.scrollHeight > 2 * l.clientHeight)(document.getElementById('list'))")
    await until(page, BOTTOM)
    long = "word " * 150
    await _send(page, long, "two")
    await queue_is(page, (long, "sending…"), ("two", "queued"))
    await rendered(page)
    assert await page.evaluate(BOTTOM)
    fake.send({"sent": T, "error": "reason " * 60})
    await until(page, "document.querySelector('.q.failed') !== null")
    await rendered(page)
    assert await page.evaluate(BOTTOM)
    await page.locator("#reply").press_sequentially("draft")
    await act(page, "edit")  # in place: the box holds a draft
    await rendered(page)
    await page.locator(".q textarea").evaluate("e => { e.style.height = '300px'; }")  # dragged by its handle
    await rendered(page)
    assert await page.evaluate(BOTTOM)
    await page.locator(".q textarea").press("Escape")
    await act(page, "discard")
    await queue_is(page, ("two", "sending…"))
    await rendered(page)
    assert await page.evaluate(BOTTOM)
    await page.context.close()


@dataclass
class Jumping(Chat):
    """A search hit on message 1, whose `around` page has newer messages below: the view detaches."""

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "search" in msg:
            self.send(
                {
                    "search": T,
                    "q": msg["q"],
                    "hits": [{"id": "1", "at": MINE["at"], "name": "Me", "text": "x"}],
                }
            )
        elif "around" in msg or "after" in msg:  # still short of the latest message
            side = "around" if "around" in msg else "after"
            page = {"history": T, side: msg[side], "messages": self.history if side == "around" else []}
            self.send({**page, "source": "archive", "newer": True})
        else:
            super().on_verb(msg)


async def test_chat_page_bubbles_hide_while_detached_from_the_latest_message(pages: Pages) -> None:
    """017-AC4: the bubbles follow the chat's last message: a jump back in history, which hides it,
    hides them; back at the latest message they show again."""
    fake = Jumping(replies=[None])
    page = await open_chat(pages, fake)
    await _send(page, "hi")
    await queue_is(page, ("hi", "sending…"))
    await page.locator("#find").fill("x")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()
    await until(page, "!document.getElementById('latest').hidden")
    assert await page.locator("#outq").is_hidden()
    await page.locator("#latest").click()
    await until(page, "!document.getElementById('outq').hidden")
    await queue_is(page, ("hi", "sending…"))
    await page.context.close()


@dataclass
class LateFollow(Chat):
    """Answers `follow` only once `late` is released: the history page lands first."""

    late: list[dict[str, Any]] = field(default_factory=list)
    holding: bool = True

    def on_verb(self, msg: dict[str, Any]) -> None:
        if "follow" in msg and self.holding:
            self.late.append(msg)
        else:
            super().on_verb(msg)


async def test_chat_page_echo_before_the_resend_after_a_reload_replaces_its_bubble(pages: Pages) -> None:
    """017-AC5, 017-AC12: a message in flight at a reload comes back queued with its attempt; its echo,
    in the history page read before the socket's follow (so before the resend), replaces its bubble;
    the resend then goes checked and no bubble comes back."""
    verb = {"send": T, "text": "hi", "mentions": [], "format": "full"}
    sent = {"cmid": "1789466500123", "json": json.dumps(verb, separators=(",", ":"))}
    fake = LateFollow(replies=[{"ok": True}])
    echo = {**MINE, "id": "9", "at": "2026-10-09T10:05:00.000Z", "html": "<p>hi</p>", "cmid": sent["cmid"]}
    fake.history = [*fake.history, echo]
    page = await open_chat(pages, fake, init=_seed({"queue:" + T: {"items": [{"verb": verb, "sent": sent}]}}))
    await page.wait_for_selector('[data-id="9"]')
    await queue_is(page)
    assert fake.of("send") == []
    fake.holding = False
    fake.on_verb(fake.late.pop())
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0].get("check") is True and fake.cmids == [sent["cmid"]]
    await fake.handled()
    await queue_is(page)
    await page.context.close()


@pytest.mark.parametrize("cmid", ["abc", "1</b>"], ids=["letters", "markup"])
async def test_a_malformed_stored_draft_attempt_is_ignored(pages: Pages, cmid: str) -> None:
    """017-AC12: a stored draft's kept attempt is data from sessionStorage: a malformed id is dropped,
    the draft goes as a new message."""
    verb = {"send": T, "text": "hi", "mentions": [], "format": "full"}
    draft = {
        "text": "hi",
        "quotes": [],
        "sent": {"cmid": cmid, "json": json.dumps(verb, separators=(",", ":"))},
    }
    fake = Chat()
    page = await open_chat(pages, fake, init=_seed({"draft:" + T: draft}))
    box = page.locator("#reply")
    assert await box.input_value() == "hi"
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert "check" not in fake.of("send")[0] and fake.cmids[0].isdigit()
    await page.context.close()


UNSURE_ERR = {"sent": T, "error": "no answer from Teams", "unsure": True}


def _echo_of(cmid: str) -> dict[str, Any]:
    return {
        "thread": T,
        "msg": {**MINE, "id": "9", "at": "2026-10-09T10:05:00.000Z", "html": "<p>hi</p>", "cmid": cmid},
    }


@BOTH
@pytest.mark.parametrize("order", ["failure-then-echo", "echo-then-failure"])
async def test_chat_page_a_failure_teams_echoed_is_done(pages: Pages, order: str) -> None:
    """017-AC5 (never both), 017-AC3: an unsure failure whose attempt Teams echoes, before or after the
    failure lands, did go out: no bubble beside the message, and "two" behind it goes out."""
    fake = Chat(replies=[None] * 2)
    page = await open_chat(pages, fake, init=FREED)
    await _send(page, "hi", "two")
    await wait(lambda: len(fake.of("send")) == 1)
    frames = [UNSURE_ERR, _echo_of(fake.cmids[0])]
    for frame in frames if order == "failure-then-echo" else frames[::-1]:
        fake.send(frame)
        await fake.handled()
    await wait(lambda: len(fake.of("send")) == 2)
    assert fake.of("send")[1]["text"] == "two"
    await queue_is(page, ("two", "sending…"))
    assert await page.locator('#msgs [data-id="9"]').count() == 1
    await page.context.close()


@BOTH
async def test_chat_page_an_edit_in_place_outlives_the_old_echo(pages: Pages) -> None:
    """017-AC5 (never neither): the failed message edited in place keeps its editor when its old
    attempt's echo lands; the edit goes under a new id and its bubble shows while it is in flight."""
    fake = Chat(replies=[None] * 2)
    page = await open_chat(pages, fake)
    await _send(page, "hi")
    await wait(lambda: len(fake.of("send")) == 1)
    first = fake.cmids[0]
    fake.send({"sent": T, "error": "boom"})
    await queue_is(page, ("hi", "failed: boom"))
    await page.locator("#reply").press_sequentially("draft")
    await act(page, "edit")
    editor = page.locator(".q textarea")
    await editor.press("End")
    await editor.press_sequentially(" fixed")
    fake.send(_echo_of(first))
    await page.wait_for_selector('[data-id="9"]')
    await fake.handled()
    assert await editor.input_value() == "hi fixed"
    await editor.press("Enter")
    await wait(lambda: len(fake.of("send")) == 2)
    assert fake.cmids[1] != first and "check" not in fake.of("send")[1]
    await queue_is(page, ("hi fixed", "sending…"))
    await page.context.close()


@BOTH
async def test_chat_page_a_resend_of_an_echoed_message_shows_no_bubble(pages: Pages) -> None:
    """017-AC5 (never both): echoed while out of the queue (✎ into the composer), the message sent again
    unchanged goes under its id, checked: it is the message on screen, no bubble beside it, acked or not."""
    fake = Chat(replies=[None] * 2)
    page = await open_chat(pages, fake)
    await _send(page, "hi")
    await wait(lambda: len(fake.of("send")) == 1)
    first = fake.cmids[0]
    fake.send({"sent": T, "error": "boom"})
    await queue_is(page, ("hi", "failed: boom"))
    await act(page, "edit")
    await until(page, "document.getElementById('reply').value === 'hi'")
    fake.send(_echo_of(first))
    await page.wait_for_selector('[data-id="9"]')
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.of("send")) == 2)
    assert fake.cmids[1] == first and fake.of("send")[1]["check"] is True
    await queue_is(page)
    fake.send({"sent": T, "ok": True})
    await fake.handled()
    await queue_is(page)
    assert await page.locator('#msgs [data-id="9"]').count() == 1
    await page.context.close()


async def test_chat_page_an_echo_on_screen_hides_the_resend_of_its_id(pages: Pages) -> None:
    """017-AC5 (never both): a message whose echo is already on screen goes again under its id (checked,
    after a reload): no bubble beside the message while it is in flight, nor once acked."""
    verb = {"send": T, "text": "hi", "mentions": [], "format": "full"}
    sent = {"cmid": "1789466500123", "json": json.dumps(verb, separators=(",", ":"))}
    fake = LateFollow(replies=[None])
    fake.history = [*fake.history, _echo_of(sent["cmid"])["msg"]]
    page = await open_chat(pages, fake, init=_seed({"queue:" + T: {"items": [{"verb": verb, "sent": sent}]}}))
    await page.wait_for_selector('[data-id="9"]')
    fake.holding = False
    fake.on_verb(fake.late.pop())
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["check"] is True and fake.cmids == [sent["cmid"]]
    await fake.handled()
    await queue_is(page)
    fake.send({"sent": T, "ok": True})
    await fake.handled()
    await queue_is(page)
    await page.context.close()


@BOTH
async def test_chat_page_escape_on_an_edit_dropped_by_its_echo_returns_to_the_composer(pages: Pages) -> None:
    """017-AC6: Escape on the in-place edit of a failure whose old attempt Teams echoed meanwhile: the
    message counts as sent and leaves; the focus goes back to the composer, not to nowhere."""
    fake = Chat(replies=[None] * 2)
    page = await open_chat(pages, fake)
    await _send(page, "hi", "two")
    await wait(lambda: len(fake.of("send")) == 1)
    fake.send({"sent": T, "error": "boom"})
    await queue_is(page, ("hi", "failed: boom"), ("two", "paused"))
    await page.locator("#reply").press_sequentially("draft")
    await act(page, "edit")
    editor = page.locator(".q textarea")
    await editor.press_sequentially(" more")
    fake.send(_echo_of(fake.cmids[0]))
    await page.wait_for_selector('[data-id="9"]')
    await editor.press("Escape")
    await queue_is(page, ("two", "sending…"))
    assert await page.evaluate("document.activeElement.id") == "reply"
    await page.context.close()


async def _long_reason(fake: Any, page: Page, width: int, scroller: str) -> None:
    """At `width` px, a failure with a long reason: the message text keeps room, nothing scrolls
    sideways, the label's title holds the whole reason."""
    await page.set_viewport_size({"width": width, "height": 700})
    await _send(page, "hello there")
    await wait(lambda: len(fake.of("send")) == 1)
    reason = "no answer from Teams: it may have gone out · ↻ checks the chat before sending again"
    fake.send({"sent": T, "error": reason})
    await until(page, "document.querySelector('.q.failed') !== null")
    await rendered(page)
    assert await page.locator(".q.failed .qtext").evaluate("e => e.getBoundingClientRect().width") > 60
    assert await page.locator(".q.failed .qstate").get_attribute("title") == "failed: " + reason
    for sel in (scroller, "html"):
        assert await page.locator(sel).evaluate("e => e.scrollWidth <= e.clientWidth"), sel


@BOTH
async def test_chat_page_a_long_failure_reason_wraps_in_a_narrow_window(pages: Pages) -> None:
    """017-AC3: chat page at 420 px."""
    fake = Chat(replies=[None])
    page = await open_chat(pages, fake)
    await _long_reason(fake, page, 420, "#list")
    await page.context.close()


async def test_widget_a_long_failure_reason_leaves_the_text_room(pages: Pages) -> None:
    """017-AC3, 017-AC7: widget at 360 px."""
    fake = Widget(replies=[None])
    page = await _open_widget(pages, fake)
    await _long_reason(fake, page, 360, "#rows")
    await pages.close()


async def test_chat_page_an_echo_held_while_detached_still_counts(pages: Pages) -> None:
    """017-AC5, 017-AC3: jumped back in history, the echo of a message in flight waits unshown; the
    failure that follows still finds it: the message counts as sent, "two" goes out."""
    fake = Jumping(replies=[None] * 2)
    page = await open_chat(pages, fake)
    await _send(page, "hi", "two")
    await wait(lambda: len(fake.of("send")) == 1)
    await page.locator("#find").fill("x")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()
    await until(page, "!document.getElementById('latest').hidden")
    fake.send(_echo_of(fake.cmids[0]))
    await fake.handled()
    assert await page.locator('#msgs [data-id="9"]').count() == 0  # held until the latest page
    fake.send(UNSURE_ERR)
    await wait(lambda: len(fake.of("send")) == 2)
    assert fake.of("send")[1]["text"] == "two"
    await page.context.close()


async def test_chat_page_someone_elses_message_with_the_same_id_is_no_echo(pages: Pages) -> None:
    """017-AC5: only my own message is the echo: another sender's message under the same
    clientmessageid leaves the bubble in place."""
    fake = Chat(replies=[None])
    page = await open_chat(pages, fake)
    await _send(page, "hi")
    await wait(lambda: len(fake.of("send")) == 1)
    other = _echo_of(fake.cmids[0])
    other["msg"] = {**other["msg"], "from": "8:orgid:ann", "name": "Ann Lee", "me": False}
    fake.send(other)
    await page.wait_for_selector('[data-id="9"]')
    await queue_is(page, ("hi", "sending…"))
    fake.send({"sent": T, "ok": True})
    await queue_is(page, ("hi", "sent"))
    await page.context.close()


UPDATES = {"edit": {"html": "<p>hi again</p>"}, "delete": {"html": "", "deleted": True}}


@pytest.mark.parametrize("update", list(UPDATES))
async def test_chat_page_an_edit_of_my_message_keeps_it_the_echo(pages: Pages, update: str) -> None:
    """017-AC5: an edit or delete of my echoed message relayed without its clientmessageid is still
    that message: the next send shows only its own bubble, not the old one's back beside it."""
    fake = Chat(replies=[{"ok": True}, None])
    page = await open_chat(pages, fake)
    await _send(page, "hi")
    await queue_is(page, ("hi", "sent"))
    echo = _echo_of(fake.cmids[0])
    fake.send(echo)
    await page.wait_for_selector('[data-id="9"]')
    await queue_is(page)
    fake.send({"thread": T, "edit": {**echo["msg"], **UPDATES[update], "cmid": ""}})
    await fake.handled()
    await _send(page, "two")
    await queue_is(page, ("two", "sending…"))
    await page.context.close()


async def test_chat_page_an_edit_held_while_detached_keeps_it_the_echo(pages: Pages) -> None:
    """017-AC5, 017-AC3: jumped back in history, my message's echo then its edit (no clientmessageid)
    wait unshown; the unsure failure that follows still finds the echo: sent, "two" goes out."""
    fake = Jumping(replies=[None] * 2)
    page = await open_chat(pages, fake)
    await _send(page, "hi", "two")
    await wait(lambda: len(fake.of("send")) == 1)
    await page.locator("#find").fill("x")
    await page.locator("#find").press("Enter")
    await page.locator("#hits button").click()
    await until(page, "!document.getElementById('latest').hidden")
    echo = _echo_of(fake.cmids[0])
    fake.send(echo)
    fake.send({"thread": T, "edit": {**echo["msg"], **UPDATES["edit"], "cmid": ""}})
    await fake.handled()
    fake.send(UNSURE_ERR)
    await wait(lambda: len(fake.of("send")) == 2)
    assert fake.of("send")[1]["text"] == "two"
    await page.context.close()
