"""Burst sends through the shared send queue (spec 017 phase 1) against a fake socket: the widget's
reply stays open while its messages go out one at a time, failures keep PR #43's behaviour, and a
reload (a newer page, F5, or after a server restart) keeps the queue in order, the message in flight
going again with its id and the check, the composer keeping only the draft. Synthetic data, no
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
    open_fake,
    row,
    until,
    wait,
)
from test_browser_chat_compose import L
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


@pytest.mark.parametrize("retry", [True, False], ids=["retried", "dropped"])
async def test_widget_failure_in_a_burst_comes_back_and_holds_the_rest(pages: Pages, retry: bool) -> None:
    """017-AC1 (phase 1 keeps PR #43's failure): the failed message returns to the box ahead of the
    newer draft, the rest waits; Enter retries it unchanged (checked, same id) or drops it from an
    emptied box; then the rest goes, in order, the reply open throughout."""
    fake = Widget(replies=[None] * 3)
    page = await _open_widget(pages, fake)
    box = page.locator("#reply")
    await _burst(page, "one", "two", "three")
    fake.send({"sent": T, "ok": True})
    await wait(lambda: len(fake.of("send")) == 2)
    draft = "" if retry else "new"  # an edit after the failure would make it a new message
    await box.press_sequentially(draft)
    fake.send({"sent": T, "error": "boom"})
    await _error_is(page, "boom · 1 more queued after it")
    assert await box.input_value() == "\n".join(filter(None, ["two", draft]))
    await fake.settled(50)
    assert len(fake.of("send")) == 2
    if not retry:
        await box.press("Control+a")
        await box.press("Backspace")
    await box.press("Enter")
    if retry:
        await wait(lambda: len(fake.of("send")) == 3)
        fake.send({"sent": T, "ok": True})
    await wait(lambda: len(fake.of("send")) == (4 if retry else 3))
    await fake.settled(50)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == (["one", "two", "two", "three"] if retry else ["one", "two", "three"])
    assert [v.get("check", False) for v in sent] == ([False, False, True, False] if retry else [False] * 3)
    assert len(set(fake.cmids)) == 3
    assert await page.evaluate(OPEN) and await box.input_value() == ""


WS = re.compile(r"^ws://127\.0\.0\.1:9/ws$")
LOST = "images not kept across the reload: paste them again"


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
    ["{", _verb(send="19:other@thread.v2"), _verb(text=1), _verb(html="<b>"), _verb(mentions=[{"name": 1}])],
    ids=["json", "other-chat", "text-type", "extra-key", "mention"],
)
async def test_a_malformed_stored_queue_is_dropped(pages: Pages, stored: str) -> None:
    """017-AC12: what sessionStorage holds is checked like any input: a malformed entry sends nothing and
    breaks nothing; the page still sends."""
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


# A stored draft with no picks and another message's attempt: neither may outlive a failure merged in.
STALE = {"text": "", "quotes": [], "picked": [], "sent": {"cmid": "1", "json": "{}"}}


def _seed(stored: dict[str, Any]) -> str:
    """An init script storing `stored` (key → JSON value) once per tab, as a reload would have."""
    sets = "".join(
        f"sessionStorage.setItem({json.dumps(k)}, {json.dumps(json.dumps(v))});" for k, v in stored.items()
    )
    return f"if (!sessionStorage.getItem('seeded')) {{ sessionStorage.setItem('seeded', '1'); {sets} }}"


async def test_split_a_failure_for_a_chat_with_a_stored_draft_keeps_mentions_and_check(pages: Pages) -> None:
    """017-AC12: split, after a reload: another chat's queued message fails while its stored draft is
    not shown yet; switched to, Enter retries it as it was: its mentions, its id, the check."""
    verb = {"send": L, "text": "@Ann Lee hi", "mentions": [ANN], "format": "full"}
    init = _seed({"draft:" + L: STALE, "queue:" + L: {"items": [{"verb": verb}]}})
    fake = Chat(replies=[{"error": "boom"}])
    page = await open_chat(pages, fake, "?chatnav=split", init=init)
    await wait(lambda: len(fake.of("send")) == 1)
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    box = page.locator("#reply")
    assert await box.input_value() == "@Ann Lee hi"
    await box.focus()
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 2)
    retry = fake.of("send")[1]
    assert retry["mentions"] == [ANN] and retry["check"] is True and fake.cmids[0] == fake.cmids[1]
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

    await page.context.route_web_socket(re.compile(r"^ws://127\.0\.0\.1:9/ws\?chat$"), chat.attach)
    await page.context.route(re.compile(r"^http://127\.0\.0\.1:9/chat/[^/]+$"), chat_html)
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
