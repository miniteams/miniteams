"""Widget navigation mode between chats (spec 011 phase 6). Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_nav.py`.
"""

import re
from typing import Any

import browser_helpers
import pytest
import test_browser
from browser_helpers import Page, Route, wait  # its import skips without playwright
from test_browser import Widget, _chat, _teams_tab
from test_web_chat import T

pytestmark = pytest.mark.browser
pages, widget = browser_helpers.pages, test_browser.widget

_DISPATCH = (
    "document.querySelector('a.name').dispatchEvent("
    "new MouseEvent('click', {button: %d, bubbles: true, cancelable: true}))"
)

_SUFFIX = {"list": "", "split": "?chatnav=split"}


async def _stub_chat(route: Route) -> None:
    # The real chat page would follow T on a board that has no archive for it.
    await route.fulfill(body="<title>chat</title>", content_type="text/html")


async def _left(widget: Widget) -> None:
    """The page that navigated away closed its socket: the server handled every verb it sent first."""
    await wait(lambda: not widget.board.clients, "the old page's socket closed")


async def _load(widget: Widget, browser: bool, path: str, **stored: str) -> Page:
    page = await widget.load(browser, path)
    for k, v in stored.items():
        await page.evaluate("([k, v]) => localStorage.setItem(k, v)", [k, v])
    return await widget.load(browser, path)


@pytest.mark.parametrize("chatnav", ["list", "split"])
async def test_nav_plain_click_opens_the_chat_page_in_the_same_tab(widget: Widget, chatnav: str) -> None:
    """011-AC20: on `/?nav=chat` a plain click navigates this tab to the encoded chat URL, no verb,
    no tab from the link's own target. 011-AC21: the URL carries `chatnav=split` when set."""
    page = await _load(widget, True, "/?nav=chat", chatnav=chatnav)
    await widget.ctx.route(re.compile(r"/chat/"), _stub_chat)
    widget.verbs.clear()
    await page.locator("a.name").click()
    await page.wait_for_url(widget.base + _chat(T) + _SUFFIX[chatnav])
    assert "%3A" in page.url and "%40" in page.url
    await _left(widget)
    assert widget.verbs == []
    assert len(widget.ctx.pages) == 1


async def test_nav_enter_on_the_filtered_selection_opens_the_chat_page(widget: Widget) -> None:
    """011-AC20: in nav mode, Enter on the row picked from the filter box navigates like a plain click."""
    page = await _load(widget, True, "/?nav=chat")
    await widget.ctx.route(re.compile(r"/chat/"), _stub_chat)
    widget.verbs.clear()
    await page.locator("#q").fill("fict")
    await page.locator("#q").press("ArrowDown")
    await page.locator("#q").press("Enter")
    await page.wait_for_url(widget.base + _chat(T), timeout=5000)
    await _left(widget)
    assert widget.verbs == []
    assert len(widget.ctx.pages) == 1


@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_nav_middle_click_opens_the_chat_page_in_a_new_tab(widget: Widget, browser: bool) -> None:
    """011-AC20: middle click in nav mode opens the chat page (through --browser when set) whatever
    the middle-click setting; the link's default never fires too."""
    page = await _load(widget, browser, "/?nav=chat", midclick="teams")
    await widget.ctx.route(re.compile(r"/chat/"), _stub_chat)
    if browser:
        assert await widget.act(lambda: page.locator("a.name").click(button="middle")) == ([(T, "chat")], [])
    else:
        assert await widget.opens_tab(widget.base + _chat(T), button="middle") == []
    dispatched: list[bool] = []

    async def click() -> None:
        dispatched.append(await page.evaluate(_DISPATCH % 1))

    assert await widget.act(click) == ([], [])
    assert dispatched == [False]
    assert page.url == widget.base + "/?nav=chat"


@pytest.mark.parametrize("chatnav", ["list", "split"])
@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_middle_click_carries_the_split_setting(
    widget: Widget, monkeypatch: pytest.MonkeyPatch, browser: bool, chatnav: str
) -> None:
    """011-AC21: the chat page a middle click opens gets the widget's `chatnav` when split: in the
    verb for --browser (which has none of our storage), in the tab URL otherwise."""

    async def spy(
        thread_id: str,
        target: str = "app",
        ws: Any = None,
        msg_id: str = "",
    ) -> bool:
        widget.verbs.append((thread_id, "split" if target == "split" else target in ("chat", "web")))
        return True

    page = await _load(widget, browser, "/?nav=chat", chatnav=chatnav)
    monkeypatch.setattr(widget.board, "open_row", spy)
    await widget.ctx.route(re.compile(r"/chat/"), _stub_chat)
    if browser:
        sent = await widget.act(lambda: page.locator("a.name").click(button="middle"))
        assert sent == ([(T, "split" if chatnav == "split" else True)], [])
    else:
        assert await widget.opens_tab(widget.base + _chat(T) + _SUFFIX[chatnav], button="middle") == []


@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
async def test_nav_ctrl_click_is_unchanged(widget: Widget, browser: bool) -> None:
    """011-AC20: Ctrl click in nav mode still opens Teams."""
    page = await _load(widget, browser, "/?nav=chat")
    if browser:
        ctrl = await widget.act(lambda: page.locator("a.name").click(modifiers=["Control"]))
        assert ctrl == ([(T, True)], [])
    else:
        assert await widget.opens_tab(_teams_tab(widget), modifiers=["Control"]) == []
    assert page.url == widget.base + "/?nav=chat"


@pytest.mark.parametrize("chatnav", ["list", "split"])
async def test_board_without_nav_is_unchanged(widget: Widget, chatnav: str) -> None:
    """011-AC20: `/` without `nav` behaves as before whatever `chatnav` is stored."""
    page = await _load(widget, True, "/", chatnav=chatnav)
    assert await widget.act(lambda: page.locator("a.name").click()) == ([(T, False)], [])
    assert await widget.act(lambda: page.locator("a.name").click(button="middle")) == ([(T, True)], [])
    assert await widget.act(lambda: page.locator("a.name").click(modifiers=["Control"])) == ([(T, True)], [])
    assert page.url == widget.base + "/"


async def test_chatnav_setting_is_stored(widget: Widget) -> None:
    """011-AC20: the ⚙ "Chat navigation" segment defaults to List and persists the choice."""
    page = await widget.load(True)
    await page.click("#gear")
    assert await page.get_by_role("button", name="List", exact=True).get_attribute("class") == "on"
    await page.get_by_role("button", name="Split", exact=True).click()
    assert await page.evaluate("localStorage.getItem('chatnav')") == "split"
    await widget.load(True)
    await page.click("#gear")
    assert await page.get_by_role("button", name="Split", exact=True).get_attribute("class") == "on"


async def test_chatnav_url_param_overrides_the_stored_choice(widget: Widget) -> None:
    """011-AC20: `?chatnav=split` wins over a stored `list`, without replacing it."""
    page = await _load(widget, True, "/?chatnav=split", chatnav="list")
    await page.click("#gear")
    assert await page.get_by_role("button", name="Split", exact=True).get_attribute("class") == "on"
    assert await page.evaluate("localStorage.getItem('chatnav')") == "list"
