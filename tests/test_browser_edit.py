"""Editing a message like composing one, on the chat page (spec 019 phase 2), against the composer files'
fake chat socket. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_edit.py`.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import browser_helpers
import pytest
from browser_helpers import ANN, Page, Pages, T, closed, shows, until, wait
from test_browser_chat_compose import MINE, Fake, _open

from miniteams import chat_verbs as CV
from miniteams.compose import resolve_mentions

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)
POP = "#editpop-1"
EDITOR = '[data-id="1"] .editor'
E1, E2 = "1789466500000", "1789466600000"  # edittimes (ms epochs)
Q1, Q2 = "1760000000001", "1760000000002"  # quoted message ids
I1, I2 = "0-weu-d1-aaaa", "0-weu-d1-bbbb"  # AMS object ids


def _form(text: str = "mine", edittime: str = E1, **over: Any) -> dict[str, Any]:
    return {"ok": True, "text": text, "picks": [], "quotes": [], "images": [], "edittime": edittime, **over}


RICH = _form(
    "**hi** @Ann Lee",
    picks=[{**ANN, "at": 7}],
    quotes=[
        {"key": Q1, "name": "Ann Lee", "line": "first"},
        {"key": Q2, "name": "Bob Roe", "line": "second"},
    ],
    images=[
        {"key": I1, "src": "/emoji/0-aaaa", "alt": ""},
        {"key": I2, "src": "/emoji/0-bbbb", "alt": "chart"},
    ],
)


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    board = Fake()
    await _open(pages, board)
    yield board
    await pages.close()
    assert pages.external == []


async def _edit(fake: Fake) -> Any:
    """✎ on message 1; the editor once filled from the fake's `editable` answer."""
    page = fake.page
    assert page is not None
    await page.locator('[data-id="1"] [data-act=edit]').dispatch_event("click")
    await page.wait_for_selector(f"{EDITOR}:not([readonly])")
    return page.locator(EDITOR)


async def _select(page: Page, value: str, a: int, b: int) -> None:
    await page.locator(EDITOR).evaluate(
        "(e, [v, a, b]) => { e.value = v; e.focus(); e.setSelectionRange(a, b); }", [value, a, b]
    )


def _frame(page: Page, frame: dict[str, Any]) -> Any:
    """A frame straight into the page's socket handler, as if the server had sent it."""
    return page.evaluate(
        "f => window.__ws.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(f) }))", frame
    )


def _record(html: str, edittime: str) -> dict[str, Any]:
    return {
        "edit": {**MINE, "html": html, "editable": True, "edittime": edittime, "reactions": []},
        "thread": T,
    }


@BOTH
async def test_the_editor_completes_like_the_composer_and_posts_the_mention(fake: Fake) -> None:
    """019-AC1: `:` and `@` in the editor open the composer's completion; the picked mention is posted."""
    page = fake.page
    assert page is not None
    box = await _edit(fake)
    await box.press("End")
    await box.press_sequentially(" :fi")
    await shows(page, ["🔥:fire:", "🐟:fish:"], pop=POP)
    await box.press("Enter")
    await box.press_sequentially(" @ann")
    await shows(page, ["Ann Lee"], pop=POP)
    assert await box.get_attribute("aria-controls") == POP[1:]
    await box.press("Enter")
    assert await box.input_value() == "mine 🔥 @Ann Lee "
    assert await closed(page)  # the composer's list never opened
    await box.press("Enter")
    await wait(lambda: len(fake.of("edit")) == 1)
    assert fake.of("edit") == [
        {
            "edit": T,
            "msg": "1",
            "text": "mine 🔥 @Ann Lee ",
            "mentions": [{**ANN, "at": 8}],  # UTF-16: the emoji counts two (spec 033)
            "keep_quotes": [],
            "keep_images": [],
        }
    ]
    assert [v["thread"] for v in fake.of("complete")] == [T, T]


@BOTH
async def test_the_editor_formats_and_previews_like_the_composer(fake: Fake) -> None:
    """019-AC2: Ctrl+B / Ctrl+I and the mini bar's buttons format the selection; 👁 shows the preview the
    composer shows for the same text, and hides it again."""
    page = fake.page
    assert page is not None
    box = await _edit(fake)
    for how, expected in (
        ("Control+b", "say **hi** now"),
        ("Control+i", "say *hi* now"),
        ("strike", "say ~~hi~~ now"),
        ("code", "say `hi` now"),
        ("list", "- say hi now"),
        ("quote", "> say hi now"),
    ):
        await _select(page, "say hi now", 4, 6)
        if how.startswith("Control"):
            await box.press(how)
        else:
            await page.locator(f'[data-id="1"] .tools [data-fmt="{how}"]').click()
        assert await box.input_value() == expected, how
    assert await page.locator("#reply").input_value() == ""  # the composer's bar stayed out of it
    text = "**b** _i_\n- one"
    await page.locator("#reply").fill(text)
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await box.fill(text)
    eye = page.locator('[data-id="1"] .tools [aria-pressed]')
    assert await eye.inner_text() == "👁"
    await eye.click()
    view = page.locator('[data-id="1"] .view')
    await view.locator(".pv").wait_for()
    assert await view.inner_html() == await page.locator("#view").inner_html()
    assert await view.get_attribute("class") == await page.locator("#view").get_attribute("class")
    assert await eye.get_attribute("aria-pressed") == "true"
    await eye.click()
    assert await view.is_hidden()
    await eye.click()
    await view.locator(".pv").wait_for()
    await box.press_sequentially("x")  # a change hides it, as in the composer
    assert await view.is_hidden()
    await fake.settled()
    assert fake.of("edit") == []


async def test_quotes_and_images_show_and_only_the_kept_ones_are_posted(fake: Fake) -> None:
    """019-AC4 + 019-AC6 + 019-AC7: the editor opens on the form's text with its quotes as chips above and
    its images as thumbnails under it; ✕ / × drop one each, and the save posts the form's text and picks
    with the other keys only."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = RICH
    box = await _edit(fake)
    assert await box.input_value() == RICH["text"]
    chips = page.locator('[data-id="1"] .editbox .quote')
    assert await chips.all_inner_texts() == ["Ann Lee\nfirst\n✕", "Bob Roe\nsecond\n✕"]
    above, field = await chips.first.bounding_box(), await box.bounding_box()
    thumbs = page.locator('[data-id="1"] .editbox .imgs img')
    under = await thumbs.first.bounding_box()
    assert above and field and under and above["y"] < field["y"] < under["y"]
    assert await thumbs.evaluate_all("ns => ns.map(n => [n.getAttribute('src'), n.alt])") == [
        ["/emoji/0-aaaa", "image 1"],
        ["/emoji/0-bbbb", "chart"],
    ]
    await chips.first.locator("button").click()
    await page.locator('[data-id="1"] .imgs [aria-label="remove image 2"]').click()
    assert await chips.count() == 1 and await thumbs.count() == 1
    await box.press("Enter")
    await wait(lambda: len(fake.of("edit")) == 1)
    assert fake.of("edit") == [
        {
            "edit": T,
            "msg": "1",
            "text": RICH["text"],
            "mentions": [{**ANN, "at": RICH["text"].index("@")}],
            "keep_quotes": [Q2],
            "keep_images": [I1],
        }
    ]
    await page.wait_for_selector('[data-id="1"] .html')  # the ack closes it


@pytest.mark.parametrize("drop", ["", "quote", "image", "text"])
async def test_enter_with_nothing_changed_sends_nothing(fake: Fake, drop: str) -> None:
    """019-AC11: Enter on the untouched editor closes it and sends no edit; a dropped quote, a dropped
    image or a changed text is a change."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = RICH
    box = await _edit(fake)
    if drop == "quote":
        await page.locator('[data-id="1"] .editbox .quote button').first.click()
    elif drop == "image":
        await page.locator('[data-id="1"] .imgs .rm').first.click()
    elif drop == "text":
        await box.press_sequentially("!")
    await box.press("Enter")
    if drop:
        await wait(lambda: len(fake.of("edit")) == 1)
        return
    assert await page.locator(".editor").count() == 0
    assert await page.locator('[data-id="1"] .html').inner_text() == "mine"
    await fake.settled()
    assert fake.of("edit") == []


async def _clock(page: Page, edittime: str) -> str:
    """HH:MM of `edittime` in the browser's time zone, computed here."""
    offset = await page.evaluate("t => new Date(t).getTimezoneOffset()", int(edittime))
    at = datetime.fromtimestamp(int(edittime) / 1000, UTC) - timedelta(minutes=offset)
    return at.strftime("%H:%M")


async def test_an_edit_made_elsewhere_is_said_and_can_be_taken(fake: Fake) -> None:
    """019-AC10: a reaction (with or without the body) says nothing; a new edittime shows "edited elsewhere
    at HH:MM" with "take that version", which reloads text, quotes and images; saving stays possible."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form()
    box = await _edit(fake)
    await box.fill("my draft")
    note = page.locator('[data-id="1"] .edit-note')
    await _frame(
        page, {"edit": {"id": "1", "reactions": [{"key": "like", "emoji": "👍", "count": 1}]}, "thread": T}
    )
    await _frame(page, _record("<p>mine</p>", E1))
    await fake.handled()
    assert await note.is_hidden()
    fake.forms["1"] = {**RICH, "edittime": E2}
    await _frame(page, _record("<p>changed</p>", E2))
    await note.wait_for()
    assert await note.inner_text() == f"edited elsewhere at {await _clock(page, E2)} · take that version"
    assert await box.input_value() == "my draft"
    await note.locator("button").click()
    await until(page, f"document.querySelector('{EDITOR}').value === {RICH['text']!r}")
    assert await note.is_hidden()
    assert await page.locator('[data-id="1"] .editbox .quote').count() == 2
    assert await page.locator('[data-id="1"] .editbox .imgs img').count() == 2
    assert len(fake.of("editable")) == 2
    await _frame(page, _record("<p>again</p>", "1789466700000"))
    await note.wait_for()
    await box.press_sequentially(" ok")
    await box.press("Enter")  # last write wins
    await wait(lambda: len(fake.of("edit")) == 1)
    assert fake.of("edit")[0]["text"] == RICH["text"] + " ok"


_ERR = "document.querySelector('[data-id=\"1\"] .edit-err').textContent !== ''"
TWO = [{"key": Q1, "name": "Ann Lee", "line": "first"}, {"key": Q2, "name": "Bob Roe", "line": "second"}]


async def test_a_changed_message_keeps_the_editor_and_the_typed_text_on_reopen(fake: Fake) -> None:
    """019 Decisions ("Reopen" after a refusal keeps a typed text): the server's CHANGED refusal keeps the
    editor and its text, says why; "reopen" asks for the form again (a failed opening offers the same) and
    takes its quotes and images, the text typed meanwhile kept, its mention still picked; the save then
    sends them."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form(quotes=TWO)
    fake.edit_error = CV.CHANGED
    box = await _edit(fake)
    await box.press("End")
    await box.press_sequentially(" @ann")
    await shows(page, ["Ann Lee"], pop=POP)
    await box.press("Enter")
    await box.press("Enter")
    err = page.locator('[data-id="1"] .edit-err')
    await until(page, _ERR)
    assert await err.inner_text() == f"{CV.CHANGED} · reopen"
    assert await box.input_value() == "mine @Ann Lee "
    fake.forms["1"] = {"error": "unknown message"}
    await err.locator("button", has_text="reopen").click()
    await until(page, _ERR)
    assert await err.inner_text() == "unknown message · reopen"
    assert await box.input_value() == "mine @Ann Lee "
    fake.forms["1"] = _form("theirs now", E2, quotes=TWO[1:])
    fake.edit_error = ""
    await err.locator("button", has_text="reopen").click()
    await page.wait_for_selector(f"{EDITOR}:not([readonly])")
    assert await box.input_value() == "mine @Ann Lee "
    assert await page.locator('[data-id="1"] .editbox .quote').all_inner_texts() == ["Bob Roe\nsecond\n✕"]
    assert await err.inner_text() == ""
    assert len(fake.of("editable")) == 3
    await box.press("Enter")
    await wait(lambda: len(fake.of("edit")) == 2)
    assert fake.of("edit")[1] == {
        "edit": T,
        "msg": "1",
        "text": "mine @Ann Lee ",
        "mentions": [{**ANN, "at": 5}],
        "keep_quotes": [Q2],
        "keep_images": [],
    }


async def test_reopen_with_the_text_untouched_takes_the_new_text(fake: Fake) -> None:
    """019 Decisions ("Reopen" after a refusal keeps a typed text): nothing typed since opening, "reopen"
    shows the message as it is now."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form(quotes=TWO)
    fake.edit_error = CV.CHANGED
    box = await _edit(fake)
    await page.locator('[data-id="1"] .editbox .quote button').first.click()  # a change: Enter saves
    await box.press("Enter")
    await until(page, _ERR)
    fake.forms["1"] = _form("theirs now", E2)
    await page.locator('[data-id="1"] .edit-err button', has_text="reopen").click()
    await until(page, f"document.querySelector('{EDITOR}').value === 'theirs now'")


async def test_a_reopen_answered_blocked_keeps_the_typed_text(fake: Fake) -> None:
    """019 Decisions (a reopen answered "not editable here" keeps a typed text read-only): the editor stays
    with the typed text, the reason and Open in Teams on its error line; Enter sends nothing, Escape
    closes."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form(quotes=TWO)
    fake.edit_error = CV.CHANGED
    box = await _edit(fake)
    await box.fill("my edit")
    await box.press("Enter")
    await until(page, _ERR)
    fake.forms["1"] = {"blocked": "a table"}
    err = page.locator('[data-id="1"] .edit-err')
    await err.locator("button", has_text="reopen").click()
    await until(page, 'document.querySelector(\'[data-id="1"] .edit-err\')?.textContent.startsWith("can\'t")')
    assert await err.inner_text() == "can't edit here: a table · Open in Teams"
    assert await err.locator("a", has_text="Open in Teams").count() == 1
    assert await box.input_value() == "my edit"
    assert await box.get_attribute("readonly") is not None
    assert await page.locator('[data-id="1"] .blocked').count() == 0
    await box.press("Enter")
    await fake.settled()
    assert len(fake.of("edit")) == 1  # the refused one only
    await box.press("Escape")
    assert await page.locator(".editor").count() == 0


async def test_a_reopen_answered_blocked_with_nothing_typed_closes_the_editor(fake: Fake) -> None:
    """019-AC9 + 019 Decisions (a reopen answered "not editable here"): with nothing typed there is nothing
    to keep: the editor closes and the message says why, with Open in Teams."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form(quotes=TWO)
    fake.edit_error = CV.CHANGED
    await _edit(fake)
    await page.locator('[data-id="1"] .editbox .quote button').first.click()  # a change: Enter saves
    await page.locator(EDITOR).press("Enter")
    await until(page, _ERR)
    fake.forms["1"] = {"blocked": "a table"}
    await page.locator('[data-id="1"] .edit-err button', has_text="reopen").click()
    line = page.locator('[data-id="1"] .blocked')
    await line.wait_for()
    assert await line.inner_text() == "can't edit here: a table · Open in Teams"
    assert await page.locator(".editor").count() == 0


async def test_an_edit_elsewhere_during_a_failed_save_is_said(fake: Fake) -> None:
    """019-AC10: a newer version relayed while a save was in flight shows its note once the save fails."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form()
    box = await _edit(fake)
    await box.fill("mine v2")
    fake.hold = True
    await box.press("Enter")
    await wait(lambda: len(fake.held) == 1)
    await _frame(page, _record("<p>changed</p>", E2))
    await fake.handled()
    note = page.locator('[data-id="1"] .edit-note')
    assert await note.is_hidden()  # not while it saves
    fake.send({"edit": T, "msg": "1", "error": "busy"})
    await note.wait_for()
    assert await note.inner_text() == f"edited elsewhere at {await _clock(page, E2)} · take that version"
    assert (
        await page.locator('[data-id="1"] .edit-err').inner_text()
        == "another action is still running, try again"
    )


async def test_an_edit_elsewhere_during_a_lost_save_is_said(fake: Fake) -> None:
    """019-AC10: a newer version relayed while a save was in flight shows its note once the socket drop
    ends the save, before the page reconnects (1 s)."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form()
    box = await _edit(fake)
    await box.fill("mine v2")
    fake.hold = True
    await box.press("Enter")
    await wait(lambda: len(fake.held) == 1)
    await _frame(page, _record("<p>changed</p>", E2))
    await fake.handled()
    await fake.ws.close()
    await until(page, "!document.querySelector('[data-id=\"1\"] .edit-note').hidden", tries=50)
    assert len(fake.of("follow")) == 1  # said before the reconnect's restore could


async def test_an_edit_landing_while_the_editor_loads_is_said(fake: Fake) -> None:
    """019-AC10 + editable busy retry: a busy answer is asked again, the editor then opens; a change
    relayed while it loaded, newer than the form it got, shows the note at once."""
    page = fake.page
    assert page is not None
    fake.hold = True
    fake.forms["1"] = {"error": "busy"}
    await page.locator('[data-id="1"] [data-act=edit]').dispatch_event("click")
    await wait(lambda: len(fake.held) == 1)
    await _frame(page, _record("<p>changed</p>", E2))
    fake.forms["1"] = _form()  # E1: older than the change relayed
    fake.hold = False
    fake.release(0)
    await page.wait_for_selector(f"{EDITOR}:not([readonly])")
    assert len(fake.of("editable")) == 2
    note = page.locator('[data-id="1"] .edit-note')
    await note.wait_for()
    assert await note.inner_text() == f"edited elsewhere at {await _clock(page, E2)} · take that version"


async def test_an_older_record_raises_no_note(fake: Fake) -> None:
    """019-AC10: a record older than the form the editor opened on (relayed late) is no edit elsewhere."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form(edittime=E2)
    await _edit(fake)
    await _frame(page, _record("<p>older</p>", E1))
    await fake.handled()
    assert await page.locator('[data-id="1"] .edit-note').is_hidden()


async def test_an_editable_busy_every_time_ends_on_the_error_line(fake: Fake) -> None:
    """Editable busy retry: after BUSY_TRIES retries the editor says so, with "reopen"."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = {"error": "busy"}
    await page.locator('[data-id="1"] [data-act=edit]').dispatch_event("click")
    await until(page, _ERR, tries=1000)
    assert await page.locator('[data-id="1"] .edit-err').inner_text() == (
        "another action is still running, try again · reopen"
    )
    assert len(fake.of("editable")) == 11  # the first ask and 10 retries
    assert await page.locator(EDITOR).get_attribute("readonly") is not None


async def test_a_restored_editor_starts_with_its_preview_off(fake: Fake) -> None:
    """019-AC2: an editor rebuilt after a reconnect while the composer shows its preview gets an unpressed
    👁 of its own."""
    page = fake.page
    assert page is not None
    box = await _edit(fake)
    await box.fill("kept")
    await page.locator("#reply").fill("a")
    await page.locator("#show").click()
    await page.locator("#view .pv").wait_for()
    await fake.ws.close()
    await wait(lambda: len(fake.of("history")) == 2)  # reconnected, the page read again
    await until(page, f"document.querySelector('{EDITOR}')?.value === 'kept'")
    assert await page.locator("#show").get_attribute("aria-pressed") == "true"
    eye = page.locator('[data-id="1"] .tools [aria-pressed]')
    assert await eye.get_attribute("aria-pressed") == "false"
    assert len(fake.of("editable")) == 1  # restored from what it held


async def test_the_message_is_text_in_the_editor(fake: Fake) -> None:
    """019-AC12: markup in the message's text, a quote's name and line, an image's alt and a blocking
    reason shows as text, never as elements."""
    page = fake.page
    assert page is not None
    fake.forms["1"] = _form(
        "<b>x</b>",
        quotes=[{"key": Q1, "name": "<i>n</i>", "line": "<img src=/emoji/0-q onerror=alert(1)>"}],
        images=[{"key": I1, "src": "/emoji/0-aaaa", "alt": "<b>alt</b>"}],
    )
    box = await _edit(fake)
    assert await box.input_value() == "<b>x</b>"
    chip = page.locator('[data-id="1"] .editbox .quote')
    assert await chip.all_inner_texts() == ["<i>n</i>\n<img src=/emoji/0-q onerror=alert(1)>\n✕"]
    assert await chip.locator("i, img").count() == 0
    assert await page.locator('[data-id="1"] .imgs img').get_attribute("alt") == "<b>alt</b>"
    assert await page.locator('[data-id="1"] .imgs b').count() == 0
    await box.press("Escape")
    fake.forms["1"] = {"blocked": "<b>a table</b>"}
    await page.locator('[data-id="1"] [data-act=edit]').dispatch_event("click")
    line = page.locator('[data-id="1"] .blocked')
    await line.wait_for()
    assert await line.inner_text() == "can't edit here: <b>a table</b> · Open in Teams"
    assert await line.locator("b").count() == 0
    assert await page.locator(".editor").count() == 0
    await fake.settled()
    assert fake.of("edit") == []


@BOTH
async def test_backspace_after_a_kept_brace_is_an_ordinary_backspace(fake: Fake) -> None:
    """033 review: an edit opens with a kept `@{Ann Lee|Ann} `; Backspace at its end deletes the space, never
    shortens the brace to `@{Ann`, and the save carries the kept mention."""
    page = fake.page
    assert page is not None
    kept = {"name": "{Ann Lee|Ann}", "mri": ANN["mri"], "at": 3}
    fake.forms["1"] = _form("hi @{Ann Lee|Ann} ", picks=[kept])
    box = await _edit(fake)
    await box.press("End")
    await box.press("Backspace")
    assert await box.input_value() == "hi @{Ann Lee|Ann}"
    await box.press("Enter")
    await wait(lambda: len(fake.of("edit")) == 1)
    [sent] = fake.of("edit")
    assert (sent["text"], sent["mentions"]) == ("hi @{Ann Lee|Ann}", [kept])


@BOTH
@pytest.mark.parametrize("keys", [["Control+z"], ["Control+z", "Control+Shift+z"]])
@pytest.mark.parametrize(
    ("other", "outcome"),
    [("Bob Roe", [ANN["mri"]]), ("Bob Lee", "a mention the edit would read as someone else")],
)
async def test_an_undo_in_an_edit_keeps_the_kept_mention_its_own(
    fake: Fake, keys: list[str], other: str, outcome: Any
) -> None:
    """033 review r4/r5: a departed Ann's kept `@{Lee|Lee}`; text typed then undone (or undone and redone),
    then more typed: the page dropped its anchors; the save, read by the server, still mentions Ann while
    nobody else answers to `Lee`, and is refused when Bob Lee does, never sent to him."""
    page = fake.page
    assert page is not None
    kept = {"name": "{Lee|Lee}", "mri": ANN["mri"]}
    fake.forms["1"] = _form("hi @{Lee|Lee}", picks=[{**kept, "at": 3}])
    box = await _edit(fake)
    await box.press("End")
    await box.press_sequentially(" ok")
    for key in keys:
        await box.press(key)
    await box.press("End")
    await box.press_sequentially("!")  # unchanged, the editor would send nothing
    await box.press("Enter")
    await wait(lambda: len(fake.of("edit")) == 1)
    [verb] = fake.of("edit")
    assert verb["mentions"] == []  # the page lost them
    request = CV.parse_act("edit", verb)
    assert isinstance(request, CV.ActRequest) and request.text is not None
    roster = [{"mri": "8:orgid:me", "name": "Me Self"}, {"mri": "8:orgid:other", "name": other}]
    lost = CV._kept_unpicked(request.text, request.mentions, [{**kept, "at": 3}], roster, T, "8:orgid:me")
    if isinstance(outcome, str):
        assert lost == outcome
        return
    assert not isinstance(lost, str)
    anchored = CV._anchored_picks(T, CV._humans(roster), lost, lost)
    assert not isinstance(anchored, str)
    got = resolve_mentions(request.text, roster, T, "8:orgid:me", picks=anchored)
    assert [p["mri"] for p in got] == outcome


@BOTH
async def test_a_body_only_edit_frame_keeps_the_rest_of_the_row(fake: Fake) -> None:
    """037-AC1: a refreshed card comes as `{id, html, type}`: the row's body changes, its reactions and
    edited mark stay."""
    page = fake.page
    assert page is not None
    reactions = [{"key": "like", "emoji": "👍", "count": 1, "mine": False, "people": ["Ann Lee"]}]
    shown = _record("<p>card OPEN</p>", E1)
    await _frame(page, {**shown, "edit": {**shown["edit"], "edited": True, "reactions": reactions}})
    await page.locator('[data-id="1"] .edited').wait_for()
    await _frame(page, {"edit": {"id": "1", "html": "<p>card DONE</p>", "type": "text"}, "thread": T})
    await until(page, "document.querySelector('[data-id=\"1\"] .html').textContent.includes('card DONE')")
    assert await page.locator('[data-id="1"] .edited').count() == 1
    assert "👍" in await page.locator('[data-id="1"]').inner_text()
