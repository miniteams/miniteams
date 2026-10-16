"""Plumbing for the browser test files (`test_browser*.py`): the playwright gate, the browser and its
pages, a fake board socket, and waits. Importing it skips the importing module without playwright.
"""

import asyncio
import contextlib
import json
import os
import re
import resource
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

CHROME = Path("/usr/bin/google-chrome")
# e2e layers must not silently skip in CI: there a missing playwright or Chrome fails instead.
if os.environ.get("CI", "").lower() not in ("1", "true"):
    pytest.importorskip("playwright.async_api", reason="run with `uv run --with playwright`")
    if not CHROME.exists():
        pytest.skip(f"{CHROME} missing", allow_module_level=True)

from conftest import auth_cookie, wait  # noqa: E402
from playwright.async_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Error,
    Page,
    Route,
    async_playwright,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"  # pragma: allowlist secret
)
# Each socket frame lands in window.__frames once the page's own handler ran (setTimeout: after it);
# every frame the page sends lands in window.__out (a binary one as {binary: <bytes>}); window.__ws is the
# page's latest socket.
FRAMES = """(() => { const Real = WebSocket; window.__frames = []; window.__out = [];
  window.WebSocket = class extends Real { constructor(...a) { super(...a); window.__ws = this;
    this.addEventListener("message", (e) => setTimeout(() => window.__frames.push(JSON.parse(e.data)))); }
    send(d) { window.__out.push(typeof d === "string" ? JSON.parse(d) : { binary: d.size ?? d.byteLength });
      super.send(d); } };
})();"""
# An element's text contrast against what is under it (backgrounds and opacity up its parents).
CONTRAST = """e => {
  const rgba = (c) => { const n = c.match(/[\\d.]+/g).map(Number), s = c.startsWith('color(') ? 255 : 1;
    return [n[0] * s, n[1] * s, n[2] * s, n.length > 3 ? n[3] : 1]; };
  const chain = [];
  for (let n = e; n; n = n.parentElement) chain.unshift(n);
  let bg = [255, 255, 255], op = 1;
  for (const n of chain) {
    const [r, g, b, a] = rgba(getComputedStyle(n).backgroundColor);
    bg = [r, g, b].map((v, i) => v * a + bg[i] * (1 - a));
    op *= Number(getComputedStyle(n).opacity);
  }
  const [r, g, b, a] = rgba(getComputedStyle(e).color), k = a * op;
  const fg = [r, g, b].map((v, i) => v * k + bg[i] * (1 - k));
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const lum = (c) => c.map(lin)
    .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}"""
# Fake pages: never dialled, every request on it is answered by a route. Not port 9: Firefox refuses it.
ORIGIN = "http://127.0.0.1:8"
POP_ASK_MS = 120  # the pages' completion debounce (POP_ASK_MS in ui.js)
BARRIER = {"barrier": 1}
DONE = {"done": 1}  # the fake's sentinel frame: the page handled every frame sent before it
# Fake-board data shared by the composer files.
T = "19:fake@thread.v2"
ANN = {"name": "Ann Lee", "mri": "8:orgid:ann"}
ANN2 = {"name": "Ann Lee", "mri": "8:orgid:ann2"}  # another person, same display name
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


@dataclass
class Pages:
    browser: Browser
    host: str = ""
    cookie: dict[str, Any] | None = None  # the served board's `mt` login, sent by every page opened
    external: list[str] = field(default_factory=list)  # requests that tried to leave 127.0.0.1
    errors: list[str] = field(default_factory=list)
    contexts: list[BrowserContext] = field(default_factory=list)

    async def context(self, **ctx: Any) -> BrowserContext:
        context = await self.browser.new_context(**ctx)
        self.contexts.append(context)
        return context

    def serve(self, host: str, board: Any) -> None:
        """Pages open on the board served at `host`, logged in the way its launch URL logs a browser in."""
        self.host = host
        self.cookie = auth_cookie(board)

    async def open(self, path: str, *, csp: bool = False, **ctx: Any) -> Page:
        """A page on `path` of the served board. wait_for_function evaluates, which the chat CSP
        forbids: contexts bypass it unless `csp` asks for the real policy."""
        context = await self.context(bypass_csp=not csp, **ctx)
        if self.cookie:
            await context.add_cookies([self.cookie])  # type: ignore[list-item]
        await context.add_init_script(FRAMES)
        await context.route(re.compile(r"^(?!http://127\.0\.0\.1:)"), self._block)
        page = await context.new_page()
        self.watch(page)
        await page.goto(f"http://{self.host}{path}")
        return page

    def watch(self, page: Page) -> None:
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))

    async def close(self) -> None:
        """Close every page before its server stops: the server waits close_timeout (10 s) on a
        socket whose page navigated away."""
        for context in self.contexts:
            await context.close()
        self.contexts.clear()

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
async def pages(request: pytest.FixtureRequest) -> AsyncIterator[Pages]:
    """Chrome; `@pytest.mark.parametrize("pages", ["firefox"], indirect=True)` adds playwright's Firefox."""
    engine = getattr(request, "param", "chromium")
    with _uncapped():
        driver = async_playwright()
        p = await driver.__aenter__()
    browser, found = None, None
    try:
        if engine == "firefox":
            try:
                browser = await p.firefox.launch(headless=True)
            except Error:
                if os.environ.get("CI", "").lower() in ("1", "true"):
                    raise
                pytest.skip("run `uv run --with playwright playwright install firefox`")
        else:
            browser = await p.chromium.launch(executable_path=str(CHROME), headless=True)
        found = Pages(browser)
        yield found
    finally:
        if browser is not None:
            await browser.close()
        await driver.__aexit__(None, None, None)
    assert found is None or found.errors == []


def row(
    thread_id: str, label: str, last_activity: str = "2026-10-09T10:00:00.000Z", **over: Any
) -> dict[str, Any]:
    """A board row as the server broadcasts it."""
    return {
        "id": thread_id,
        "label": label,
        "last_activity": last_activity,
        "unread": False,
        "mention": None,
        "sender": "Ann Lee",
        "text": f"snippet {label}",
        "seen_at": None,
        "muted": False,
        "typing": [],
        **over,
    }


async def until(page: Page, js: str, tries: int = 500) -> None:
    """wait_for_function evaluates a string the chat page's CSP refuses: poll through evaluate instead."""
    for _ in range(tries):
        if await page.evaluate(js):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{js} not true in {tries / 100:g} s")


# The send queue as shown (spec 017): each item's text and label, in order.
QUEUE = (
    "JSON.stringify([...document.querySelectorAll('.q')]"
    ".map((n) => [n.querySelector('.qtext').textContent, n.querySelector('.qstate').textContent]))"
)


async def queue_is(page: Page, *items: tuple[str, str]) -> None:
    """The page's send queue shows `items`, (text, label) each, in order."""
    await until(page, f"{QUEUE} === JSON.stringify({json.dumps([list(i) for i in items])})")


async def act(page: Page, name: str) -> None:
    """Clicks the failed item's `name` button (retry, edit, discard)."""
    await page.locator(f".q.failed button[title={name}]").click()


async def past(page: Page, ms: int) -> None:
    """Resolve after every page timer due within `ms`: Chrome fires timers in deadline order, so a
    debounce armed before this call has run, however late the host runs it."""
    await page.evaluate("ms => new Promise((r) => setTimeout(r, ms))", ms)


async def rendered(page: Page) -> None:
    """Resolve once the page rendered twice: a scroll event from an earlier scroll has been handled."""
    await page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))")


async def shows(page: Page, expected: list[str]) -> None:
    """The open list reads `expected`: an answer to an earlier prefix may show first."""
    seen: list[str] = []
    for _ in range(500):
        if not await page.locator("#pop").is_hidden():
            seen = await page.locator("#pop [role=option]").all_text_contents()
            if seen == expected:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"list shows {seen}, expected {expected}")


async def closed(page: Page) -> bool:
    return await page.locator("#pop").is_hidden()


@dataclass
class Fake:
    """The board side of a page's socket. Subclasses answer in `on_verb`; `hold` queues `answer()`s."""

    page: Page | None = None
    verbs: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    hold: bool = False
    ws: Any = None
    frames: list[bytes] = field(default_factory=list)  # binary messages, in order
    # `send` message ids, kept out of `verbs` so the composer files compare verbs without them
    cmids: list[str] = field(default_factory=list)

    def attach(self, ws: Any) -> None:
        self.ws = ws
        ws.on_message(self._on_message)

    def _on_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            self.frames.append(raw)
            return
        msg = json.loads(raw)
        if "send" in msg and "cmid" in msg:
            self.cmids.append(msg.pop("cmid"))
        self.verbs.append(msg)
        if msg != BARRIER:
            self.on_verb(msg)

    def on_verb(self, msg: dict[str, Any]) -> None:
        pass

    def send(self, frame: dict[str, Any]) -> None:
        self.ws.send(json.dumps(frame))

    def answer(self, frame: dict[str, Any]) -> None:
        if self.hold:
            self.held.append(frame)
        else:
            self.send(frame)

    def release(self, i: int) -> None:
        self.send(self.held[i])

    def of(self, verb: str) -> list[dict[str, Any]]:
        return [v for v in self.verbs if verb in v]

    async def settled(self, past_ms: int = 0) -> None:
        """Every verb the page sent so far reached us (after its timers due within `past_ms`): the
        socket is ordered, a barrier closes it."""
        assert self.page is not None
        if past_ms:
            await past(self.page, past_ms)
        await self.page.evaluate(f"window.__ws.send({json.dumps(json.dumps(BARRIER))})")
        await wait(lambda: BARRIER in self.verbs, "barrier")
        self.verbs.remove(BARRIER)

    async def handled(self) -> None:
        """The page handled every frame sent to it so far: a sentinel frame comes back in order."""
        assert self.page is not None
        n = await self.page.evaluate("window.__frames.length")
        self.send(DONE)
        await until(self.page, f"window.__frames.slice({n}).some(f => f.done === 1)")


async def open_fake(
    pages: Pages,
    fake: Fake,
    url: str,
    html: str,
    *,
    page_route: str | re.Pattern[str],
    ws_route: str | re.Pattern[str],
    headers: dict[str, str] | None = None,
    init: str = "",
    **ctx: Any,
) -> Page:
    """`html` served on `page_route` of ORIGIN with `fake` behind the socket, then `url` opened;
    `init` runs in the page before its own script."""
    context = await pages.context(**ctx)
    await context.route_web_socket(ws_route, fake.attach)
    # After the socket route: its own WebSocket stand-in must be the class FRAMES wraps.
    await context.add_init_script(FRAMES)
    if init:
        await context.add_init_script(init)

    async def other(route: Route) -> None:
        if not route.request.url.startswith(ORIGIN + "/"):
            pages.external.append(route.request.url)
        await route.abort()

    async def emoji(route: Route) -> None:
        await route.fulfill(body=PNG, content_type="image/png")

    async def page_html(route: Route) -> None:
        await route.fulfill(body=html, headers=headers or {"Content-Type": "text/html; charset=utf-8"})

    await context.route("**/*", other)  # last registered wins: the catch-all goes first
    await context.route(ORIGIN + "/emoji/*", emoji)
    await context.route(page_route, page_html)
    page = fake.page = await context.new_page()
    pages.watch(page)
    await page.goto(url)
    return page


async def pick_namesakes(fake: Fake, picks: list[int]) -> Any:
    """`@ann` picks the n-th of two namesakes for each n in `picks`, the text cleared before every pick
    but the first; Enter sends. The mentions sent."""
    page = fake.page
    assert page is not None
    box = page.locator("#reply")
    for i, n in enumerate(picks):
        if i:
            await box.press("Control+a")
            await box.press("Backspace")
        await box.press_sequentially("@ann")
        await shows(page, ["Ann Lee", "Ann Lee"])
        for _ in range(n):
            await box.press("ArrowDown")
        await box.press("Enter")
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    return fake.of("send")[0]["mentions"]


async def pick_out_of_order(fake: Fake) -> Any:
    """`@bob` picked, then `hi @ann` picked before it in the text; Enter sends. The mentions sent."""
    page = fake.page
    assert page is not None
    box = page.locator("#reply")
    await box.press_sequentially("@bob")
    await shows(page, ["Bob Roe"])
    await box.press("Enter")
    await box.press("Home")
    await box.press_sequentially("hi @ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    assert await box.input_value() == "hi @Ann Lee @Bob Roe "
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    return fake.of("send")[0]["mentions"]


async def pick_then_glue(fake: Fake) -> Any:
    """`@ann` picked, its trailing space turned into `ds`: `@Ann Leeds`; Enter sends. The mentions sent."""
    page = fake.page
    assert page is not None
    box = page.locator("#reply")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    await box.press("ArrowLeft")
    await box.press("Delete")
    await box.press_sequentially("ds")
    assert await box.input_value() == "@Ann Leeds"
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    return fake.of("send")[0]["mentions"]
