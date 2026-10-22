"""Pasted images in both composers (spec 012 phase 5b) against a fake socket: the queue, preview,
removal, per-chat isolation, limits, the `send` frame and its binary image frames, failures and
unsure sends. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_images.py`.
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
    ORIGIN,
    Page,
    Pages,
    T,
    act,
    open_fake,
    queue_is,
    until,
    wait,
)
from test_browser_chat_compose import Fake as ChatFake
from test_browser_chat_compose import L
from test_browser_chat_compose import _open as open_chat
from test_browser_compose import Fake as WidgetFake
from test_images import png

from miniteams import web as W

pytestmark = pytest.mark.browser
pages = browser_helpers.pages

# A module global read by the fixture: a mutation run swaps it for an altered copy.
WIDGET = W._page("widget.html")
# Object URLs the page releases, in order.
FREED = """(() => { window.__freed = []; const free = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = (u) => { window.__freed.push(u); free(u); };
  // window.__throwOnce: the next object URL fails, as a broken clipboard file might.
  const make = URL.createObjectURL.bind(URL);
  URL.createObjectURL = (b) => {
    if (window.__throwOnce) { window.__throwOnce = false; throw new Error("boom"); }
    return make(b); }; })();"""
# Pastes [[hex, type, padding bytes]] (and `text`) on #reply; true when the page took the paste over.
PASTE = """([files, text, html]) => {
  const dt = new DataTransfer();
  for (const [hex, type, pad] of files) {
    const bytes = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
    dt.items.add(new File([bytes, new Uint8Array(pad)], "pasted", { type }));
  }
  if (text) dt.setData("text/plain", text);
  if (html) dt.setData("text/html", html);
  const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
  document.getElementById("reply").dispatchEvent(ev);
  return ev.defaultPrevented;
}"""
UNSURE = "no answer from Teams: the message may have gone out"
SHOWN = "no answer from Teams: it may have gone out · ↻ checks the chat before sending again"  # the page's


def _replying(cls: type) -> type:
    """`cls` answering each `send` with the next of `replies` ({} fields; None: no answer), then ok."""

    @dataclass
    class Replying(cls):  # type: ignore[valid-type, misc]
        replies: list[dict[str, Any] | None] = field(default_factory=list)

        def on_verb(self, msg: dict[str, Any]) -> None:
            if "send" not in msg:
                super().on_verb(msg)
                return
            reply = self.replies.pop(0) if self.replies else {"ok": True}
            if reply is not None:
                self.send({"sent": msg["send"], **reply})

    return Replying


Widget = _replying(WidgetFake)
Chat = _replying(ChatFake)


async def _open_widget(pages: Pages, fake: Any) -> Page:
    page = await open_fake(
        pages,
        fake,
        ORIGIN + "/",
        WIDGET,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
        init=FREED,
    )
    await page.wait_for_selector("a.name")
    await page.locator("button.reply").dispatch_event("click")
    await page.wait_for_function("document.activeElement.id === 'reply'")
    return page


@pytest.fixture
async def widget(pages: Pages) -> AsyncIterator[Any]:
    fake = Widget()
    await _open_widget(pages, fake)
    yield fake
    await pages.close()
    assert pages.external == []


async def _paste(page: Page, *files: bytes | tuple[bytes, str, int], text: str = "", html: str = "") -> bool:
    spec = [[f.hex(), "image/png", 0] if isinstance(f, bytes) else [f[0].hex(), f[1], f[2]] for f in files]
    return bool(await page.evaluate(PASTE, [spec, text, html]))


async def _thumbs(page: Page, n: int) -> list[str]:
    """The current chat's thumbnail sources, once there are `n`."""
    await until(page, f"document.querySelectorAll('.imgs .thumb img').length === {n}")
    return list(await page.eval_on_selector_all(".imgs .thumb img", "els => els.map((e) => e.src)"))


def _err(page: Page) -> Any:
    return page.locator(".composer > small, #err")


async def _error_is(page: Page, text: str) -> None:
    """The composer's error line reads `text` (a paste is checked asynchronously)."""
    js = "document.querySelector('.composer > small, #err')?.textContent"
    await until(page, f"{js} === {json.dumps(text)}")


# --- queue: paste, order, preview, removal (012-AC19, 012-AC20) ---


async def test_paste_queues_a_thumbnail_and_sends_nothing(widget: Any) -> None:
    """012-AC19: a pasted image shows as a compact local thumbnail labelled temporary; nothing is sent
    or uploaded."""
    page = widget.page
    assert await _paste(page, png())
    [src] = await _thumbs(page, 1)
    assert src.startswith("blob:")
    assert await page.locator(".imgs .thumb img").evaluate("e => e.getBoundingClientRect().width") == 32
    assert await page.locator(".imgs .temp").inner_text() == "images are lost on reload"
    await widget.settled()
    assert widget.of("send") == [] and widget.frames == []


async def test_pastes_accumulate_in_order_and_follow_the_text(widget: Any) -> None:
    """012-AC20, 012-AC21: two images in one paste then a third accumulate in order; Enter sends the
    text with their measured sizes, then one binary frame per image in that order."""
    page, box = widget.page, widget.page.locator("#reply")
    one, two, three = png(3, 2), png(4, 2), png(5, 3)
    await _paste(page, one, two)
    await _thumbs(page, 2)
    await _paste(page, three)
    await _thumbs(page, 3)
    await box.press_sequentially("hi")
    await box.press("Enter")
    await wait(lambda: len(widget.frames) == 3)
    [verb] = widget.of("send")
    assert verb["text"] == "hi"
    assert verb["images"] == [
        {"w": 3, "h": 2, "size": len(one)},
        {"w": 4, "h": 2, "size": len(two)},
        {"w": 5, "h": 3, "size": len(three)},
    ]
    assert widget.frames == [one, two, three]
    assert len(widget.cmids) == 1 and widget.cmids[0].isdigit()


async def test_images_alone_send_and_success_releases_them(widget: Any) -> None:
    """012-AC21, 012-AC25: Enter sends images without text; success drops the queue and releases its
    object URLs."""
    page = widget.page
    await _paste(page, png())
    [src] = await _thumbs(page, 1)
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(widget.frames) == 1)
    assert widget.of("send")[0]["text"] == ""
    await until(page, f"window.__freed.includes({json.dumps(src)})")
    await page.locator("button.reply").dispatch_event("click")
    assert await page.locator(".imgs .thumb").count() == 0


async def test_enter_with_the_list_open_picks_and_does_not_send(widget: Any) -> None:
    """012-AC21: queued images change nothing to the completion rule: Enter picks, the next one sends."""
    page, box = widget.page, widget.page.locator("#reply")
    await _paste(page, png())
    await _thumbs(page, 1)
    await box.press_sequentially("@ann")
    await page.locator("#pop [role=option]").first.wait_for()
    await box.press("Enter")
    await widget.settled()
    assert widget.of("send") == []
    await box.press("Enter")
    await wait(lambda: len(widget.frames) == 1)
    assert widget.of("send")[0]["mentions"] == [{"name": "Ann Lee", "mri": "8:orgid:ann"}]


async def test_text_paste_is_left_to_the_textarea(widget: Any) -> None:
    """012-AC20: a text paste, even one carrying a picture of itself, is not taken over."""
    page = widget.page
    assert not await _paste(page, text="plain")
    assert not await _paste(page, png(), text="office text")
    await widget.settled()
    assert await page.locator(".imgs .thumb").count() == 0


async def test_preview_and_removal(widget: Any) -> None:
    """012-AC20, 012-AC25: a thumbnail opens a larger local preview; closing it keeps image and text;
    × and the preview's Remove drop one image each, release its URL, and Remove closes the preview."""
    page, box = widget.page, widget.page.locator("#reply")
    await box.press_sequentially("draft")
    await _paste(page, png(3, 2), png(4, 2), png(5, 2))
    first, second, third = await _thumbs(page, 3)
    await page.locator(".imgs .rm").first.click()
    assert await _thumbs(page, 2) == [second, third]
    await until(page, f"window.__freed.includes({json.dumps(first)})")
    await page.locator(".imgs .open").first.click()
    view = page.locator("dialog.imgview")
    assert await view.evaluate("d => d.open")
    assert await view.locator("img").get_attribute("src") == second
    # A 4x2 image previews larger than its thumbnail, whole.
    assert await view.locator("img").evaluate("e => e.getBoundingClientRect().width") >= 240
    await view.get_by_text("Close").click()
    assert not await view.evaluate("d => d.open")
    assert await _thumbs(page, 2) == [second, third] and await box.input_value() == "draft"
    await page.locator(".imgs .open").nth(1).click()
    await view.get_by_text("Remove").click()
    assert not await view.evaluate("d => d.open")
    assert await _thumbs(page, 1) == [second]
    assert await box.input_value() == "draft"
    await until(page, f"window.__freed.includes({json.dumps(third)})")
    await page.locator(".imgs .open").first.click()
    await page.keyboard.press("Escape")  # the native dialog closes on Escape, the image stays
    assert not await view.evaluate("d => d.open")
    assert await _thumbs(page, 1) == [second]


# --- limits (012-AC25) ---


async def test_limits_refuse_with_a_clear_error_and_keep_the_draft(widget: Any) -> None:
    """012-AC25: count, size, total and content limits refuse the image with a clear error; the text and
    the images already queued stay. A pasted non-image becomes a file chip (025-AC5)."""
    page, box = widget.page, widget.page.locator("#reply")
    await box.press_sequentially("keep me")
    await _paste(page, *(png(i + 1, 1) for i in range(9)))
    await _thumbs(page, 8)
    await _error_is(page, "at most 8 images per message")
    for _ in range(8):
        await page.locator(".imgs .rm").first.click()
    await _paste(page, (png(), "image/png", 10 << 20))
    await _error_is(page, "an image is over 10 MiB")
    await _paste(page, (b"%PDF-1.4", "application/pdf", 0))  # not an image: a file chip (025-AC5)
    await until(page, "document.querySelectorAll('.imgs .chip').length === 1")
    await _error_is(page, "")
    await _err(page).evaluate("e => { e.textContent = ''; }")
    freed = await page.evaluate("window.__freed.length")
    await _paste(page, (b"\0" * 64, "image/png", 0))  # an image label, no image inside
    await _error_is(page, "unsupported image: PNG, JPEG, GIF or WebP only")
    await until(page, f"window.__freed.length === {freed + 1}")  # its measuring URL released
    await _paste(page, *((png(i + 1, 1), "image/png", 9 << 20) for i in range(5)))
    await _thumbs(page, 4)
    await _error_is(page, "images are over 40 MiB in total")
    assert await box.input_value() == "keep me"
    await widget.settled()
    assert widget.of("send") == []


# --- failures and unsure sends (012-AC23) ---


async def test_a_failed_send_keeps_everything_and_retries_with_the_same_id(widget: Any) -> None:
    """012-AC23, 017-AC3, 017-AC6: a failure stays in the queue with its reason, its text and images, the
    composer free; ↻ retries it (same message id, so the server reuses its uploads), with its images
    again, and asks for the check (any failure may hide a posted message)."""
    page, box = widget.page, widget.page.locator("#reply")
    widget.replies = [{"error": "Teams answered 403"}]
    await box.press_sequentially("hi")
    await _paste(page, png())
    await _thumbs(page, 1)
    await box.press("Enter")
    await queue_is(page, ("hi", "failed: Teams answered 403"))
    assert await box.input_value() == ""
    await _thumbs(page, 0)
    widget.frames.clear()  # an answer before the bytes stops them (025): only the retry's count
    await act(page, "retry")
    await wait(lambda: len(widget.of("send")) == 2 and widget.frames == [png()])
    assert widget.cmids[0] == widget.cmids[1]
    assert "check" not in widget.of("send")[0] and widget.of("send")[1]["check"] is True


async def test_an_unsure_send_spins_and_the_retry_asks_for_a_check(widget: Any) -> None:
    """012-AC23, 017-AC1, 017-AC6: while unanswered the composer spins, its box empty and usable; Enter
    on it sends nothing more; an unsure answer says so, and ↻ asks the server to look for the same id
    first. An image pasted during a send is the next message's: it stays removable."""
    page, box = widget.page, widget.page.locator("#reply")
    widget.replies = [None, None]
    await _paste(page, png())
    await _thumbs(page, 1)
    await box.press("Enter")
    await until(page, "document.querySelector('.imgs').classList.contains('busy')")
    assert await page.locator(".imgs .temp").inner_text() == "sending…"
    assert not await box.is_disabled() and await box.input_value() == ""
    await _thumbs(page, 0)
    await box.press("Enter")
    await widget.settled()
    assert len(widget.of("send")) == 1
    widget.send({"sent": T, "error": UNSURE, "unsure": True})
    await queue_is(page, ("🖼", "failed: " + SHOWN))
    await _thumbs(page, 0)
    await act(page, "retry")
    await wait(lambda: len(widget.of("send")) == 2)
    assert widget.of("send")[1]["check"] is True and widget.cmids[0] == widget.cmids[1]
    await _paste(page, png(3, 2))
    await _thumbs(page, 1)
    assert await page.locator(".imgs .rm").is_enabled()
    await page.locator(".imgs .open").click()
    remove = page.locator("dialog.imgview").get_by_text("Remove")
    assert await remove.is_enabled()
    await remove.click()
    await _thumbs(page, 0)


async def test_a_lost_socket_mid_send_arms_the_check(pages: Pages) -> None:
    """012-AC23, 017-AC13 (supersedes the manual retry): no answer will come once the socket drops: the
    message goes back to the queue, no error, the box untouched; once the socket is back it goes again
    with its id and the check, never blindly, then the queued one."""
    fake = Widget(replies=[None])
    page = await _open_widget(pages, fake)
    box = page.locator("#reply")
    for text in ("hi", "two"):
        await box.press_sequentially(text)
        await box.press("Enter")
    await box.press_sequentially("next")
    await wait(lambda: len(fake.of("send")) == 1)
    await fake.ws.close()
    await until(page, "window.__ws.readyState === 3")
    assert await _err(page).inner_text() == "" and await box.input_value() == "next"
    await until(page, "window.__ws.readyState === 1", tries=500)
    await wait(lambda: len(fake.of("send")) == 3)
    await fake.settled(50)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["hi", "hi", "two"]
    assert [v.get("check", False) for v in sent] == [False, True, False]
    assert fake.cmids[0] == fake.cmids[1] != fake.cmids[2]
    assert await box.input_value() == "next"
    await pages.close()


# --- chat page: split isolation, the real CSP, teardown (012-AC22, 012-AC19, 012-AC25) ---


async def test_images_stay_with_their_chat_and_success_clears_only_its_own(pages: Pages) -> None:
    """012-AC22: under the real CSP (blob: thumbnails), images pasted in T stay in T across split
    switches; L has its own; T's answer, arriving while L is shown, clears only what T sent."""
    fake = Chat(replies=[None])
    page = await open_chat(pages, fake, "?chatnav=split", init=FREED)
    box = page.locator("#reply")
    await page.wait_for_selector(f'#rows li[data-id="{L}"]')
    await box.press_sequentially("for T")
    await _paste(page, png(3, 2))
    [t_img] = await _thumbs(page, 1)
    await until(page, "document.querySelector('.imgs img').naturalWidth === 3")
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    await _thumbs(page, 0)
    await _paste(page, png(4, 2))
    [l_img] = await _thumbs(page, 1)
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == T)
    assert await _thumbs(page, 1) == [t_img] and await box.input_value() == "for T"
    await box.press("Enter")
    await wait(lambda: len(fake.frames) == 1)
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == L)
    fake.send({"sent": T, "ok": True})
    await until(page, f"window.__freed.includes({json.dumps(t_img)})")
    assert await _thumbs(page, 1) == [l_img]
    await page.locator(f'#rows li[data-id="{T}"] a.name').click()
    await wait(lambda: fake.of("history")[-1]["history"] == T)
    await _thumbs(page, 0)
    assert await box.input_value() == ""
    assert fake.of("send")[0]["images"] == [{"w": 3, "h": 2, "size": len(png(3, 2))}]
    await page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide', {persisted: false}))")
    await until(page, f"window.__freed.includes({json.dumps(l_img)})")
    await page.context.close()
    assert pages.external == []


async def test_removing_an_image_clears_its_refusal(widget: Any) -> None:
    """012-AC25: the refusal is about the images as they were: removing one clears it."""
    page = widget.page
    await _paste(page, *(png(i + 1, 1) for i in range(9)))
    await _thumbs(page, 8)
    await _error_is(page, "at most 8 images per message")
    await page.locator(".imgs .rm").first.click()
    await _thumbs(page, 7)
    await _error_is(page, "")


async def test_a_refusal_cleared_by_a_change_leaves_a_newer_error_alone(pages: Pages) -> None:
    """012-AC25: clearing a refusal never erases an error the page showed since, in the chat page."""
    fake = Chat()
    page = await open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    await _paste(page, *(png(i + 1, 1) for i in range(9)))
    await _thumbs(page, 8)
    await _error_is(page, "at most 8 images per message")
    await page.locator(".imgs .rm").first.click()
    await _error_is(page, "")
    await _paste(page, *(png(i + 1, 2) for i in range(2)))
    await _error_is(page, "at most 8 images per message")
    await _err(page).evaluate("e => { e.textContent = 'offline, not sent'; }")
    await page.locator(".imgs .rm").first.click()
    await _thumbs(page, 7)
    await _error_is(page, "offline, not sent")


async def test_chat_page_preview_and_image_only_send(pages: Pages) -> None:
    """012-AC20, 012-AC21: the chat composer previews and sends images alone, like the widget."""
    fake = Chat()
    page = await open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    await _paste(page, png(6, 4))
    await _thumbs(page, 1)
    await page.locator(".imgs .open").click()
    assert await page.locator("dialog.imgview img").evaluate("e => e.naturalWidth") == 6
    await page.locator("dialog.imgview").get_by_text("Close").click()
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.frames) == 1)
    assert fake.of("send")[0]["text"] == "" and fake.frames == [png(6, 4)]
    await _thumbs(page, 0)
    await page.context.close()


async def _edit_after_unsure(fake: Any, page: Page) -> None:
    """An unsure answer, then the message taken back into the empty composer (✎, 017-AC6) and its text
    edited (then an image removed, then one added): each Enter is a new message (new id, no check),
    never swallowed by an "already sent"."""
    box = page.locator("#reply")
    fake.replies = [{"error": UNSURE, "unsure": True}] * 3
    await box.press_sequentially("hi")
    await _paste(page, png(3, 2), png(4, 2))
    await _thumbs(page, 2)
    await box.press("Enter")
    for text, n in (("hi", 1), ("hi again", 2), ("hi again", 3)):
        await queue_is(page, (text, "failed: " + SHOWN))
        await act(page, "edit")  # text and images back in the box
        await until(page, f"document.getElementById('reply').value === {json.dumps(text)}")
        if n == 1:
            await _thumbs(page, 2)
            await box.press_sequentially(" again")
        elif n == 2:
            await _thumbs(page, 2)
            await page.locator(".imgs .rm").first.click()
            await _thumbs(page, 1)
        else:
            await _thumbs(page, 1)
            await _paste(page, png(5, 2))
            await _thumbs(page, 2)
        await box.press("Enter")
        await wait(lambda n=n: len(fake.of("send")) == n + 1)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["hi", "hi again", "hi again", "hi again"]
    assert [len(v["images"]) for v in sent] == [2, 2, 1, 2]
    assert all("check" not in v for v in sent)
    assert len(set(fake.cmids)) == 4


async def test_an_edit_after_an_unsure_send_is_a_new_message(widget: Any) -> None:
    """012-AC23: widget composer."""
    await _edit_after_unsure(widget, widget.page)


async def test_an_edit_after_an_unsure_send_is_a_new_message_in_the_chat_page(pages: Pages) -> None:
    """012-AC23: chat page composer."""
    fake = Chat()
    page = await open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    await _edit_after_unsure(fake, page)
    await page.context.close()


# Paste then Enter in one task: the image is still being measured when the send starts.
ENTER = "new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})"
PASTE_ENTER = PASTE.replace(
    "return ev.defaultPrevented;",
    f"document.getElementById('reply').dispatchEvent({ENTER}); return ev.defaultPrevented;",
)


async def _paste_then_enter(fake: Any, page: Page) -> None:
    box = page.locator("#reply")
    await box.press_sequentially("now")
    await page.evaluate(PASTE_ENTER, [[[png(3, 2).hex(), "image/png", 0]], ""])
    await wait(lambda: len(fake.frames) == 1)
    assert fake.of("send")[0]["images"] == [{"w": 3, "h": 2, "size": len(png(3, 2))}]
    await until(page, "document.querySelectorAll('.imgs .thumb').length === 0")
    await page.evaluate(PASTE_ENTER, [[[png(4, 2).hex(), "image/png", 0]], ""])  # no text: the image alone
    await wait(lambda: len(fake.frames) == 2)
    assert fake.of("send")[1]["text"] == "" and fake.frames[1] == png(4, 2)


async def test_an_image_pasted_right_before_enter_goes_with_it(pages: Pages) -> None:
    """012-AC21: widget: Enter waits for an image still being measured; it is not left behind."""
    fake = Widget()
    page = await _open_widget(pages, fake)
    await _paste_then_enter(fake, page)
    await pages.close()


async def test_an_image_pasted_right_before_enter_goes_with_it_in_the_chat_page(pages: Pages) -> None:
    """012-AC21: chat page composer."""
    fake = Chat()
    page = await open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    await _paste_then_enter(fake, page)
    await page.context.close()


async def test_after_a_success_the_same_text_is_a_new_message(widget: Any) -> None:
    """012-AC23: once a draft went out, sending the same text again is a new message: new id, no
    check (a check would find the first one and skip it)."""
    page = widget.page
    await page.locator("#reply").press_sequentially("ok")
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(widget.of("send")) == 1)
    await page.locator("#reply").press_sequentially("ok")
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(widget.of("send")) == 2)
    assert [v["text"] for v in widget.of("send")] == ["ok", "ok"]
    assert "check" not in widget.of("send")[1] and widget.cmids[0] != widget.cmids[1]


async def test_a_text_only_send_spins_too(widget: Any) -> None:
    """012-AC23: every send shows the spinner while unanswered, images or not."""
    page, box = widget.page, widget.page.locator("#reply")
    widget.replies = [None]
    await box.press_sequentially("hi")
    await box.press("Enter")
    await until(page, "document.querySelector('.imgs').classList.contains('busy')")
    assert await page.locator(".imgs").is_visible()
    assert await page.locator(".imgs .temp").inner_text() == "sending…"


async def _retype_after_unsure(fake: Any, page: Page) -> None:
    box = page.locator("#reply")
    fake.replies = [{"error": UNSURE, "unsure": True}]
    await box.press_sequentially("same")
    await box.press("Enter")
    await queue_is(page, ("same", "failed: " + SHOWN))
    await act(page, "edit")
    await until(page, "document.getElementById('reply').value === 'same'")
    await box.press("Control+a")
    await box.press("Backspace")
    await box.press_sequentially("same")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 2)
    assert [v["text"] for v in fake.of("send")] == ["same", "same"]
    assert "check" not in fake.of("send")[1] and fake.cmids[0] != fake.cmids[1]


async def test_a_cleared_and_retyped_draft_is_a_new_message(widget: Any) -> None:
    """012-AC23, 017-AC6: widget: an unsure send taken back into the composer (✎), cleared, the same text
    typed again: a new message (new id, no check), which the lookup would otherwise mistake for the
    first one and drop."""
    await _retype_after_unsure(widget, widget.page)


async def test_a_cleared_and_retyped_draft_is_a_new_message_in_the_chat_page(pages: Pages) -> None:
    """012-AC23: chat page composer."""
    fake = Chat()
    page = await open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    await _retype_after_unsure(fake, page)
    await page.context.close()


async def _refused_paste_then_enter(fake: Any, page: Page) -> None:
    await page.evaluate(PASTE_ENTER, [[[(b"\0" * 64).hex(), "image/png", 0]], ""])
    await _error_is(page, "unsupported image: PNG, JPEG, GIF or WebP only")
    await until(page, "!document.getElementById('reply').disabled")
    await fake.settled(50)
    assert fake.of("send") == []


async def test_enter_with_only_a_refused_paste_sends_nothing(widget: Any) -> None:
    """012-AC25: widget: Enter waits for the paste; refused, nothing is sent and its reason stays."""
    await _refused_paste_then_enter(widget, widget.page)


async def test_enter_with_only_a_refused_paste_sends_nothing_in_the_chat_page(pages: Pages) -> None:
    """012-AC25: chat page composer."""
    fake = Chat()
    page = await open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    await _refused_paste_then_enter(fake, page)
    await page.context.close()


async def test_a_paste_that_throws_does_not_block_later_sends(widget: Any) -> None:
    """012-AC23: a paste that fails unexpectedly says so, and the next send still goes."""
    page, box = widget.page, widget.page.locator("#reply")
    await page.evaluate("window.__throwOnce = true")
    await _paste(page, png())
    await _error_is(page, "could not read the pasted image")
    await box.press_sequentially("hi")
    await box.press("Enter")
    await wait(lambda: len(widget.of("send")) == 1)
    assert widget.of("send")[0]["text"] == "hi" and "images" not in widget.of("send")[0]


# --- chat page: a burst of sends (012) ---


async def _burst(page: Page, *texts: str) -> None:
    box = page.locator("#reply")
    for text in texts:
        await box.press_sequentially(text)
        await box.press("Enter")


async def test_a_burst_of_enters_queues_in_order_from_a_usable_box(pages: Pages) -> None:
    """012: Enter empties the box at once, which stays usable; later messages queue, one in flight per
    chat, each with its own id and images, sent in order as the acks come."""
    fake = Chat(replies=[None] * 3)
    page = await open_chat(pages, fake, init=FREED)
    box = page.locator("#reply")
    await box.focus()
    await _burst(page, "one")
    await _paste(page, png(4, 2))
    await _thumbs(page, 1)
    await _burst(page, "two", "three")
    assert await box.input_value() == "" and not await box.is_disabled()
    await _thumbs(page, 0)
    await fake.settled(50)
    assert [v["text"] for v in fake.of("send")] == ["one"]
    fake.send({"sent": T, "ok": True})
    await wait(lambda: len(fake.of("send")) == 2)
    fake.send({"sent": T, "ok": True})
    await wait(lambda: len(fake.of("send")) == 3)
    fake.send({"sent": T, "ok": True})
    await fake.settled(50)
    sent = fake.of("send")
    assert [v["text"] for v in sent] == ["one", "two", "three"]
    assert ["images" in v for v in sent] == [False, True, False] and fake.frames == [png(4, 2)]
    assert len(set(fake.cmids)) == 3 and all("check" not in v for v in sent)


# --- a browser's "Copy image" of a GIF: a still PNG plus the GIF's address in text/html ---

GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
GIF_URL = "https://gifs.example/media/a.GIF?cid=1"
COPIED = f'<meta charset="utf-8"><img src="{GIF_URL.replace("&", "&amp;")}" alt="">'


async def _gif_route(page: Page, answer: bytes | int | None) -> list[str]:
    """`/gif` answering `answer` (bytes, a status, None: a network failure); returns the URLs asked."""
    asked: list[str] = []

    async def handle(route: Any) -> None:
        asked.append(route.request.url)
        if answer is None:
            await route.abort()
        elif isinstance(answer, int):
            await route.fulfill(status=answer, body="not found\n")
        else:
            await route.fulfill(body=answer, content_type="image/gif")

    await page.route(ORIGIN + "/gif?*", handle)
    return asked


async def test_a_copied_gif_is_pasted_as_the_original_fetched_by_the_server(widget: Any) -> None:
    """The still PNG is replaced by the GIF `/gif` fetched, even with Enter pressed while it is fetched."""
    page = widget.page
    asked = await _gif_route(page, GIF)
    await page.evaluate(PASTE_ENTER, [[[png(3, 2).hex(), "image/png", 0]], "", COPIED])
    await wait(lambda: len(widget.frames) == 1)
    assert asked == [f"{ORIGIN}/gif?url={quote(GIF_URL, safe='')}"]
    assert widget.of("send")[0]["images"] == [{"w": 1, "h": 1, "size": len(GIF)}]
    assert widget.frames == [GIF]
    # Firefox also puts the address in text/plain: still an image copy.
    assert await _paste(page, png(3, 2), text=GIF_URL, html=COPIED)
    await _thumbs(page, 1)
    assert len(asked) == 2


@pytest.mark.parametrize("raw", ["https://gifs.example/ä b.gif", "HTTPS://GIFS.example/a.gif"])
async def test_firefox_text_names_the_gif_unnormalized(widget: Any, raw: str) -> None:
    """Firefox's text/plain is the address as written: compared once normalized, still an image copy."""
    page = widget.page
    asked = await _gif_route(page, GIF)
    assert await _paste(page, png(3, 2), text=raw, html=f'<img src="{raw}">')
    await _thumbs(page, 1)
    assert len(asked) == 1


@pytest.mark.parametrize("answer", [404, 500, None])
async def test_a_failed_gif_fetch_falls_back_to_the_pasted_still(widget: Any, answer: int | None) -> None:
    """A refusal or a network failure: the still PNG of the clipboard, no error shown."""
    page, still = widget.page, png(3, 2)
    asked = await _gif_route(page, answer)
    assert await _paste(page, still, html=COPIED)
    await _thumbs(page, 1)
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(widget.frames) == 1)
    assert len(asked) == 1
    assert widget.frames == [still]
    assert await page.evaluate("document.querySelector('.composer > small, #err')?.textContent || ''") == ""


@pytest.mark.parametrize(
    "html",
    [
        "",
        '<img src="https://gifs.example/a.png">',
        '<img src="http://gifs.example/a.gif">',
        '<img src="https://gifs.example/a.gif"><img src="https://gifs.example/b.gif">',
        '<img src="https://gifs.example/a.png?name=a.gif">',
        '<img src="https://gifs.example/a.gifv">',
    ],
)
async def test_other_image_pastes_keep_the_clipboard_file(widget: Any, html: str) -> None:
    """No html, a non-GIF, a non-https GIF or two images: the pasted file as before, nothing fetched."""
    page, still = widget.page, png(3, 2)
    asked = await _gif_route(page, GIF)
    assert await _paste(page, still, html=html)
    await _thumbs(page, 1)
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(widget.frames) == 1)
    assert widget.frames == [still] and asked == []


async def test_a_text_paste_naming_another_address_stays_text(widget: Any) -> None:
    page = widget.page
    asked = await _gif_route(page, GIF)
    assert not await _paste(page, png(), text="see https://gifs.example/other.gif", html=COPIED)
    await widget.settled()
    assert await page.locator(".imgs .thumb").count() == 0 and asked == []
