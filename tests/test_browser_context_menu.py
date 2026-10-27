"""A message's context menu (spec 034) on the chat page, against the composer files' fake chat socket.
Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_context_menu.py`.
"""

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import browser_helpers
import pytest
import test_browser_split_ux as ux
from browser_helpers import ORIGIN, Page, Pages, T, open_fake, rendered, shows, until, wait
from test_browser_chat_compose import MINE, PAGE, Fake

from miniteams import web as W

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)

ANN_MSG = {"from": "8:orgid:ann", "name": "Ann Lee", "me": False, "type": "text", "editable": True}
LIKED = [{"key": "like", "emoji": "👍", "count": 1, "mine": True, "people": ["Me"]}]
# Older lines first, so the list scrolls; then mine (1), Ann's reply with a link (2), a deleted one (3),
# Ann's image (4).
HISTORY = [
    *(
        {**ANN_MSG, "id": str(500 + i), "at": f"2026-10-09T09:{i:02d}:00.000Z", "html": f"<p>older {i}</p>"}
        for i in range(30)
    ),
    {**MINE, "html": "<p>mine</p>", "editable": True, "type": "text"},
    {
        **ANN_MSG,
        "id": "2",
        "at": "2026-10-09T10:01:00.000Z",
        "html": '<blockquote class="reply" data-msg="1"><strong>Me</strong><p>mine</p></blockquote>'
        '<p>hello <a href="https://example.com/x" target="_blank" rel="noopener">link</a></p>'
        "<p>second line</p>",
        "reactions": LIKED,
    },
    {**ANN_MSG, "id": "3", "at": "2026-10-09T10:02:00.000Z", "html": "", "deleted": True},
    {
        **ANN_MSG,
        "id": "4",
        "at": "2026-10-09T10:03:00.000Z",
        "html": '<p><img src="/emoji/0-pic" alt="pic"></p>',
    },
]
MENU = ".ctxmenu"
ACTS = f"[...document.querySelectorAll('{MENU} > [data-act]')].map(n => n.dataset.act)"
# The bar's controls as it shows them (a read-only chat hides some by CSS).
BAR = """id => [...document.querySelectorAll(`[data-id="${id}"] > .actions > [data-act]`)]
  .filter(c => getComputedStyle(c).display !== 'none').map(c => c.dataset.act)"""
FOCUSED = "document.activeElement"
# Whether the last contextmenu event reached the document with its default prevented.
WATCH = "document.addEventListener('contextmenu', e => { window.__prevented = e.defaultPrevented; })"


async def _open(pages: Pages, fake: Fake, init: str = "", **ctx: Any) -> Page:
    headers = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": W._page_csp(PAGE)}
    page = await open_fake(
        pages,
        fake,
        f"{ORIGIN}/chat/{quote(T, safe='')}",
        PAGE,
        page_route=re.compile(r"^http://127\.0\.0\.1:8/chat/[^/]+$"),
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws\?chat$"),
        headers=headers,
        init=WATCH + ";" + init,
        **ctx,
    )
    await page.wait_for_selector('[data-id="4"]')
    await until(
        page, "document.body.classList.contains('readonly') || !!document.querySelector('#status:not(.off)')"
    )
    # A visible size, and loaded: the list's own scroll to the bottom as it grows would close a menu.
    await page.locator(f"{_msg('4')} img").evaluate("i => { i.style.width = i.style.height = '40px'; }")
    await until(page, "document.querySelector('[data-id=\"4\"] img').complete")
    await rendered(page)
    return page


@pytest.fixture
async def chat(pages: Pages) -> AsyncIterator[tuple[Page, Fake]]:
    fake = Fake(history=HISTORY, opener=True)
    yield await _open(pages, fake), fake
    await pages.close()
    assert pages.external == []


def _msg(mid: str) -> str:
    return f'#msgs > [data-id="{mid}"]'


async def _right_click(page: Page, selector: str) -> dict[str, float]:
    """Right-clicks the middle of `selector`, in view first (its scroll settled: a scroll closes a menu); the
    point clicked."""
    await page.locator(selector).scroll_into_view_if_needed()
    await rendered(page)
    box = await page.locator(selector).bounding_box()
    assert box
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.click(x, y, button="right")
    return {"x": x, "y": y}


async def _menu(page: Page, mid: str, part: str = ".html p >> nth=-1") -> dict[str, float]:
    at = await _right_click(page, f"{_msg(mid)} {part}")
    await page.wait_for_selector(MENU)
    return at


async def _item(page: Page, act: str) -> None:
    await page.locator(f"{MENU} [data-act={act}]").click()
    assert await page.locator(MENU).count() == 0


@BOTH
async def test_a_right_click_opens_the_menu_at_the_pointer(chat: tuple[Page, Fake]) -> None:
    """034-AC1: right click on a message's body: the favorites, `…`, the bar's actions plus Copy text, at the
    pointer, the first item focused; the browser's menu is prevented."""
    page, _ = chat
    at = await _menu(page, "2")
    assert await page.evaluate("window.__prevented") is True
    box = await page.locator(MENU).bounding_box()
    assert box and abs(box["x"] - at["x"]) < 1 and abs(box["y"] - at["y"]) < 1
    assert await page.evaluate(ACTS) == ["react", "reply", "text", "copy", "teams"]
    favs = page.locator(f"{MENU} .favs button")
    assert await favs.evaluate_all("ns => ns.map(n => n.dataset.fav || n.textContent)") == [
        "👍", "❤️", "😆", "😮", "😢", "😡", "🙏", "🙃", "…"
    ]  # fmt: skip
    assert await favs.first.get_attribute("class") == "mine"  # my 👍 is marked
    assert await favs.first.get_attribute("aria-label") == "remove 👍"
    assert await page.evaluate(f"{FOCUSED} === document.querySelector('{MENU} button')")
    assert await page.locator(MENU).get_attribute("role") == "menu"


@pytest.mark.parametrize(
    ("mid", "read_only", "expected"),
    [
        ("2", False, ["react", "reply", "text", "copy", "teams"]),  # someone else's text message
        ("1", False, ["react", "reply", "edit", "delete", "text", "copy", "teams"]),  # mine
        ("3", False, ["copy", "teams"]),  # deleted
        ("4", False, ["react", "reply", "copy", "teams"]),  # an image alone: no text to copy
        ("2", True, ["text", "copy", "teams"]),  # read-only chat
        ("1", True, ["text", "copy", "teams"]),
    ],
)
async def test_the_menu_offers_what_the_bar_shows(
    pages: Pages, mid: str, read_only: bool, expected: list[str]
) -> None:
    """034-AC7 + 034-AC8: the menu's actions are the bar's, under its conditions, plus Copy text on a message
    not deleted; read-only: Open in Teams, Copy link, Copy text."""
    fake = Fake(history=HISTORY, read_only=read_only)
    page = await _open(pages, fake)
    try:
        await _menu(page, mid, ".html")
        assert await page.evaluate(ACTS) == expected
        assert [a for a in expected if a != "text"] == sorted(
            await page.evaluate(f"({BAR})({mid!r})"), key=expected.index
        )
    finally:
        await pages.close()


@BOTH
async def test_a_favorite_reacts_as_the_bar_picker_does(chat: tuple[Page, Fake]) -> None:
    """034-AC2: a favorite sends what the bar's picker sends for it, mine toggling off; the menu closes."""
    page, fake = chat
    for fav in ("👍", "❤️"):
        await _menu(page, "2")
        await page.locator(f'{MENU} [data-fav="{fav}"]').click()
        assert await page.locator(MENU).count() == 0
        await page.locator(_msg("2")).hover()
        await page.locator(f"{_msg('2')} [data-act=react]").click()
        await page.locator(f'{_msg("2")} .picker [data-fav="{fav}"]').click()
    await wait(lambda: len(fake.of("react")) == 4)
    menu, bar, menu2, bar2 = fake.of("react")
    assert menu == bar == {"react": T, "msg": "2", "key": "like", "remove": True}
    assert menu2 == bar2 == {"react": T, "msg": "2", "key": "2764-fe0f", "remove": False}


@BOTH
async def test_any_emoji_opens_the_search_and_reacts_with_the_pick(chat: tuple[Page, Fake]) -> None:
    """034-AC3: `…` opens the emoji search; the emoji picked is the reaction sent."""
    page, fake = chat
    await _menu(page, "2")
    await page.locator(f"{MENU} .favs button", has_text="…").click()
    assert await page.locator(MENU).count() == 0
    search = page.locator(f"{_msg('2')} .esearch input")
    await search.press_sequentially("fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"], pop="#epop")
    await search.press("Enter")
    await wait(lambda: len(fake.of("react")) == 1)
    assert fake.of("react") == [{"react": T, "msg": "2", "key": "1f525", "remove": False}]


@BOTH
async def test_reply_edit_delete_and_teams_run_the_bar_actions(chat: tuple[Page, Fake]) -> None:
    """034-AC4: Reply quotes the message in the composer, Edit opens its editor, Delete asks then deletes,
    Open in Teams opens its link; each closes the menu."""
    page, fake = chat
    await _menu(page, "2")
    await _item(page, "reply")
    assert await page.locator('#quotes .quote[data-quote="2"]').count() == 1
    assert await page.evaluate(f"{FOCUSED}.id") == "reply"
    await _menu(page, "2")
    await _item(page, "teams")
    await wait(lambda: fake.of("open") == [{"open": T, "msg": "2"}])
    await _menu(page, "1")
    await _item(page, "edit")
    await page.wait_for_selector(f"{_msg('1')} .editor:not([readonly])")
    await page.keyboard.press("Escape")
    await page.wait_for_selector(f"{_msg('1')} .html")
    asked: list[str] = []

    async def accept(dialog: Any) -> None:
        asked.append(dialog.message)
        await dialog.accept()

    page.once("dialog", accept)
    await _menu(page, "1")
    await _item(page, "delete")
    await wait(lambda: fake.of("delete") == [{"delete": T, "msg": "1"}])
    assert asked == ["Delete this message?"]


async def test_copy_link_and_copy_text_fill_the_clipboard(pages: Pages) -> None:
    """034-AC4 + 034-AC5: Copy link copies the bar's 🔗 link; Copy text the message's plain text without its
    reply quote."""
    fake = Fake(history=HISTORY)
    page = await _open(pages, fake, permissions=["clipboard-read", "clipboard-write"])
    try:
        await _menu(page, "2")
        await _item(page, "copy")
        await until(page, f"navigator.clipboard.readText().then(t => t === {W.deep_link(T, '2')!r})")
        await until(page, "document.getElementById('toast').textContent === 'Copied'")
        assert await page.evaluate("getComputedStyle(document.getElementById('toast')).opacity") == "1"
        clear = "(t, l) => t.getBoundingClientRect().bottom <= l.getBoundingClientRect().top"
        assert await page.evaluate(
            f"({clear})(document.getElementById('toast'), (l => (l.hidden = false, l))("
            "document.getElementById('lag')))"
        )  # above the lag note, not on it
        await page.evaluate("document.getElementById('lag').hidden = true")
        await page.wait_for_timeout(1000)
        await _menu(page, "2")
        await _item(page, "copy")
        # 2 s after the first copy, 1 s after the second: the second's timer holds.
        await page.wait_for_timeout(1000)
        assert await page.evaluate("document.getElementById('toast').textContent") == "Copied"
        await until(page, "document.getElementById('toast').textContent === ''")  # gone: `:empty` hides it
        style = "(s => [s.display, s.opacity])(getComputedStyle(document.getElementById('toast')))"
        assert await page.evaluate(style) == ["block", "0"]  # still in the a11y tree, unseen
        await _menu(page, "2")
        await _item(page, "text")
        await until(page, "navigator.clipboard.readText().then(t => t.includes('second'))")
        text = await page.evaluate("navigator.clipboard.readText()")
        assert text.split() == ["hello", "link", "second", "line"]  # the quoted "mine" left out
        assert await page.locator("#toast").inner_text() == "Copied"
    finally:
        await pages.close()


@BOTH
@pytest.mark.parametrize("target", ["link", "image", "selection"])
async def test_links_images_and_selections_keep_the_browser_menu(
    chat: tuple[Page, Fake], target: str
) -> None:
    """034-AC6: a right click on a link, an image, or text selected in the message is the browser's."""
    page, _ = chat
    if target == "link":
        await _right_click(page, f"{_msg('2')} .html a")
    elif target == "image":
        await _right_click(page, f"{_msg('4')} .html img")
    else:
        await page.locator(f"{_msg('2')} .html p >> nth=-1").evaluate(
            "p => { const r = document.createRange(); r.selectNodeContents(p);"
            " getSelection().removeAllRanges(); getSelection().addRange(r); }"
        )
        await _right_click(page, f"{_msg('2')} .html p >> nth=-1")
        assert await page.evaluate("getSelection().toString()") == "second line"
    await until(page, "window.__prevented !== undefined")
    assert await page.evaluate("window.__prevented") is False
    assert await page.locator(MENU).count() == 0


@BOTH
async def test_one_menu_and_every_way_out_closes_it(chat: tuple[Page, Fake]) -> None:
    """034-AC9: Escape, a click outside, a list scroll, another message's menu, and the message drawn again
    each close the menu; never two menus."""
    page, fake = chat
    await _menu(page, "2")
    await page.keyboard.press("Escape")
    assert await page.locator(MENU).count() == 0
    assert await page.evaluate(f"{FOCUSED}.dataset.id") == "2"
    await _menu(page, "2")
    await page.locator("#label").click()
    assert await page.locator(MENU).count() == 0
    await _menu(page, "2", ".head")  # its menu hangs left, over 3 and 4: 4 is right-clicked in its middle
    four = await page.locator(_msg("4")).bounding_box()
    assert four
    x, y = four["x"] + four["width"] / 2, four["y"] + four["height"] / 2
    await page.mouse.click(x, y, button="right")
    await until(page, f"document.querySelectorAll('{MENU}').length === 1 && {ACTS}.length === 4")
    menu = await page.locator(MENU).bounding_box()
    assert menu and abs(menu["x"] - x) < 1  # 4's own menu, at the pointer
    assert await page.evaluate(ACTS) == ["react", "reply", "copy", "teams"]
    msgs = await page.locator("#list").bounding_box()
    assert msgs
    await page.mouse.move(msgs["x"] + 20, msgs["y"] + 20)  # over the list, not the menu
    await page.mouse.wheel(0, -200)
    await until(page, f"!document.querySelector('{MENU}')")
    await _menu(page, "2")
    # 2 drawn again, its height unchanged: no scroll closes the menu for it.
    fake.send({"edit": {**HISTORY[-3], "reactions": [{**LIKED[0], "count": 2}]}, "thread": T})
    await until(page, f"!document.querySelector('{MENU}')")
    assert await page.evaluate(f"{FOCUSED}.dataset.id") == "2"  # its new node
    assert await page.locator(f"{_msg('2')} .reacts").inner_text() == "👍 2"


async def test_the_menu_key_and_shift_f10_open_it_from_the_keyboard(chat: tuple[Page, Fake]) -> None:
    """034-AC10: on a focused message, Menu or Shift+F10 opens the menu under its top left, first item
    focused; ↑/↓/Home/End move, Enter runs and refocuses the message, Escape closes and refocuses it, Tab
    leaves.
    (Chromium only: headless Firefox fires no contextmenu for these keys.)"""
    page, fake = chat
    await page.locator(_msg("2")).focus()
    await page.keyboard.press("Shift+F10")
    await page.wait_for_selector(MENU)
    menu, msg = await page.locator(MENU).bounding_box(), await page.locator(_msg("2")).bounding_box()
    assert menu and msg and abs(menu["x"] - msg["x"]) < 1 and abs(menu["y"] - msg["y"]) < 1
    label = f"{FOCUSED}.dataset.fav || {FOCUSED}.dataset.act || {FOCUSED}.textContent"
    assert await page.evaluate(label) == "👍"
    for key, expected in (
        ("ArrowDown", "❤️"), ("End", "teams"), ("ArrowDown", "👍"), ("ArrowUp", "teams"), ("ArrowUp", "copy"),
        ("Home", "👍"), ("ArrowUp", "teams"),
    ):  # fmt: skip
        await page.keyboard.press(key)
        assert await page.evaluate(label) == expected, key
    await page.keyboard.press("Escape")
    assert await page.locator(MENU).count() == 0
    assert await page.evaluate(f"{FOCUSED}.dataset.id") == "2"
    await page.keyboard.press("ContextMenu")
    await page.wait_for_selector(MENU)
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")  # ❤️
    assert await page.locator(MENU).count() == 0
    assert await page.evaluate(f"{FOCUSED}.dataset.id") == "2"
    await wait(lambda: fake.of("react") == [{"react": T, "msg": "2", "key": "2764-fe0f", "remove": False}])
    await page.keyboard.press("Shift+F10")
    await page.wait_for_selector(MENU)
    await page.keyboard.press("Tab")
    assert await page.locator(MENU).count() == 0
    assert await page.evaluate(f"!document.activeElement.closest('{MENU}')")


@pytest.mark.parametrize("pages", ["firefox"], indirect=True)
async def test_menu_keys_move_and_close_in_firefox(chat: tuple[Page, Fake]) -> None:
    """034-AC10: the menu's own keys in Firefox, opened by a right click; the thread's keys stay out of it."""
    page, _ = chat
    await _menu(page, "2")
    label = f"{FOCUSED}.dataset.fav || {FOCUSED}.dataset.act"
    for key, expected in (("ArrowDown", "❤️"), ("End", "teams"), ("Home", "👍"), ("ArrowUp", "teams")):
        await page.keyboard.press(key)
        assert await page.evaluate(label) == expected, key
    await page.keyboard.press("Escape")
    assert await page.locator(MENU).count() == 0
    assert await page.evaluate(f"{FOCUSED}.dataset.id") == "2"


@BOTH
async def test_a_menu_by_the_bottom_right_edge_stays_in_view(pages: Pages) -> None:
    """034-AC11: opened at the viewport's bottom right corner, the menu is moved to fit inside it."""
    fake = Fake(history=HISTORY)
    page = await _open(pages, fake, viewport={"width": 520, "height": 420})
    try:
        box = await page.locator(_msg("4")).bounding_box()
        assert box
        x, y = box["x"] + box["width"] - 3, box["y"] + box["height"] - 3
        await page.mouse.click(x, y, button="right")
        await page.wait_for_selector(MENU)
        menu = await page.locator(MENU).bounding_box()
        assert menu and menu["x"] < x and menu["y"] < y  # moved, not cut
        assert menu["x"] >= 0 and menu["y"] >= 0
        assert menu["x"] + menu["width"] <= 520 and menu["y"] + menu["height"] <= 420
    finally:
        await pages.close()


@BOTH
async def test_a_refused_clipboard_says_so(pages: Pages) -> None:
    """034-AC4 + 034-AC5: Copy text and Copy link from the menu say when the clipboard refuses."""
    refuse = "Clipboard.prototype.writeText = () => Promise.reject(new DOMException('x'))"
    page = await _open(pages, Fake(history=HISTORY), init=refuse)
    try:
        for act, said in (
            ("text", "can't copy here: select the text, Ctrl+C"),
            ("copy", "can't copy here: right-click ↗ Teams, Copy link address"),
        ):
            await page.evaluate("document.getElementById('err').textContent = ''")
            await _menu(page, "2")
            await _item(page, act)
            await until(page, "document.getElementById('err').textContent !== ''")
            assert await page.locator("#err").inner_text() == said, act
            assert await page.locator("#toast").text_content() == ""
    finally:
        await pages.close()


# The menu's top below its message's.
def _gap(mid: str = "2") -> str:
    return (
        "document.querySelector('.ctxmenu').getBoundingClientRect().top"
        f" - document.querySelector('#msgs > [data-id=\"{mid}\"]').getBoundingClientRect().top"
    )


GAP = _gap()


@BOTH
async def test_the_list_following_new_messages_keeps_the_menu_beside_its_message(
    chat: tuple[Page, Fake],
) -> None:
    """034-AC9: new messages at the bottom scroll the list, the menu moves with its message and keeps the
    focus; it closes once its message is out of view, and on the reader's own scroll."""
    page, fake = chat
    at = await _menu(page, "2")
    gap = await page.evaluate(GAP)
    assert (
        abs(await page.evaluate("document.querySelector('.ctxmenu').getBoundingClientRect().top") - at["y"])
        < 1
    )
    for i in range(5, 8):
        top = await page.evaluate("document.getElementById('list').scrollTop")
        live = {**ANN_MSG, "id": str(i), "at": f"2026-10-09T10:0{i}:00.000Z", "html": "<p>live</p>" * 2}
        fake.send({"msg": live, "thread": T})
        await page.wait_for_selector(_msg(str(i)))
        await until(page, f"document.getElementById('list').scrollTop > {top}")  # the list followed
        await until(page, f"Math.abs(({GAP}) - {gap}) < 1")
        assert await page.locator(MENU).count() == 1
        assert await page.evaluate(f"!!{FOCUSED}.closest('{MENU}')")
    fake.send({"msg": {**live, "id": "8", "html": "<p>long</p>" * 40}, "thread": T})
    await page.wait_for_selector(_msg("8"))
    await until(page, f"!document.querySelector('{MENU}')")  # 2 left the view
    await _menu(page, "8")
    msgs = await page.locator("#list").bounding_box()
    assert msgs
    await page.mouse.move(msgs["x"] + 20, msgs["y"] + 20)  # over the list, not the menu
    await page.mouse.wheel(0, -200)
    await until(page, f"!document.querySelector('{MENU}')")


async def _menu_left(page: Page, mid: str) -> None:
    """Right-clicks `mid`'s body near its left edge: its menu leaves the bars, on the right, clear."""
    await page.locator(_msg(mid)).scroll_into_view_if_needed()
    await rendered(page)
    box = await page.locator(f"{_msg(mid)} .html").bounding_box()
    assert box
    await page.mouse.click(box["x"] + 20, box["y"] + box["height"] / 2, button="right")
    await page.wait_for_selector(MENU)


@BOTH
async def test_a_press_on_another_message_bar_closes_the_menu(chat: tuple[Page, Fake]) -> None:
    """034-AC9: a press outside closes the menu even where the target keeps the focus (the bar's buttons);
    that bar's action then runs as usual."""
    page, fake = chat
    await _menu_left(page, "2")
    four = await page.locator(_msg("4")).bounding_box()
    assert four
    await page.mouse.move(four["x"] + four["width"] - 30, four["y"] + four["height"] / 2)
    await page.locator(f"{_msg('4')} [data-act=react]").click()
    assert await page.locator(MENU).count() == 0
    await page.locator(f'{_msg("4")} .picker [data-fav="❤️"]').click()
    await wait(lambda: fake.of("react") == [{"react": T, "msg": "4", "key": "2764-fe0f", "remove": False}])


@BOTH
async def test_a_message_above_growing_in_a_short_list_moves_the_menu(pages: Pages) -> None:
    """034-AC9: in a list too short to scroll, a message above growing (an image loading) moves the
    menu's message down without any scroll: the menu moves with it."""
    page = await _open(pages, Fake(history=[HISTORY[30], HISTORY[-1]]))
    try:
        await _menu(page, "4", ".html")
        gap = await page.evaluate(_gap("4"))
        top = await page.locator(_msg("4")).evaluate("n => n.getBoundingClientRect().top")
        await page.evaluate("document.querySelector('[data-id=\"1\"] .html').style.minHeight = '120px'")
        await until(page, f"document.querySelector({_msg('4')!r}).getBoundingClientRect().top > {top} + 50")
        assert await page.evaluate("(l => l.scrollHeight <= l.clientHeight)(document.getElementById('list'))")
        await until(page, f"Math.abs(({_gap('4')}) - {gap}) < 1")
        assert await page.locator(MENU).count() == 1
    finally:
        await pages.close()


@BOTH
async def test_the_list_scrolled_without_a_resize_moves_the_menu(chat: tuple[Page, Fake]) -> None:
    """034-AC9: the list scrolled by the page itself, nothing resized: the menu moves with its message."""
    page, _ = chat
    await _menu_left(page, "2")
    gap = await page.evaluate(GAP)
    await page.evaluate("document.getElementById('list').scrollTop -= 40")
    await until(page, f"Math.abs(({GAP}) - {gap}) < 1")
    assert await page.locator(MENU).count() == 1


async def test_a_chat_switch_closes_the_menu(pages: Pages) -> None:
    """034-AC9: switching chats in the split page, by a row's click or by Back, removes the open menu."""
    split = ux.Split(pages, ux.Fake(rows=ux._board(4)))
    page = await split.open()
    try:
        await page.wait_for_selector(_msg("1"))
        await _menu(page, "1")
        await page.locator(f'#rows li[data-id="{ux._id(1)}"] .name').click()
        await wait(lambda: split.fake.follows()[-1:] == [ux._id(1)])
        await until(page, f"!document.querySelector('{MENU}')")
        await page.wait_for_selector(_msg("1"))
        await _menu(page, "1")
        await page.evaluate("history.back()")  # no click: the focus stays in the menu until the switch
        await wait(lambda: split.fake.follows()[-1:] == [ux._id(2)])
        await until(page, f"!document.querySelector('{MENU}')")
    finally:
        await pages.close()
    assert pages.external == []


# Selects the word's paragraph on a right press, after the page saw the press: as macOS does before its menu.
MAC_SELECT = """addEventListener("pointerdown", (e) => { if (e.button === 2 && e.target.matches(".html p")) {
  const r = document.createRange(); r.selectNodeContents(e.target);
  getSelection().removeAllRanges(); getSelection().addRange(r); } })"""


async def test_a_selection_is_read_as_the_press_found_it(pages: Pages) -> None:
    """034-AC6: a right click that selects the word under it (macOS) still opens our menu; a selection made
    before it keeps the browser's; a keyboard open reads the selection as it is then, not an earlier
    press's. (Chromium: the keyboard opens.)"""
    page = await _open(pages, Fake(history=HISTORY), init=MAC_SELECT)
    try:
        await _menu(page, "2")
        assert await page.evaluate("getSelection().toString()") == "second line"  # the press selected it
        await page.keyboard.press("Escape")
        await _right_click(page, f"{_msg('2')} .html p >> nth=-1")  # the selection now predates the press
        assert await page.evaluate("window.__prevented") is False
        assert await page.locator(MENU).count() == 0
        await page.evaluate("getSelection().removeAllRanges()")
        await page.locator(_msg("2")).focus()
        await page.keyboard.press("Shift+F10")  # the last press found a selection; there is none now
        await page.wait_for_selector(MENU)
        await page.keyboard.press("Escape")
        await page.evaluate("window.__prevented = undefined")
        await page.locator(f"{_msg('2')} .html p >> nth=-1").evaluate(
            "p => { const r = document.createRange(); r.selectNodeContents(p);"
            " getSelection().removeAllRanges(); getSelection().addRange(r); }"
        )
        await page.keyboard.press("Shift+F10")
        await until(page, "window.__prevented === false")
        assert await page.locator(MENU).count() == 0
    finally:
        await pages.close()


@BOTH
async def test_ctrl_click_opens_at_the_pointer_a_keyboard_zero_at_the_message(
    chat: tuple[Page, Fake],
) -> None:
    """034-AC1 + 034-AC10: a macOS Ctrl+click (button 0 with Ctrl) opens at the pointer; a button 0 without
    Ctrl (a keyboard's, in Firefox) under the message's top left."""
    page, _ = chat
    p = page.locator(f"{_msg('2')} .html p >> nth=-1")
    box, msg = await p.bounding_box(), await page.locator(_msg("2")).bounding_box()
    assert box and msg
    x, y = box["x"] + 10, box["y"] + 5
    for ctrl, at in ((True, (x, y)), (False, (msg["x"], msg["y"]))):
        init = {"bubbles": True, "cancelable": True, "button": 0, "ctrlKey": ctrl, "clientX": x, "clientY": y}
        await p.evaluate("(p, i) => p.dispatchEvent(new MouseEvent('contextmenu', i))", init)
        await page.wait_for_selector(MENU)
        menu = await page.locator(MENU).bounding_box()
        assert menu and abs(menu["x"] - at[0]) < 1 and abs(menu["y"] - at[1]) < 1, ctrl
        await page.keyboard.press("Escape")
