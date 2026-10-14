"""Presence dots in a headless browser (spec 015): widget rows, chat page rows and message authors.

Run: `uv run --with playwright pytest tests/test_browser_presence.py`. Synthetic data, no network."""

from collections.abc import AsyncIterator
from typing import Any

import browser_helpers
import httpx
import pytest
import test_web_chat
from browser_helpers import Page, Pages, until, wait  # skips without playwright
from test_web_chat import ANN, ME, T, _at, _event, _m, _served

from miniteams import presence as P
from miniteams import web as W
from miniteams.config import Settings

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, data_dir, quiet_directory = test_web_chat.board, test_web_chat.data_dir, test_web_chat.quiet_directory

DM = f"19:{ME.removeprefix('8:orgid:')}_{ANN.removeprefix('8:orgid:')}@unq.gbl.spaces"
DOTS = "[...document.querySelectorAll('[data-pmri] > .pres')]"


def _entry(availability: str, activity: str = "") -> dict[str, Any]:
    return {"presence": [{"mri": ANN, "presence": {"availability": availability, "activity": activity}}]}


@pytest.fixture
async def served(board: W.Board, settings: Settings, pages: Pages) -> AsyncIterator[tuple[Pages, P.Presence]]:
    board.rows[DM] = {"id": DM, "label": "Ann Lee", "last_activity": _at(9), "last_id": "9", "typing": []}
    board.rows[DM].update(seen_at=None, muted=False, mention=None)

    def nothing(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no presence request without a socket session")

    client = httpx.AsyncClient(transport=httpx.MockTransport(nothing))
    presence = board.presence = P.Presence(settings, lambda: "tok", board.broadcast_presence, client=client)
    board.broadcast()  # the follow list now holds the 1:1 peer
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield pages, presence
        await pages.close()
    await presence.close()
    assert pages.external == []


async def _dots(page: Page) -> list[list[str]]:
    return list(await page.evaluate(f"{DOTS}.map(d => [d.className, d.parentElement.title])"))


async def test_one_person_dot_on_every_avatar_updates_live_and_unknown_shows_none(
    served: tuple[Pages, P.Presence],
) -> None:
    """015-AC3/AC4: the 1:1 row (widget + chat list) and the author avatars get one dot with the
    "Availability · Activity" hover; a callback repaints all of them, a plain chat page too; unknown
    removes them."""
    pages, presence = served
    widget = await pages.open("/")
    chat = await pages.open(f"/chat/{T}?chatnav=split")
    plain = await pages.open(f"/chat/{T}")  # no board list: reached as a follower, not a board client
    await until(chat, "document.querySelectorAll('.msg .av[data-pmri]').length > 0")
    await until(chat, "document.querySelector('#rows .rav[data-pmri]') !== null")
    await until(plain, "document.querySelectorAll('.msg .av[data-pmri]').length > 0")
    assert ANN in presence.wanted
    for page in (widget, chat, plain):
        assert await page.evaluate(DOTS + ".length") == 0  # nothing known yet: no dot, not Offline

    await presence.on_presence(_entry("Busy", "InAMeeting"))
    await until(widget, DOTS + ".length === 1")
    authors = await chat.evaluate("document.querySelectorAll('.msg .av[data-pmri]').length")
    await until(chat, f"{DOTS}.length === {authors + 1}")
    await until(plain, f"{DOTS}.length === {authors}")
    for page in (widget, chat, plain):
        assert set(map(tuple, await _dots(page))) == {("pres busy", "Busy · In a meeting")}

    await presence.on_presence(_entry("Available", "Available"))  # no reload: the same nodes repaint
    for page in (widget, chat, plain):
        await until(page, DOTS + ".every(d => d.className === 'pres ok')")
    assert set(map(tuple, await _dots(chat))) == {("pres ok", "Available")}

    await presence.on_presence(_entry("PresenceUnknown"))
    for page in (widget, chat, plain):
        await until(page, DOTS + ".length === 0")
        assert await page.evaluate("[...document.querySelectorAll('[data-pmri]')].every(a => !a.title)")


async def test_cached_state_is_faded_and_says_unconfirmed(served: tuple[Pages, P.Presence]) -> None:
    """015-AC7: a state from the cache shows at once, faded, its hover saying it is unconfirmed."""
    pages, presence = served
    presence.states[ANN] = P.State("Away", "Away", presence.clock() + 600, False)
    widget = await pages.open("/")
    await until(widget, DOTS + ".length === 1")
    assert await _dots(widget) == [["pres away unsure", "Away (unconfirmed)"]]
    opacity = await widget.evaluate(f"getComputedStyle({DOTS}[0]).opacity")
    assert float(opacity) < 1


async def test_chat_page_reports_its_authors_and_the_board_drops_them_on_close(
    board: W.Board, served: tuple[Pages, P.Presence]
) -> None:
    """015-AC6: an open chat page adds its authors to the follow list; closing it removes them."""
    pages, presence = served
    board.rows.pop(DM)
    board.broadcast()
    assert ANN not in presence.wanted
    chat = await pages.open(f"/chat/{T}")
    await until(chat, "document.querySelectorAll('.msg .av[data-pmri]').length > 0")
    await wait(lambda: ANN in presence.wanted, "authors followed")
    assert list(board._authors.values()) == [[ANN]]
    await pages.close()
    await wait(lambda: not board._authors, "chat socket gone")
    assert ANN not in presence.wanted


async def test_every_availability_has_its_dot_and_hover(served: tuple[Pages, P.Presence]) -> None:
    """015-AC3: each availability the service sends maps to its dot colour and its spelled-out hover."""
    pages, presence = served
    widget = await pages.open("/")
    await until(widget, "document.querySelector('[data-pmri]') !== null")
    cases = [
        ("Available", "Available", "pres ok", "Available"),
        ("AvailableIdle", "Inactive", "pres ok", "Available idle · Inactive"),
        ("Busy", "InACall", "pres busy", "Busy · In a call"),
        ("BusyIdle", "Inactive", "pres busy", "Busy idle · Inactive"),
        ("DoNotDisturb", "Presenting", "pres dnd", "Do not disturb · Presenting"),
        ("Away", "Away", "pres away", "Away"),
        ("BeRightBack", "BeRightBack", "pres away", "Be right back"),
        ("Offline", "OffWork", "pres off", "Offline · Off work"),
    ]
    for availability, activity, cls, hover in cases:
        await presence.on_presence(_entry(availability, activity))
        await until(widget, f"{DOTS}.length === 1 && {DOTS}[0].className === {cls!r}")
        assert await _dots(widget) == [[cls, hover]], availability


async def test_a_live_message_gets_its_author_dot_as_it_renders(
    board: W.Board, served: tuple[Pages, P.Presence]
) -> None:
    """015-AC4: a message arriving live from a person whose presence the page knows shows the dot at once."""
    pages, presence = served
    chat = await pages.open(f"/chat/{T}")
    await until(chat, "document.querySelectorAll('.msg .av[data-pmri]').length > 0")
    await presence.on_presence(_entry("Busy", "InAMeeting"))
    await until(chat, DOTS + ".length > 0")
    # Recorded at insertion: a later repaint (the authors round-trip) must not be what paints it.
    await chat.evaluate(
        """window.dotAtInsert = null;
        new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes)
          if (n.dataset?.id === "6") window.dotAtInsert = !!n.querySelector(".av > .pres"); })
        .observe(document.getElementById("msgs"), { childList: true, subtree: true });"""
    )
    await board.on_event(_event("NewMessage", T, **_m("6", _at(6), "<p>live</p>")))
    await until(chat, "window.dotAtInsert !== null")
    assert await chat.evaluate("window.dotAtInsert") is True
