"""Files in both composers (spec 025 phase 2) against a fake socket, in Chrome and Firefox: the
paperclip and drops attach file chips beside images, × removes one, the limits refuse with a clear
message, Enter sends text, images and files through the send queue (the files in chunked binary
frames), the queued line names them, a failure or a dropped socket keeps them retryable. Synthetic
data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_files.py`.
"""

import json
import re
from typing import Any

import browser_helpers
import pytest
from browser_helpers import ORIGIN, Page, Pages, act, open_fake, past, queue_is, until, wait
from test_browser_chat_compose import L
from test_browser_edit import _edit
from test_browser_images import FREED, WIDGET, Chat, Widget, _error_is, _open_widget, _thumbs
from test_browser_images import open_chat as _open_chat
from test_images import png

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)
CHUNK = 1 << 20  # ui.js FILE_CHUNK
SHOWN = "no answer from Teams: it may have gone out · ↻ checks the chat before sending again"

# Drops [[name, type, hex, padding bytes]] on `sel` (the composer), as a file manager would; whether the
# page took the drop over.
DROP = """([sel, files]) => {
  const dt = new DataTransfer();
  for (const [name, type, hex, pad] of files) {
    const bytes = new Uint8Array(hex.match(/../g)?.map((h) => parseInt(h, 16)) || []);
    dt.items.add(new File([bytes, new Uint8Array(pad)], name, { type }));
  }
  const target = document.querySelector(sel);
  target.dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }));
  const ev = new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true });
  target.dispatchEvent(ev);
  return ev.defaultPrevented;
}"""
CHIPS = (
    "JSON.stringify([...document.querySelectorAll('.imgs .chip')]"
    ".map((c) => [c.querySelector('.name').textContent, c.querySelector('small').textContent]))"
)


BIG_DROP = """([sel, mib, name]) => { const dt = new DataTransfer();
  dt.items.add(new File([new Uint8Array(mib << 20)], name));
  const ev = new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true });
  document.querySelector(sel).dispatchEvent(ev); return ev.defaultPrevented; }"""
# A socket draining MIB MiB/s, its buffer as the page sees it (the test's socket drains at once);
# window.__peak: the most it held.
SLOW = """(() => { const proto = WebSocket.prototype, real = proto.send, RATE = (MIB << 20) / 1000;
  let held = 0, at = performance.now(); window.__peak = 0;
  const now = () => {
    const t = performance.now(); held = Math.max(0, held - (t - at) * RATE); at = t; return held; };
  // A setter too: the test's stand-in socket sets its own.
  Object.defineProperty(proto, "bufferedAmount", { get: now, set() {}, configurable: true });
  proto.send = function (d) {
    if (typeof d !== "string") { now(); held += d.byteLength ?? d.size; }
    window.__peak = Math.max(window.__peak, held);
    return real.call(this, d); }; })();"""


async def _widget(pages: Pages, **kw: Any) -> tuple[Any, Page]:
    fake = Widget(**kw)
    return fake, await _open_widget(pages, fake)


async def _chat(pages: Pages, **kw: Any) -> tuple[Any, Page]:
    fake = Chat(**kw)
    page = await _open_chat(pages, fake, init=FREED)
    await page.locator("#reply").focus()
    return fake, page


async def open_fake_widget_with(pages: Pages, fake: Any, init: str) -> Page:
    page = await open_fake(
        pages,
        fake,
        ORIGIN + "/",
        WIDGET,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
        init=init,
    )
    await page.wait_for_selector("a.name")
    await page.locator("button.reply").dispatch_event("click")
    await page.wait_for_function("document.activeElement.id === 'reply'")
    return page


OPEN = {"widget": _widget, "chat": _chat}
ZONE = {"widget": "li.composer", "chat": "footer"}


async def _clip(page: Page, *files: tuple[str, str, bytes]) -> None:
    """Picks `files` (name, type, bytes) with the paperclip."""
    async with page.expect_file_chooser() as chooser:
        await page.locator("#clip").click()
    await (await chooser.value).set_files([{"name": n, "mimeType": t, "buffer": b} for n, t, b in files])


async def _drop(page: Page, where: str, *files: tuple[str, str, bytes, int]) -> bool:
    return bool(await page.evaluate(DROP, [where, [[n, t, b.hex(), pad] for n, t, b, pad in files]]))


async def _chips(page: Page, *expected: tuple[str, str]) -> None:
    await until(
        page, f"{CHIPS} === {json.dumps(json.dumps([list(e) for e in expected], separators=(',', ':')))}"
    )


def _bytes(fake: Any) -> bytes:
    return b"".join(fake.frames)


@BOTH
@pytest.mark.parametrize("where", ["widget", "chat"])
async def test_paperclip_chips_remove_and_send_through_the_queue(pages: Pages, where: str) -> None:
    """025-AC5: the paperclip attaches files as chips (name, human size); × removes one; Enter sends the
    text with the rest through the send queue: their names and sizes in the verb, then their bytes in
    chunked binary frames, in order; success clears the chips."""
    fake, page = await OPEN[where](pages)
    big = bytes(range(256)) * (5 * CHUNK // 512)  # 2.5 MiB: three frames
    await _clip(
        page, ("report.pdf", "application/pdf", b"%PDF-1.7"), ("data.bin", "application/octet-stream", big)
    )
    await _clip(page, ("drop me.txt", "text/plain", b"x" * 2048))
    await _chips(page, ("report.pdf", "8 B"), ("data.bin", "2.5 MiB"), ("drop me.txt", "2.0 KiB"))
    assert await page.locator(".imgs .temp").inner_text() == "attachments are lost on reload"
    await page.get_by_role("button", name="remove drop me.txt").click()
    await _chips(page, ("report.pdf", "8 B"), ("data.bin", "2.5 MiB"))
    box = page.locator("#reply")
    await box.press_sequentially("see attached")
    await box.press("Enter")
    await wait(lambda: len(_bytes(fake)) == 8 + len(big))
    [verb] = fake.of("send")
    assert verb["text"] == "see attached" and "images" not in verb
    assert verb["files"] == [{"name": "report.pdf", "size": 8}, {"name": "data.bin", "size": len(big)}]
    assert [len(f) for f in fake.frames] == [8, CHUNK, CHUNK, len(big) - 2 * CHUNK]
    assert _bytes(fake) == b"%PDF-1.7" + big
    await until(page, "!document.querySelector('.imgs .chip')")
    assert await box.input_value() == ""
    await pages.close()


@BOTH
@pytest.mark.parametrize("where", ["widget", "chat"])
async def test_a_drop_sends_images_inline_and_other_files_as_files(pages: Pages, where: str) -> None:
    """025-AC5: a drop on the composer attaches: an image Teams shows inline takes the inline image
    path (thumbnail, measured, one frame), anything else, an unsupported image type included, is a file
    chip; one message carries both, images first on the wire."""
    fake, page = await OPEN[where](pages)
    pic = png(4, 3)
    assert await _drop(
        page,
        ZONE[where],
        ("a.png", "image/png", pic, 0),
        ("b.pdf", "application/pdf", b"%PDF", 0),
        ("c.bmp", "image/bmp", b"BM", 0),
    )
    await _thumbs(page, 1)
    await _chips(page, ("b.pdf", "4 B"), ("c.bmp", "2 B"))
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.frames) == 3)
    [verb] = fake.of("send")
    assert verb["images"] == [{"w": 4, "h": 3, "size": len(pic)}]
    assert verb["files"] == [{"name": "b.pdf", "size": 4}, {"name": "c.bmp", "size": 2}]
    assert fake.frames == [pic, b"%PDF", b"BM"]
    await pages.close()


@BOTH
async def test_the_paperclip_sends_an_image_as_a_file(pages: Pages) -> None:
    """025-AC5: what the paperclip picks goes as a file card, an image too (Teams' paperclip does so)."""
    fake, page = await _widget(pages)
    await _clip(page, ("photo.png", "image/png", png()))
    await _chips(page, ("photo.png", f"{len(png())} B"))
    assert await page.locator(".imgs .thumb").count() == 0
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.frames) == 1)
    assert fake.of("send")[0]["files"] == [{"name": "photo.png", "size": len(png())}]
    await pages.close()


@BOTH
@pytest.mark.parametrize("where", ["widget", "chat"])
async def test_more_than_ten_files_or_one_over_100_mib_is_refused(pages: Pages, where: str) -> None:
    """025-AC6: an 11th file and a file over 100 MiB are refused with a clear message before anything
    is queued; the refusal clears once the files change back under the limit; the text stays."""
    fake, page = await OPEN[where](pages)
    box = page.locator("#reply")
    await box.press_sequentially("keep me")
    await _clip(page, *((f"f{i}.txt", "text/plain", b"x") for i in range(11)))
    await until(page, "document.querySelectorAll('.imgs .chip').length === 10")
    await _error_is(page, "at most 10 files per message")
    await page.get_by_role("button", name="remove f0.txt").click()
    await _error_is(page, "")
    # Built in the page: 100 MiB + 1 byte does not cross the test driver's wire.
    big = "new File([new Uint8Array((100 << 20) + 1)], 'huge.iso')"
    took = await page.evaluate(
        f"""(sel) => {{ const dt = new DataTransfer(); dt.items.add({big});
        const ev = new DragEvent("drop", {{ dataTransfer: dt, bubbles: true, cancelable: true }});
        document.querySelector(sel).dispatchEvent(ev); return ev.defaultPrevented; }}""",
        ZONE[where],
    )
    assert took
    await _error_is(page, "huge.iso: over 100 MiB")
    assert await page.locator(".imgs .chip").count() == 9
    assert await box.input_value() == "keep me"
    await fake.settled()
    assert fake.of("send") == []
    await pages.close()


@BOTH
@pytest.mark.parametrize("where", ["widget", "chat"])
async def test_the_image_count_notice_clears_once_back_under_the_limit(pages: Pages, where: str) -> None:
    """Reported bug (012-AC25): "at most 8 images per message" from a second drop goes away once an
    image is removed, and a file added meanwhile does not keep it."""
    fake, page = await OPEN[where](pages)
    await _drop(page, ZONE[where], *((f"{i}.png", "image/png", png(i + 1, 1), 0) for i in range(8)))
    await _thumbs(page, 8)
    await _drop(page, ZONE[where], ("9.png", "image/png", png(9, 1), 0))
    await _error_is(page, "at most 8 images per message")
    await page.locator(".imgs .thumb .rm").first.click()
    await _thumbs(page, 7)
    await _error_is(page, "")
    await pages.close()


@BOTH
@pytest.mark.parametrize("where", ["widget", "chat"])
async def test_the_queue_names_the_files_and_a_failure_keeps_them_for_retry(pages: Pages, where: str) -> None:
    """025-AC5, 017-AC3, 017-AC6: the queued line or bubble names its files; a failure shows its cause
    and keeps them; ↻ sends them again with the same id and the check."""
    fake, page = await OPEN[where](pages, replies=[{"error": "files not sent: Teams answered 507"}])
    await _clip(page, ("a.pdf", "application/pdf", b"%PDF"))
    box = page.locator("#reply")
    await box.press_sequentially("hi")
    await box.press("Enter")
    await queue_is(page, ("hi 📎 a.pdf", "failed: files not sent: Teams answered 507"))
    assert await box.input_value() == ""
    fake.frames.clear()  # a refusal may stop the stream early
    await act(page, "retry")
    await wait(lambda: len(fake.of("send")) == 2 and len(fake.frames) == 1)
    first, again = fake.of("send")
    assert again["files"] == first["files"] == [{"name": "a.pdf", "size": 4}]
    assert again.get("check") is True and fake.cmids[0] == fake.cmids[1]
    assert fake.frames == [b"%PDF"]
    await pages.close()


@BOTH
async def test_a_socket_lost_mid_stream_resends_the_files_checked(pages: Pages) -> None:
    """025-AC5, 017-AC13: the socket drops while a file streams: the message goes back to the queue and,
    once the socket is back, goes again whole with its id and the check, never twice blindly."""
    fake, page = await _widget(pages, replies=[None])
    data = b"y" * (3 * CHUNK)
    await _clip(page, ("big.bin", "application/octet-stream", data))
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(fake.of("send")) == 1 and len(fake.frames) >= 1)
    await fake.ws.close()
    await until(page, "window.__ws.readyState === 3")
    fake.frames.clear()
    await until(page, "window.__ws.readyState === 1")
    await wait(lambda: len(fake.of("send")) == 2 and _bytes(fake) == data)
    first, again = fake.of("send")
    assert again.get("check") is True and fake.cmids[0] == fake.cmids[1] and again["files"] == first["files"]
    await pages.close()


@BOTH
async def test_a_file_dropped_off_the_composer_does_not_leave_the_page(pages: Pages) -> None:
    """025-AC5: a near miss attaches nothing and does not navigate to the file (drafts and queue stay)."""
    fake, page = await _widget(pages)
    assert await _drop(page, "header", ("a.pdf", "application/pdf", b"%PDF", 0))
    await fake.settled()
    assert await page.locator(".imgs .chip").count() == 0
    await pages.close()


@BOTH
async def test_a_file_name_is_text_not_markup(pages: Pages) -> None:
    """025-AC5: a chip and a queued line show a file name as typed, never as markup."""
    fake, page = await _chat(pages, replies=[None])
    name = "<img src=x onerror=alert(1)>.txt"
    await _clip(page, (name, "text/plain", b"1"))
    await _chips(page, (name, "1 B"))
    await page.locator("#reply").press("Enter")
    await queue_is(page, (f"📎 {name}", "sending…"))
    assert await page.locator(".imgs img, .q img").count() == 0
    await pages.close()


@BOTH
async def test_an_edit_offers_no_paperclip(pages: Pages) -> None:
    """025-AC5: the chat page's editor copies the formatting bar without the paperclip: an edit keeps
    its attachments as they are."""
    fake, page = await _chat(pages)
    await _edit(fake)
    assert await page.locator("#tools #clip").count() == 1
    assert await page.locator(".editbox #clip, .editbox input[type=file]").count() == 0
    await pages.close()


@BOTH
async def test_a_refused_send_stops_its_stream(pages: Pages) -> None:
    """025-AC5: once the server refused the message, the rest of its file is not sent for nothing."""
    fake = Widget(replies=[{"error": "files not sent: Teams answered 507"}])
    page = await open_fake_widget_with(pages, fake, SLOW.replace("MIB", "64"))
    assert await page.evaluate(BIG_DROP, ["li.composer", 40, "big.bin"])
    await until(page, "document.querySelectorAll('.imgs .chip').length === 1")
    await page.locator("#reply").press("Enter")
    await queue_is(page, ("📎 big.bin", "failed: files not sent: Teams answered 507"))
    await past(page, 1000)  # 40 MiB at 64 MiB/s: all of it would be out by now
    await fake.settled()
    assert len(_bytes(fake)) < 20 << 20
    await pages.close()


@pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)
async def test_two_chats_files_never_interleave_on_the_wire(pages: Pages) -> None:
    """025-AC5: the server takes binary frames as the latest send's: a send for another chat waits
    until the previous one's files are all out."""
    log: list[str] = []

    class Logged(Chat):  # type: ignore[valid-type, misc]
        def _on_message(self, raw: str | bytes) -> None:
            log.append("frame" if isinstance(raw, bytes) else "verb" if "send" in json.loads(raw) else "")
            super()._on_message(raw)

    fake = Logged(replies=[None, None])
    page = await _open_chat(pages, fake, "?chatnav=split", init=FREED)
    await page.wait_for_selector(f'#rows li[data-id="{L}"]')
    assert await page.evaluate(BIG_DROP, ["footer", 40, "t.bin"])
    await until(page, "document.querySelectorAll('.imgs .chip').length === 1")
    await page.locator("#reply").press("Enter")
    await page.locator(f'#rows li[data-id="{L}"] a.name').click()
    assert await page.evaluate(BIG_DROP, ["footer", 1, "l.bin"])
    await until(page, "document.querySelectorAll('.imgs .chip').length === 1")
    await page.locator("#reply").press("Enter")
    await wait(lambda: log.count("verb") == 2 and log.count("frame") == 41, tries=1500)
    sends = [x for x in log if x]
    assert sends == ["verb"] + ["frame"] * 40 + ["verb", "frame"]
    await pages.close()


@pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)
async def test_a_big_file_streams_paced_on_the_socket_buffer(pages: Pages) -> None:
    """025-AC5: a large file is not queued whole in the socket's memory."""
    fake = Widget()
    page = await open_fake_widget_with(pages, fake, SLOW.replace("MIB", "4"))
    assert await page.evaluate(BIG_DROP, ["li.composer", 16, "big.bin"])
    await until(page, "document.querySelectorAll('.imgs .chip').length === 1")
    await page.locator("#reply").press("Enter")
    await wait(lambda: len(_bytes(fake)) == 16 << 20, tries=1500)
    assert await page.evaluate("window.__peak") <= (4 << 20) + CHUNK
    await pages.close()


@pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)
async def test_a_read_only_chat_takes_no_drop(pages: Pages) -> None:
    """025-AC5: a read-only chat's composer is off: a drop attaches nothing."""
    fake, page = await _chat(pages, read_only=True)
    await until(page, "document.getElementById('reply').disabled")
    assert await _drop(page, "footer", ("a.pdf", "application/pdf", b"%PDF", 0))  # the page's guard took it
    await fake.settled()
    assert await page.locator(".imgs .chip").count() == 0
    await pages.close()
