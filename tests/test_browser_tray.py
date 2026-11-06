"""The widget page's side of the native window's tray (spec 024): the ⚙ "Tray" section, the badge
count it publishes and the notifications it raises, against a fake board and a stubbed bridge. Synthetic
data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_tray.py`.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import browser_helpers
import pytest
from browser_helpers import (  # skips without playwright
    ORIGIN,
    SOCKET_DOWN,
    Page,
    Pages,
    open_fake,
    row,
    until,
)

from miniteams import web as W

pytestmark = [pytest.mark.browser, pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)]
pages = browser_helpers.pages

PAGE = W._page("widget.html")
A, B, C, D, E = (f"19:t{x}@thread.v2" for x in "abcde")
MENTION = {"kind": "me", "by": "Ann Lee", "text": "hi", "at": "2026-10-09T10:00:00.000Z", "edited": False}
ROWS = [
    row(A, "Fictional A", unread=True, last_id="1"),
    row(B, "Fictional B", unread=True, mention=MENTION, last_id="1"),
    row(C, "Fictional C", unread=True, muted=True, last_id="1"),
    row(D, "Fictional D", seen_at="2026-10-09T11:00:00.000Z", last_id="1"),
    row(E, "Fictional E", unread=True, muted=True, mention=MENTION, last_id="1"),
]
# What the native window injects (widget_qt.BOOT), stubbed: every badge() call lands in window.__badges,
# every notify() in window.__notes, play() in __plays, preview() in __previews, choose() in __chooses;
# window.__opened is what a notification's click calls, window.__chosen the dialog's answer.
BRIDGE = """window.__badges = []; window.__notes = []; window.__plays = []; window.__previews = [];
window.__chooses = []; window.__customs = {};
window.miniteamsTray = Promise.resolve({ badge: (pref, n) => window.__badges.push([pref, n]),
  notify: (...a) => window.__notes.push(a),
  play: (...a) => window.__plays.push(a), preview: (...a) => window.__previews.push(a),
  choose: (r) => window.__chooses.push(r),
  sounds: () => window.__fail ? Promise.reject(new Error("bridge gone"))
    : Promise.resolve(["bell", "message-new-instant"]),
  customs: () => Promise.resolve(structuredClone(window.__customs)),  // a copy, as the channel sends
  chosen: { connect: (f) => { window.__chosen = f; } },
  settings: { connect: (f) => { window.__settings = f; } },
  opened: { connect: (f) => { window.__opened = f; } } });"""
LAST = "JSON.stringify(window.__badges.at(-1))"
TABS = """Object.fromEntries([...document.querySelectorAll('#tabs button')]
  .map(b => [b.firstChild.textContent, +b.querySelector('b').textContent]))"""
MENU = "[...document.querySelectorAll('.menu h3')].map(h => h.textContent)"


class Fake(browser_helpers.Fake):
    quiet: bool = False  # a reconnection gets no board frame until the test sends one
    sockets: int = 0
    first: dict[str, Any] | None = None  # merged into the first frame of every connection

    def attach(self, ws: Any) -> None:
        super().attach(ws)
        self.sockets += 1
        if not self.quiet:
            self.send(
                {"rows": ROWS, "page": "1", "opener": False, "browser": False, "me": "Me"}
                | (self.first or {})
            )


async def _open(pages: Pages, fake: Fake, init: str = "") -> Page:
    page = await open_fake(
        pages,
        fake,
        ORIGIN + "/",
        PAGE,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
        init=init,
    )
    await page.wait_for_selector("a.name")
    return page


@pytest.fixture
async def native(pages: Pages) -> AsyncIterator[Fake]:
    fake = Fake()
    await _open(pages, fake, BRIDGE)
    yield fake
    await pages.close()
    assert pages.external == []


async def _published(page: Page, pref: str, n: int) -> None:
    await until(page, f"{LAST} === JSON.stringify([{pref!r}, {n}])")


async def test_without_the_bridge_the_page_is_unchanged(pages: Pages) -> None:
    """024-AC5: in a browser ⚙ has no Tray section and nothing is published."""
    fake = Fake()
    page = await _open(pages, fake)
    await page.locator("#gear").click()
    assert await page.evaluate(MENU) == [
        "Theme",
        "Display",
        "Click opens",
        "Middle click opens",
        "Chat navigation",
    ]
    assert await page.locator("#tray").count() == 0
    assert await page.evaluate("'__badges' in window") is False
    await pages.close()


async def test_tray_section_shows_in_the_native_window(native: Fake) -> None:
    """024-AC5: with the bridge, ⚙ gains its Tray section, Unread chosen by default."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    assert await page.evaluate(MENU) == [
        "Theme",
        "Display",
        "Click opens",
        "Middle click opens",
        "Chat navigation",
        "Tray",
        "Notify",
    ]
    buttons = page.locator("#tray button")
    assert await buttons.all_text_contents() == ["Unread", "@", "Inbox", "None"]
    assert await page.locator("#tray button.on").text_content() == "Unread"


async def test_settings_from_the_tray_opens_the_section(native: Fake) -> None:
    """024-AC5: tray → Settings… (the bridge's `settings` signal) opens ⚙ on the Tray section."""
    page = native.page
    assert page is not None
    await until(page, "typeof window.__settings === 'function'")
    assert await page.locator(".menu").count() == 0
    await page.evaluate("window.__settings()")
    await page.locator(".menu #tray").wait_for()


async def test_badge_counts_each_tab_rule_without_the_filter(native: Fake) -> None:
    """024-AC4: each preference publishes its tab's count with an empty filter box (muted rules
    included); typing in the filter changes the tabs, not the badge; the choice is kept."""
    page = native.page
    assert page is not None
    tabs = await page.evaluate(TABS)
    assert tabs == {"Inbox": 3, "@": 2, "Unread": 2, "Seen": 1, "Muted": 2}
    await _published(page, "unread", tabs["Unread"])
    await page.locator("#gear").click()
    for label, pref in (("@", "mention"), ("Inbox", "all"), ("None", "none"), ("Unread", "unread")):
        await page.locator("#tray button", has_text=re.compile(f"^{re.escape(label)}$")).click()
        await _published(page, pref, 0 if pref == "none" else tabs[label])
        assert await page.evaluate("localStorage.getItem('badge')") == pref
    await page.locator("#tray button", has_text="Inbox").click()
    n = await page.evaluate("window.__badges.length")
    await page.locator("#q").fill("Fictional A")
    assert (await page.evaluate(TABS))["Inbox"] == 1
    native.send({"rows": ROWS})  # a broadcast while filtered still counts the whole tab
    await native.handled()
    assert await page.evaluate(f"window.__badges.slice({n}).every(([p, c]) => p === 'all' && c === 3)")
    await page.reload()
    await page.wait_for_selector("a.name")
    await _published(page, "all", 3)


async def test_badge_follows_board_broadcasts(native: Fake) -> None:
    """024-AC4: a new board frame publishes its new count."""
    page = native.page
    assert page is not None
    await _published(page, "unread", 2)
    native.send({"rows": [*ROWS, row("19:tf@thread.v2", "Fictional F", unread=True)]})
    await _published(page, "unread", 3)
    native.send({"rows": [r | {"unread": False} for r in ROWS]})
    await _published(page, "unread", 0)


async def test_a_lost_socket_clears_the_badge_until_the_board_is_back(native: Fake) -> None:
    """024-AC4: disconnected, the page publishes no count (a stale one would read as live); the board
    frame of the reconnection publishes it again."""
    page = native.page
    assert page is not None
    await _published(page, "unread", 2)
    await native.ws.close()
    await _published(page, "none", 0)
    await _published(page, "unread", 2)


async def test_a_choice_made_while_disconnected_publishes_no_count(native: Fake) -> None:
    """024-AC4: picking another count while the socket is down still shows none; the next board frame
    publishes the new choice."""
    page = native.page
    assert page is not None
    await _published(page, "unread", 2)
    native.quiet = True
    await native.ws.close()
    await _published(page, "none", 0)
    n = await page.evaluate("window.__badges.length")
    await page.locator("#gear").click()
    await page.locator("#tray button", has_text="Inbox").click()
    await until(page, f"window.__badges.length > {n}")
    assert await page.evaluate(LAST) == '["none",0]'
    assert await page.evaluate("localStorage.getItem('badge')") == "all"
    await browser_helpers.wait(lambda: native.sockets == 2, "reconnection")
    assert await page.evaluate(LAST) == '["none",0]'  # connected, but no board yet
    native.send({"rows": ROWS})
    await _published(page, "all", 3)


# --- notifications (024-AC10, AC11) ---

NOTES = "JSON.stringify(window.__notes)"
LATER = "2026-10-09T12:00:00.000Z"


class Board:
    """The fake board's rows across broadcasts: each change returns the whole list, as the server sends it."""

    def __init__(self) -> None:
        self.rows = {r["id"]: dict(r) for r in ROWS}

    def change(self, thread_id: str, **over: Any) -> list[dict[str, Any]]:
        self.rows[thread_id] = self.rows[thread_id] | over  # a new dict: frames built earlier keep theirs
        return list(self.rows.values())

    def said(self, thread_id: str, msg: str, sender: str = "Bob Roe", **over: Any) -> list[dict[str, Any]]:
        """`thread_id` got message `msg`."""
        said = {"last_id": msg, "last_activity": LATER, "sender": sender, "text": f"text {msg}"}
        return self.change(thread_id, **said | over)


def _at_me(msg: str, by: str = "Bob Roe") -> dict[str, Any]:
    return MENTION | {"by": by, "text": f"hey {msg}", "at": LATER, "msg_id": msg}


async def _notes(fake: Fake, *frames: list[dict[str, Any]]) -> list[list[str]]:
    """Every notification raised so far, once the page handled `frames`, each a board broadcast."""
    assert fake.page is not None
    for rows in frames:
        fake.send({"rows": rows})
    await fake.handled()
    notes: list[list[str]] = await fake.page.evaluate("window.__notes")
    return notes


ALERTS = ["me", "all", "dm", "group", "meeting", "start"]
LABELS = ["@ you", "@ everyone", "Direct message", "Group message", "Meeting chat", "Meeting start"]
STORED = """Object.fromEntries(Object.keys(localStorage).filter(k => /^(notify|meetings|sound)/.test(k))
  .sort().map(k => [k, localStorage.getItem(k)]))"""
# A switched-off row's sound picker is dimmed: its sound never plays (039-AC5).
DIMMED = """[...document.querySelectorAll('#notify .alert')]
  .filter(l => getComputedStyle(l.querySelector('select')).opacity < 1).map(l => l.dataset.row)"""
SWITCHES = "Object.fromEntries([...document.querySelectorAll('#notify .switch')].map(s => [s.id, s.checked]))"


def _prefs(**prefs: str) -> str:
    """An init script storing `prefs` (`notify_dm="1"` → `notify.dm`) before the page reads them."""
    return "".join(f"localStorage.setItem({k.replace('_', '.')!r}, {v!r});" for k, v in prefs.items())


def _only(row_name: str, mode: str) -> str:
    """Every row's switch off but `row_name`'s with `mode` notify or both; `row_name` has a sound with both,
    and with silent (its switch off)."""
    prefs = {f"notify_{k}": "1" if k == row_name and mode != "silent" else "0" for k in ALERTS}
    return _prefs(**prefs, **({f"sound_{row_name}": "bell"} if mode != "notify" else {}))


async def _toggle(page: Page, *rows: str, on: bool = True) -> None:
    await page.locator("#gear").click()
    for r in rows:
        await page.locator(f"#notify-{r}").set_checked(on)
    await page.keyboard.press("Escape")


async def _plays(page: Page) -> list[list[str]]:
    plays: list[list[str]] = await page.evaluate("window.__plays")
    return plays


async def test_notify_table_rows_defaults_and_kept(native: Fake) -> None:
    """039-AC1, 039-AC2, 039-AC11.
    ⚙ Notify is one row per interaction, in order, each with a switch, a sound picker
    and ▶; the old segment and Meeting start switch are gone; @ rows and Meeting start notify by default, no
    row has a sound; a switch and a sound survive a reload."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    assert await page.locator("#notify .alert label").all_text_contents() == LABELS
    assert await page.locator("#notify .seg").count() == 0
    for r in ALERTS:
        line = page.locator(f"#notify .alert[data-row={r}]")
        assert await line.locator(".switch").count() == 1
        assert await line.locator("select").input_value() == ""
        assert await line.locator("button.play").count() == 1
    defaults = {f"notify-{r}": r in ("me", "all", "start") for r in ALERTS}
    assert await page.evaluate(SWITCHES) == defaults
    assert await page.evaluate(STORED) == {}
    assert await page.evaluate(DIMMED) == [r for r in ALERTS if not defaults[f"notify-{r}"]]
    await page.locator("#notify-dm").check()
    assert "dm" not in await page.evaluate(DIMMED)
    await page.locator("[data-k='sound.group']").select_option("message-new-instant")
    assert await page.evaluate(STORED) == {"notify.dm": "1", "sound.group": "message-new-instant"}
    await page.reload()
    await page.wait_for_selector("a.name")
    await page.locator("#gear").click()
    assert await page.evaluate(SWITCHES) == defaults | {"notify-dm": True}
    await until(page, "document.querySelector(\"[data-k='sound.group']\").value === 'message-new-instant'")


async def test_the_sound_picker_lists_none_the_system_sounds_then_custom(native: Fake) -> None:
    """039-AC1: None, the tray's sounds without dashes, then Custom file…; a row's own file by name, marked
    (missing) when gone."""
    page = native.page
    assert page is not None
    ding, gone = "{ name: 'ding.ogg', missing: false }", "{ name: 'gone.wav', missing: true }"
    await page.evaluate(f"window.__customs = {{ dm: {ding}, group: {gone} }}")
    await page.evaluate("window.__chosen('dm', 'ding.ogg')")  # the dialog's answer refetches the customs
    await page.locator("#gear").click()
    texts = "(r) => [...document.querySelector(`[data-k='sound.${r}']`).options].map(o => o.textContent)"
    await until(page, f"({texts})('group').includes('gone.wav (missing)')")
    base = ["None", "bell", "message new instant"]
    assert await page.evaluate(f"({texts})('me')") == [*base, "Custom file…"]
    assert await page.evaluate(f"({texts})('dm')") == [*base, "ding.ogg", "Custom file…"]
    assert await page.evaluate(f"({texts})('group')") == [*base, "gone.wav (missing)", "Custom file…"]


async def test_custom_file_asks_the_window_and_its_answer_picks_it(pages: Pages) -> None:
    """039-AC1, 039-AC11: Custom file… asks the window for its dialog and goes back to the previous choice at
    once (a cancelled dialog answers nothing); the window's `chosen` makes the row's sound `custom`, shown by
    its file name, kept across a reload."""
    # The window's sounds.json outlives a reload: so does the stub's.
    kept = "window.__customs = JSON.parse(sessionStorage.getItem('customs') || '{}');"
    page = await _open(pages, Fake(), BRIDGE + kept)
    await page.locator("#gear").click()
    picker = page.locator("[data-k='sound.me']")
    await picker.select_option("bell")
    await picker.select_option("+")
    assert await page.evaluate("window.__chooses") == ["me"]
    assert await picker.input_value() == "bell"
    assert await page.evaluate(STORED) == {"sound.me": "bell"}
    await page.evaluate(
        "sessionStorage.setItem('customs', JSON.stringify({ me: { name: 'ding.ogg', missing: false } }));"
        "window.__customs = JSON.parse(sessionStorage.getItem('customs')); window.__chosen('me', 'ding.ogg')"
    )
    await until(
        page, "document.querySelector(\"[data-k='sound.me']\").selectedOptions[0].textContent === 'ding.ogg'"
    )
    assert await picker.input_value() == "custom"
    assert await page.evaluate(STORED) == {"sound.me": "custom"}
    await page.locator("#notify .alert[data-row=me] .play").click()
    assert await page.evaluate("window.__previews") == [["me", "custom"]]
    await page.reload()
    await page.wait_for_selector("a.name")
    await page.locator("#gear").click()
    await until(
        page, "document.querySelector(\"[data-k='sound.me']\").selectedOptions[0].textContent === 'ding.ogg'"
    )
    await pages.close()


async def test_customs_are_checked_each_time_the_menu_opens(pages: Pages) -> None:
    """039-AC9: a custom file gone while the window runs shows "(missing)" on ⚙'s next opening."""
    page = await _open(pages, Fake(), BRIDGE + _prefs(sound_dm="custom"))
    await page.evaluate("window.__customs = { dm: { name: 'ding.ogg', missing: false } }")
    shown = "document.querySelector(\"[data-k='sound.dm']\").selectedOptions[0].textContent"
    await page.locator("#gear").click()
    await until(page, f"{shown} === 'ding.ogg'")
    await page.keyboard.press("Escape")
    await page.evaluate("window.__customs.dm.missing = true")
    await page.locator("#gear").click()
    await until(page, f"{shown} === 'ding.ogg (missing)'")
    assert await page.locator("#notify .alert[data-row=dm] .play").is_disabled()
    await page.keyboard.press("Escape")
    await page.evaluate("window.__customs.dm.missing = false; window.__settings()")  # tray → Settings…
    await until(page, f"{shown} === 'ding.ogg'")
    await pages.close()


async def test_a_custom_sound_the_window_does_not_know_shows_missing(pages: Pages) -> None:
    """039-AC9: a row stored as custom with no file on the window's side: "custom file (missing)", ▶ off."""
    page = await _open(pages, Fake(), BRIDGE + _prefs(sound_me="custom"))
    await page.locator("#gear").click()
    await until(page, "document.querySelector(\"[data-k='sound.me']\").options.length > 3")
    picker = page.locator("[data-k='sound.me']")
    assert await picker.evaluate("s => s.selectedOptions[0].textContent") == "custom file (missing)"
    assert await page.locator("#notify .alert[data-row=me] .play").is_disabled()
    await pages.close()


async def test_arrowing_onto_custom_file_waits_for_enter(native: Fake) -> None:
    """039-AC1: keys change a closed picker (arrows, type-ahead): landing on Custom file… opens nothing, and
    leaving it there with Tab puts the stored sound back; Enter or Space then asks the window and the picker
    goes back; a pointer pick asks at once."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    picker = page.locator("[data-k='sound.me']")
    await until(page, "document.querySelector(\"[data-k='sound.me']\").options.length > 3")
    await picker.select_option("message-new-instant")
    await picker.focus()
    await page.keyboard.press("ArrowDown")
    assert await picker.input_value() == "+"
    assert await page.evaluate("window.__chooses") == []
    await page.keyboard.press("Enter")
    assert await page.evaluate("window.__chooses") == ["me"]
    assert await picker.input_value() == "message-new-instant"
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press(" ")
    assert await page.evaluate("window.__chooses") == ["me", "me"]
    await picker.select_option("+")
    assert await page.evaluate("window.__chooses") == ["me", "me", "me"]
    assert await picker.input_value() == "message-new-instant"
    await picker.focus()
    await page.keyboard.type("cu")  # type-ahead onto Custom file…
    assert await picker.input_value() == "+"
    assert await page.evaluate("window.__chooses") == ["me", "me", "me"]
    await page.keyboard.press("Tab")
    assert await picker.input_value() == "message-new-instant"
    assert await page.evaluate(STORED) == {"sound.me": "message-new-instant"}
    assert await page.evaluate("window.__chooses") == ["me", "me", "me"]
    await picker.focus()  # the list opened from the keyboard: a pick in it asks as a pointer one does
    await page.keyboard.press("Alt+ArrowDown")
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    assert await page.evaluate("window.__chooses") == ["me", "me", "me", "me"]
    assert await picker.input_value() == "message-new-instant"


async def test_a_failed_fetch_keeps_the_lists(native: Fake) -> None:
    """039-AC9: the window not answering `sounds()` leaves ⚙ with the last lists, and no page error."""
    page = native.page
    assert page is not None
    options = "[...document.querySelector(\"[data-k='sound.me']\").options].map(o => o.textContent)"
    await page.locator("#gear").click()
    await until(page, f"{options}.includes('bell')")
    await page.keyboard.press("Escape")
    await page.evaluate("window.__fail = true")
    await page.locator("#gear").click()
    await native.handled()
    assert "bell" in await page.evaluate(options)  # the fixture's teardown asserts no page error


async def test_a_system_sound_gone_from_the_machine_shows_missing(pages: Pages) -> None:
    """039-AC9: a stored theme sound `sounds()` no longer lists: "(missing)", ▶ off, once the lists came."""
    page = await _open(pages, Fake(), BRIDGE + _prefs(sound_dm="dialog-warning"))
    await page.locator("#gear").click()
    shown = "document.querySelector(\"[data-k='sound.dm']\").selectedOptions[0].textContent"
    await until(page, f"{shown} === 'dialog warning (missing)'")
    assert await page.locator("#notify .alert[data-row=dm] .play").is_disabled()
    await pages.close()


async def test_the_hint_describes_every_switch_and_picker(native: Fake) -> None:
    """039-AC1: the Notify hint is the description of each row's switch and sound picker."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    assert await page.locator("#notify-hint").text_content() == "Switch: desktop notification. List: sound."
    rows = page.locator("#notify .switch, #notify select")
    assert (
        await rows.evaluate_all("es => es.map(e => e.getAttribute('aria-describedby'))")
        == ["notify-hint"] * 12
    )


async def test_play_previews_the_rows_sound_and_is_off_on_none(native: Fake) -> None:
    """039-AC8: ▶ asks the window to preview the row's current sound; with None it is disabled."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    play = page.locator("#notify .alert[data-row=dm] .play")
    assert await play.is_disabled()
    assert await play.get_attribute("aria-label") == "play the Direct message sound"
    assert await play.get_attribute("title")
    assert await page.locator("[data-k='sound.dm']").get_attribute("aria-label") == "Direct message sound"
    await page.locator("[data-k='sound.dm']").select_option("message-new-instant")
    await play.click()
    assert await page.evaluate("window.__previews") == [["dm", "message-new-instant"]]
    assert await page.evaluate("window.__plays") == []


MIGRATIONS = [
    (
        {"notify": "mention"},
        {"notify.me": "1", "notify.all": "1", "notify.dm": "0", "notify.group": "0", "notify.meeting": "0"},
    ),
    ({"notify": "all"}, {f"notify.{k}": "1" for k in ALERTS[:5]}),
    ({"notify": "none"}, {f"notify.{k}": "0" for k in ALERTS[:5]}),
    ({"meetings": "0"}, {"notify.start": "0"}),
    ({"meetings": "1", "notify": "none"}, {f"notify.{k}": "0" for k in ALERTS[:5]} | {"notify.start": "1"}),
]


@pytest.mark.parametrize(
    ("old", "new"), MIGRATIONS, ids=["mention", "all", "none", "meetings-0", "meetings-1"]
)
async def test_old_prefs_migrate_once(pages: Pages, old: dict[str, str], new: dict[str, str]) -> None:
    """039-AC3: the coarse `notify` / `meetings` prefs become the rows they determine and are removed; a
    reload keeps the migrated values (nothing migrates twice)."""
    once = (
        f"if (!sessionStorage.getItem('seeded')) {{ {_prefs(**old)} sessionStorage.setItem('seeded', '1'); }}"
    )
    page = await _open(pages, Fake(), BRIDGE + once)
    for _ in range(2):
        assert await page.evaluate(STORED) == dict(sorted(new.items()))
        await page.locator("#gear").click()
        want = {
            f"notify-{k}": new.get(f"notify.{k}", "1" if k in ("me", "all", "start") else "0") == "1"
            for k in ALERTS
        }
        assert await page.evaluate(SWITCHES) == want
        await page.reload()
        await page.wait_for_selector("a.name")
    await pages.close()


async def test_a_row_already_set_is_not_migrated_over(pages: Pages) -> None:
    """039-AC3: an old `notify` key showing up after a row was set changes nothing, and is removed."""
    page = await _open(pages, Fake(), BRIDGE + _prefs(notify_dm="1"))
    await page.evaluate("localStorage.setItem('notify', 'none'); localStorage.setItem('meetings', '0')")
    await page.reload()
    await page.wait_for_selector("a.name")
    assert await page.evaluate(STORED) == {"notify.dm": "1"}
    await pages.close()


async def test_mentions_notify_a_new_mention_once(native: Fake) -> None:
    """024-AC10, 039-AC4: by default a mention appearing raises one notification (sender, chat, text), also
    in a muted chat (the @ tab's rule); a plain message, my own mention and the same mention again do not."""
    b = Board()
    assert await _notes(native, b.said(A, "2")) == []
    assert await _notes(native, b.change(A, mention=_at_me("m1"))) == [[A, "Bob Roe · Fictional A", "hey m1"]]
    # Broadcast again, cleared, then back (an edit re-adding it): still one.
    again = (b.change(A, mention=_at_me("m1")), b.change(A, mention=None), b.change(A, mention=_at_me("m1")))
    assert len(await _notes(native, *again)) == 1
    assert len(await _notes(native, b.change(C, mention=_at_me("m2")))) == 2  # muted, mentioned
    assert len(await _notes(native, b.change(D, mention=_at_me("m3", by="You")))) == 2


async def test_group_messages_follow_the_inbox_rule(native: Fake) -> None:
    """024-AC10, 039-AC4: Group message on, a new message notifies; not in a muted chat, not my own, not an
    activity line (no sender), not one older than the chat's Seen mark; a mention with its message, once."""
    page = native.page
    assert page is not None
    await _toggle(page, "group")
    b = Board()
    assert await _notes(native, b.said(A, "2")) == [[A, "Bob Roe · Fictional A", "text 2"]]
    assert len(await _notes(native, b.said(C, "3"))) == 1  # muted
    assert len(await _notes(native, b.said(A, "4", sender="You"))) == 1
    assert len(await _notes(native, b.said(A, "5", sender=""))) == 1
    early = b.change(D, last_id="6", last_activity="2026-10-09T10:30:00.000Z", sender="Bob Roe")
    assert len(await _notes(native, early)) == 1  # still Seen
    assert len(await _notes(native, b.said(D, "7"))) == 2  # newer than the mark: back in Inbox
    notes = await _notes(native, b.said(B, "8", mention=_at_me("8")), b.change(B, text="edited"))
    assert notes[2:] == [[B, "Bob Roe · Fictional B", "hey 8"]]


async def test_a_direct_chat_names_its_sender_once_and_text_is_clipped(native: Fake) -> None:
    """024-AC10: a sender who is the chat's name is not repeated; a long text is cut short."""
    page = native.page
    assert page is not None
    await _toggle(page, "group")
    notes = await _notes(native, Board().said(A, "2", sender="Fictional A", text="x" * 500))
    assert notes == [[A, "Fictional A", "x" * 199 + "…"]]


DM, DMM = "19:aa_bb@unq.gbl.spaces", "19:aa_cc@unq.gbl.spaces"
MEET, MEETM, MEETS = (f"19:meeting_{x}@thread.v2" for x in ("open", "muted", "start"))
STARTS = {"text": "📞 call started", "sender": ""}


def _everything(b: Board, n: int) -> list[dict[str, Any]]:
    """One frame bringing each interaction in its own chat, `n` making the ids new, plus their look-alikes
    that no row takes: a muted chat's message, my own message and my own mention."""
    b.said(C, f"me{n}", mention=_at_me(f"me{n}"))  # muted
    b.said(E, f"all{n}", mention=_at_me(f"all{n}") | {"kind": "all"})  # muted
    b.said(DM, f"dm{n}")
    b.said(DMM, f"dmm{n}")
    b.said(A, f"g{n}")
    b.said(MEET, f"mt{n}")
    b.said(MEETM, f"mtm{n}")
    b.said(MEETS, f"st{n}", **STARTS)
    return b.said(B, f"own{n}", sender="You", mention=_at_me(f"own{n}", by="You"))


EXPECTED = {
    "me": [C, "Bob Roe · Fictional C", "hey me1"],
    "all": [E, "Bob Roe · Fictional E", "hey all1"],
    "dm": [DM, "Bob Roe · Fictional dm", "text dm1"],
    "group": [A, "Bob Roe · Fictional A", "text g1"],
    "meeting": [MEET, "Bob Roe · Fictional meeting", "text mt1"],
    "start": [MEETS, "Fictional start", "📅 meeting started"],
}


@pytest.mark.parametrize("mode", ["notify", "both", "silent"])
@pytest.mark.parametrize("alert", ALERTS)
async def test_each_row_takes_only_its_interaction(pages: Pages, alert: str, mode: str) -> None:
    """039-AC4, 039-AC5: with only one row on (its switch, with or without its sound) a frame bringing every
    interaction raises that row's alone, under its mute rule (mentions and meeting starts in muted chats,
    messages not); my own message and mention never; a sound with its switch off plays nothing, and the row
    switched off raises nothing more."""
    fake = Fake()
    b = Board()
    for t, label, muted in (
        (DM, "dm", False),
        (DMM, "dmm", True),
        (MEET, "meeting", False),
        (MEETM, "meetingm", True),
        (MEETS, "start", True),
    ):
        b.rows[t] = row(t, f"Fictional {label}", muted=muted, last_id="1")
    fake.first = {"rows": list(b.rows.values())}
    page = await _open(pages, fake, BRIDGE + _only(alert, mode))
    await until(page, "typeof window.__opened === 'function'")
    notes = await _notes(fake, _everything(b, 1))
    plays = await _plays(page)
    expected = {
        "notify": ([EXPECTED[alert]], []),
        "both": ([EXPECTED[alert]], [[alert, "bell"]]),
        "silent": ([], []),
    }
    assert (notes, plays) == expected[mode]
    await _toggle(page, alert, on=False)
    assert await _notes(fake, _everything(b, 2)) == notes
    assert await _plays(page) == plays
    await pages.close()


async def test_a_row_with_a_sound_notifies_and_plays_once(pages: Pages) -> None:
    """039-AC5: a row with its switch and a sound: one interaction, one notification and one sound, broadcast
    again or not; a row with None plays nothing."""
    page = await _open(pages, fake := Fake(), BRIDGE + _prefs(notify_group="1", sound_me="bell"))
    await until(page, "typeof window.__opened === 'function'")
    b = Board()
    frames = (b.change(A, mention=_at_me("m1")), b.change(A, mention=None), b.change(A, mention=_at_me("m1")))
    assert len(await _notes(fake, *frames)) == 1
    assert await _plays(page) == [["me", "bell"]]
    assert len(await _notes(fake, b.said(A, "2"))) == 2  # group: on, no sound
    assert await _plays(page) == [["me", "bell"]]
    await pages.close()


async def test_a_mention_takes_precedence_over_its_message(pages: Pages) -> None:
    """039-AC4, 039-AC5: a message mentioning me in a group chat is the mention's (its sound), not the group
    message's; with the @ row off, the group row takes it, with its own sound."""
    init = _prefs(notify_group="1", sound_me="bell", sound_group="message-new-instant")
    page = await _open(pages, fake := Fake(), BRIDGE + init)
    await until(page, "typeof window.__opened === 'function'")
    b = Board()
    assert await _notes(fake, b.said(A, "2", mention=_at_me("2"))) == [[A, "Bob Roe · Fictional A", "hey 2"]]
    assert await _plays(page) == [["me", "bell"]]
    await page.locator("#gear").click()
    await page.locator("#notify-me").set_checked(False)
    await page.keyboard.press("Escape")
    notes = await _notes(fake, b.said(A, "3", mention=_at_me("3")))
    assert notes[1:] == [[A, "Bob Roe · Fictional A", "text 3"]]
    assert await _plays(page) == [["me", "bell"], ["group", "message-new-instant"]]
    await pages.close()


async def test_the_sound_is_the_notifying_rows(pages: Pages) -> None:
    """039-AC4, 039-AC5: one message, the rows in order (mention, meeting start, its chat's): the first row
    switched on notifies, once, with its own sound; a later row's sound stays silent."""
    page = await _open(pages, fake := Fake(), BRIDGE + _prefs(notify_group="1", sound_group="bell"))
    await until(page, "typeof window.__opened === 'function'")
    b = Board()
    frame = b.said(A, "2", mention=_at_me("2"))
    assert await _notes(fake, frame, frame) == [[A, "Bob Roe · Fictional A", "hey 2"]]  # @ you: on by default
    assert await _plays(page) == []
    await pages.close()


async def test_a_mention_row_switched_off_keeps_its_sound_silent(pages: Pages) -> None:
    """039-AC4, 039-AC5: @ rows off but @ you with a sound, Group message on without one: a mention in a group
    chat notifies as the group message, and nothing plays."""
    init = _prefs(notify_me="0", notify_all="0", sound_me="bell", notify_group="1")
    page = await _open(pages, fake := Fake(), BRIDGE + init)
    await until(page, "typeof window.__opened === 'function'")
    b = Board()
    assert await _notes(fake, b.said(A, "2", mention=_at_me("2"))) == [[A, "Bob Roe · Fictional A", "text 2"]]
    assert await _plays(page) == []
    await pages.close()


async def test_every_switch_off_notifies_nothing(native: Fake) -> None:
    """024-AC10, 039-AC4: every row off, nothing: mentions and meeting starts included."""
    page = native.page
    assert page is not None
    await _toggle(page, *ALERTS, on=False)
    b, meet = Board(), "19:meeting_fictional@thread.v2"
    b.rows[meet] = row(meet, "Fictional standup", last_id="1")
    await _notes(native, list(b.rows.values()))
    frames = (b.said(A, "2"), b.change(B, mention=_at_me("m1")), b.said(meet, "3", **STARTS))
    assert await _notes(native, *frames) == []
    assert await _plays(page) == []


async def test_meeting_start_notifies_on_its_own_switch(native: Fake) -> None:
    """039-AC4: a call starting in a meeting chat notifies, muted or not, with every other row off; not in a
    group chat, not my own call, not an ended one; switched off it raises nothing, and the choice is kept."""
    page = native.page
    assert page is not None
    await _toggle(page, "me", "all", on=False)
    b, meet = Board(), "19:meeting_fictional@thread.v2"
    b.rows[meet] = row(meet, "Fictional standup", muted=True, last_id="1")
    assert await _notes(native, list(b.rows.values())) == []
    assert await _notes(native, b.said(meet, "2", **STARTS)) == [
        [meet, "Fictional standup", "📅 meeting started"]
    ]
    others = (
        b.said(A, "3", **STARTS),
        b.said(meet, "4", text="📞 call ended", sender=""),
        b.said(meet, "5", text="📞 call started", sender="You"),
    )
    assert len(await _notes(native, *others)) == 1
    await _toggle(page, "start", on=False)
    assert len(await _notes(native, b.said(meet, "6", **STARTS))) == 1
    await page.reload()
    await page.wait_for_selector("a.name")
    await page.locator("#gear").click()
    assert not await page.locator("#notify-start").is_checked()


async def test_a_mention_wins_over_a_meeting_start_in_one_frame(native: Fake) -> None:
    """A frame carrying both a meeting start and a new mention in one chat raises the mention."""
    b, meet = Board(), "19:meeting_fictional@thread.v2"
    b.rows[meet] = row(meet, "Fictional standup", last_id="1")
    await _notes(native, list(b.rows.values()))
    both = b.said(meet, "2", text="📞 call started", sender="", mention=_at_me("m1"))
    assert await _notes(native, both) == [[meet, "Bob Roe · Fictional standup", "hey m1"]]


async def test_no_notification_for_a_snapshot_or_a_resync(native: Fake) -> None:
    """024-AC10, 039-AC5: the first board of a connection only sets what the next one is compared with: a
    reconnection's board, newer messages and mentions in it, raises nothing and plays nothing; the change
    after it does."""
    page = native.page
    assert page is not None
    await _toggle(page, "group")
    await page.locator("#gear").click()
    await page.locator("[data-k='sound.me']").select_option("bell")
    await page.keyboard.press("Escape")
    assert await page.evaluate(NOTES) == "[]"
    b = Board()
    native.first = {"rows": b.said(A, "2", mention=_at_me("m1"))}
    await native.ws.close()
    await browser_helpers.wait(lambda: native.sockets == 2, "reconnection")
    assert await _notes(native) == []
    assert await _plays(page) == []
    assert await _notes(native, b.said(A, "3")) == [[A, "Bob Roe · Fictional A", "text 3"]]


async def test_a_seeding_board_publishes_no_count_and_raises_nothing(pages: Pages) -> None:
    """024-AC4, AC10, 039-AC5: while the server still seeds (`busy`), no count is published, not even 0; the
    seeded board publishes it and raises nothing (no sound either) for what seeding brought; the change after
    it notifies."""
    fake = Fake()
    fake.first = {"rows": [], "busy": True}
    page = await open_fake(
        pages,
        fake,
        ORIGIN + "/",
        PAGE,
        page_route=ORIGIN + "/",
        ws_route=re.compile(r"^ws://127\.0\.0\.1:8/ws$"),
        init=BRIDGE + _prefs(sound_me="bell"),
    )
    await until(page, "typeof window.__opened === 'function'")
    b = Board()
    fake.send({"rows": list(b.rows.values()), "busy": True})
    fake.send({"rows": b.said(A, "2", mention=_at_me("m1")), "busy": True})
    await fake.handled()
    assert await page.evaluate("window.__badges.every(([p, n]) => p === 'none' && n === 0)")
    fake.send({"rows": list(b.rows.values()), "busy": False})
    await _published(page, "unread", 2)
    assert await page.evaluate(NOTES) == "[]"
    assert await _plays(page) == []
    fake.send({"rows": b.change(D, mention=_at_me("m9"))})
    await until(page, "window.__notes.length === 1 && window.__plays.length === 1")
    await pages.close()


async def test_a_click_opens_the_chat_as_its_row_does(pages: Pages) -> None:
    """024-AC11: the window's `opened(chat)` (a notification's Open) does what a plain click on the row
    does: the server's `open` with an opener, the row's link without one."""
    fake = Fake()
    fake.first = {"opener": True}
    page = await _open(
        pages, fake, BRIDGE + "window.__wins = []; window.open = (...a) => window.__wins.push(a);"
    )
    await until(page, "typeof window.__opened === 'function'")
    await page.locator(f"li[data-id='{A}'] a.name").click()
    await fake.settled()
    row_click = fake.of("open")
    assert row_click == [{"open": A, "web": False}]
    await page.evaluate(f"window.__opened({A!r})")
    await fake.settled()
    assert fake.of("open") == row_click * 2
    await page.evaluate("window.__opened('19:gone@thread.v2')")  # off the board since: nothing
    fake.send({"opener": False, "rows": Board().change(B, link="https://teams.microsoft.com/l/chat/b")})
    await fake.handled()
    await page.evaluate(f"window.__opened({B!r})")
    assert await page.evaluate("window.__wins") == [
        ["https://teams.microsoft.com/l/chat/b", "_blank", "noopener"]
    ]
    await fake.settled()
    assert fake.of("open") == row_click * 2
    await pages.close()
    assert pages.external == []


@pytest.mark.parametrize("browser", [True, False], ids=["browser", "no-browser"])
@pytest.mark.parametrize("click", ["teams", "chat", "app"])
async def test_a_click_follows_the_click_setting(pages: Pages, click: str, browser: bool) -> None:
    """035-AC6: a notification's Open follows ⚙ Click opens, as a plain click on the row does: Teams web or
    the chat page, through --browser when set, else a new window; `app` without an opener is Teams web."""
    fake = Fake()
    fake.first = {"opener": click != "app", "browser": browser}
    stub = "window.__wins = []; window.open = (...a) => window.__wins.push(a);"
    init = f"localStorage.setItem('click', {click!r}); {stub}"
    page = await _open(pages, fake, BRIDGE + init)
    await until(page, "typeof window.__opened === 'function'")
    fake.send({"rows": Board().change(A, link="https://teams.microsoft.com/l/chat/a")})
    await fake.handled()
    await page.evaluate(f"window.__opened({A!r})")
    await fake.settled()
    if browser:
        assert fake.of("open") == [{"open": A, "chat": True} if click == "chat" else {"open": A, "web": True}]
        assert await page.evaluate("window.__wins") == []
    else:
        assert fake.of("open") == []
        url = "/chat/19%3Ata%40thread.v2" if click == "chat" else "https://teams.microsoft.com/l/chat/a"
        assert await page.evaluate("window.__wins") == [[url, "_blank", "noopener"]]
    await pages.close()
    assert pages.external == []


async def test_a_click_with_the_socket_down_opens_the_row_link(pages: Pages) -> None:
    """035-AC6: a notification's Open while the socket is down opens the row's link, not nothing."""
    fake = Fake()
    fake.first = {"opener": True, "browser": True}
    stub = "window.__wins = []; window.open = (...a) => window.__wins.push(a);"
    page = await _open(pages, fake, BRIDGE + stub)
    await until(page, "typeof window.__opened === 'function'")
    fake.send({"rows": Board().change(A, link="https://teams.microsoft.com/l/chat/a")})
    await fake.handled()
    await page.evaluate(SOCKET_DOWN)
    await page.evaluate(f"window.__opened({A!r})")
    assert await page.evaluate("window.__wins") == [
        ["https://teams.microsoft.com/l/chat/a", "_blank", "noopener"]
    ]
    assert fake.of("open") == []
    await pages.close()
    assert pages.external == []


async def test_remembered_notifications_are_capped(native: Fake) -> None:
    """The page runs for days: it remembers exactly the last 500 notified messages. Past that the oldest is
    forgotten (its mention back notifies again); the second oldest is still remembered."""
    many = [row(f"19:m{i}@thread.v2", f"Fictional M{i}", last_id="1") for i in range(501)]
    mentioned = [r | {"mention": _at_me(f"x{i}")} for i, r in enumerate(many)]
    assert len(await _notes(native, ROWS + many, ROWS + mentioned)) == 501

    def without(i: int) -> list[dict[str, Any]]:
        return ROWS + [r | {"mention": None} if j == i else r for j, r in enumerate(mentioned)]

    assert len(await _notes(native, without(1), ROWS + mentioned)) == 501  # 500 kept: m1 is one of them
    notes = await _notes(native, without(0), ROWS + mentioned)
    assert [n[0] for n in notes[501:]] == ["19:m0@thread.v2"]  # 501 would have kept m0


# Every ⚙ control drawn whole: inside the menu, inside the window, no segment scrolled out of sight;
# the menu itself ends inside the window.
CLIPPED = """(() => { const m = document.querySelector('.menu').getBoundingClientRect(), out = [];
  for (const e of document.querySelectorAll('.menu button, .menu .switch, .menu select, .menu label')) {
    const r = e.getBoundingClientRect();
    const narrow = e.tagName === 'SELECT' && r.width < 90;  // a picker too thin to read its sound
    if (r.left < Math.max(m.left, 0) || r.right > Math.min(m.right, innerWidth) || narrow)
      out.push(e.dataset.k || e.textContent || e.id);
  }
  for (const s of document.querySelectorAll('.menu .seg'))
    if (s.scrollWidth > s.clientWidth) out.push(s.parentNode.id || 'seg');
  if (m.bottom > innerHeight) out.push('bottom');
  return out; })()"""
# The Notify rows' switches, all in one column.
COLUMN = (
    "new Set([...document.querySelectorAll('#notify .switch')].map(s => s.getBoundingClientRect().left)).size"
)


@pytest.mark.parametrize(("width", "height"), [(267, 700), (200, 700), (267, 450), (700, 700)])
async def test_settings_fit_a_narrow_window(native: Fake, width: int, height: int) -> None:
    """039-AC12: the native window is narrow, sometimes short: every ⚙ choice, switch and sound picker shows
    whole, none cut by the window edge or hidden in a scrolled segment; the Notify switches line up; the menu
    scrolls to its last choice and a choice or a live frame keeps it there."""
    page = native.page
    assert page is not None
    await page.set_viewport_size({"width": width, "height": height})
    await page.locator("#gear").click()
    await page.locator(".menu #notify").wait_for()
    await until(page, "document.querySelector(\"[data-k='sound.me']\").options.length > 2")
    assert await page.evaluate(CLIPPED) == []
    assert await page.evaluate(COLUMN) == 1
    top = "document.querySelector('.menu').scrollTop"
    await page.locator("#notify-start").uncheck()
    assert not await page.locator("#notify-start").is_checked()
    if height < 700:  # scrolled: a choice or a live frame rebuilds the menu where it was
        assert (scrolled := await page.evaluate(top)) > 0
        await page.evaluate("window.__menu = document.querySelector('.menu')")
        native.send({"rows": ROWS, "page": "1", "opener": False, "browser": False, "me": "Me"})
        await until(page, "document.querySelector('.menu') !== window.__menu")
        assert await page.evaluate(top) == scrolled


async def test_a_live_frame_leaves_a_focused_sound_picker_alone(native: Fake) -> None:
    """039-AC1: a frame rebuilding ⚙ would close a picker's open list: while one has the focus, the menu
    stays; a choice in it rebuilds the menu, the focus kept on the picker."""
    page = native.page
    assert page is not None
    await page.locator("#gear").click()
    await page.locator("[data-k='sound.dm']").focus()
    await page.evaluate("window.__menu = document.querySelector('.menu')")
    native.send({"rows": Board().said(A, "2")})
    await native.handled()
    assert await page.evaluate("document.querySelector('.menu') === window.__menu")
    await page.locator("[data-k='sound.dm']").select_option("bell")
    await until(page, "document.querySelector('.menu') !== window.__menu")
    assert await page.evaluate("document.activeElement.dataset.k") == "sound.dm"
