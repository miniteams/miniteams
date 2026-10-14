"""Read receipts on the chat page in a headless browser (spec 011 amendment, phase 2): Element X layout.

Run: `uv run --with playwright pytest tests/test_browser_receipts.py`. Synthetic data, no network."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import browser_helpers
import httpx
import pytest
import test_web_chat
from browser_helpers import Page, Pages, until  # skips without playwright
from test_web_chat import ANN, ME, T, _archive, _at, _m, _served

from miniteams import receipts as R
from miniteams import web as W
from miniteams.config import Settings

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, quiet_directory = test_web_chat.board, test_web_chat.quiet_directory

BOB = "8:orgid:00000000-0000-0000-0000-00000000000c"
CID = "8:orgid:00000000-0000-0000-0000-00000000000d"
DAN = "8:orgid:00000000-0000-0000-0000-00000000000e"
EVE = "8:orgid:00000000-0000-0000-0000-00000000000f"
FOLDED = "19:fedcba9876543210fedcba9876543210@thread.v2"  # Ann 10, deleted 20-40 (one folded run), Bob 50
NAMES = {ANN: "Ann Lee", BOB: "Bob Ray", CID: "Cid Moe", DAN: "Dan Fox", EVE: "Eve Kim", ME: "Me Too"}
# Who wrote each archived message (id = 10 × index): Ann 10-20, me 30-40, Bob 50, me 60.
AUTHORS = {1: ANN, 2: ANN, 3: ME, 4: ME, 5: BOB, 6: ME}
# Per message: the reader initials shown on it, its "+N", its hover.
READS = """[...document.querySelectorAll('#msgs > .msg')].map(n => [n.dataset.id,
  [...n.querySelectorAll('.reads .ra')].map(a => a.textContent),
  n.querySelector('.reads .more')?.textContent || '', n.querySelector('.reads')?.title || ''])"""


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    messages = [
        _m(
            str(10 * i),
            _at(i),
            **{"from": f"https://h/v1/users/ME/contacts/{mri}", "imdisplayname": NAMES[mri]},
        )
        for i, mri in AUTHORS.items()
    ]
    run = [
        _m(str(10 * i), _at(i), **({"properties": {"deletetime": "1"}} if i in (2, 3, 4) else {}))
        for i in range(1, 6)
    ]
    _archive(path, FOLDED, run)
    return _archive(path, T, messages)


class Horizons:
    """The chat service's snapshot: `members` answer GET /v1/threads/<id>/consumptionhorizons."""

    def __init__(self) -> None:
        self.members: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith("/consumptionhorizons")
        items = [{"id": m, "consumptionhorizon": f"{at};1;1"} for m, at in self.members.items()]
        return httpx.Response(200, json={"id": T, "consumptionhorizons": items})


@pytest.fixture
async def served(board: W.Board, settings: Settings, pages: Pages) -> AsyncIterator[tuple[Pages, Horizons]]:
    assert board.directory is not None
    for mri, name in NAMES.items():
        board.directory.note_name(mri.lower(), name)
    horizons = Horizons()
    client = httpx.AsyncClient(transport=httpx.MockTransport(horizons.handler))
    board.receipts = R.Receipts(board.directory, ME, board._receipts_frame, None, client)
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield pages, horizons
        await pages.close()
    await board.receipts.close()
    assert pages.external == []


async def _open(pages: Pages) -> Page:
    chat = await pages.open(f"/chat/{T}")
    await until(chat, "document.querySelectorAll('#msgs > .msg').length === 6")
    return chat


def _live(board: W.Board, mri: str, at: int) -> Any:
    content = f'{{"user": "{mri}", "consumptionhorizon": "{at};1;1"}}'
    resource = {
        "messagetype": R.EVENT,
        "conversationLink": f"https://h/v1/users/ME/conversations/{T}",
        "content": content,
    }
    return board.on_event({"resourceType": "NewMessage", "resource": resource})


async def test_each_reader_sits_on_the_newest_message_they_read_but_their_own(
    served: tuple[Pages, Horizons],
) -> None:
    """011-AC2/AC4: Ann (read to 40) on my message 40; Bob (read to 50) not on his own message; Cid,
    read up to an id no message shows (a control message), on the newest message before it; my own
    horizon and Dan (no receipt: unknown) show nothing. The hover names the reader."""
    pages, horizons = served
    horizons.members = {ANN: "40", BOB: "50", CID: "35", ME: "60"}  # Dan: absent
    chat = await _open(pages)
    await until(chat, "document.querySelectorAll('.reads').length === 2")
    assert await chat.evaluate(READS) == [
        ["10", [], "", ""],
        ["20", [], "", ""],
        ["30", ["CM"], "", "Cid Moe"],
        ["40", ["AL"], "", "Ann Lee"],
        ["50", [], "", ""],
        ["60", [], "", ""],
    ]


async def test_more_than_three_readers_show_three_and_plus_n_and_the_hover_lists_all(
    served: tuple[Pages, Horizons],
) -> None:
    pages, horizons = served
    horizons.members = {ANN: "60", BOB: "60", CID: "60", DAN: "60", EVE: "60"}
    chat = await _open(pages)
    await until(chat, "document.querySelector('.reads') !== null")
    assert (await chat.evaluate(READS))[5] == [
        "60",
        ["AL", "BR", "CM"],
        "+2",
        "Ann Lee\nBob Ray\nCid Moe\nDan Fox\nEve Kim",
    ]
    label = await chat.evaluate("document.querySelector('.reads').getAttribute('aria-label')")
    assert label == "Read by Ann Lee, Bob Ray, Cid Moe, Dan Fox, Eve Kim"


async def test_a_live_receipt_moves_the_avatar_without_touching_the_messages(
    board: W.Board, served: tuple[Pages, Horizons]
) -> None:
    """011-AC3: the avatar moves on; message times and order stay; an older event does not move it back."""
    pages, horizons = served
    horizons.members = {ANN: "30"}
    chat = await _open(pages)
    await until(chat, "document.querySelector('[data-id=\"30\"] .reads') !== null")
    before = await chat.evaluate(
        "[...document.querySelectorAll('#msgs > .msg')].map(n => [n.dataset.id, n.dataset.at])"
    )
    await _live(board, ANN, 60)
    await until(chat, "document.querySelector('[data-id=\"60\"] .reads') !== null")
    assert await chat.evaluate("document.querySelectorAll('.reads').length") == 1
    await _live(board, ANN, 40)  # late: never regresses
    await _live(board, BOB, 20)  # a new reader: proves the frame after the late one was painted
    await until(chat, "document.querySelector('[data-id=\"20\"] .reads') !== null")
    assert await chat.evaluate("document.querySelector('[data-id=\"60\"] .reads .ra').textContent") == "AL"
    after = await chat.evaluate(
        "[...document.querySelectorAll('#msgs > .msg')].map(n => [n.dataset.id, n.dataset.at])"
    )
    assert after == before


async def test_unknown_receipts_show_nothing(board: W.Board, served: tuple[Pages, Horizons]) -> None:
    """011-AC4: nobody with a receipt (or only us) → no indicator at all, never an "unread" one."""
    pages, horizons = served
    horizons.members = {ME: "60"}
    chat = await _open(pages)
    await until(chat, "document.querySelectorAll('.msg').length === 6")
    await _live(board, ME, 60)
    await _live(board, BOB, 10)  # a later frame: the earlier ones were painted by now
    await until(chat, "document.querySelector('[data-id=\"10\"] .reads') !== null")
    assert await chat.evaluate("document.querySelectorAll('.reads').length") == 1


async def test_a_reader_inside_a_folded_run_sits_on_its_visible_head(served: tuple[Pages, Horizons]) -> None:
    """Cid read up to 30, hidden in the folded run of deleted messages 20-40: the run's head shows him."""
    pages, horizons = served
    horizons.members = {CID: "30"}
    chat = await pages.open(f"/chat/{FOLDED}")
    await until(chat, "document.querySelector('.reads') !== null")
    assert await chat.evaluate("[...document.querySelectorAll('.msg.folded')].map(n => n.dataset.id)") == [
        "30",
        "40",
    ]
    assert await chat.evaluate("document.querySelector('.reads').closest('.msg').dataset.id") == "20"


GAP = "(l => l.scrollHeight - l.scrollTop - l.clientHeight)(document.getElementById('list'))"


async def test_a_live_receipt_at_the_bottom_stays_in_view(
    board: W.Board, served: tuple[Pages, Horizons]
) -> None:
    """The avatar row grows the newest message: a page stuck to the bottom follows it."""
    pages, _ = served
    chat = await pages.open(f"/chat/{T}", viewport={"width": 700, "height": 260})
    await until(chat, "document.querySelectorAll('#msgs > .msg').length === 6")
    await until(chat, f"{GAP} < 2 && document.getElementById('list').scrollTop > 0")
    await _live(board, BOB, 60)
    await until(chat, "document.querySelector('[data-id=\"60\"] .reads') !== null")
    await until(chat, f"{GAP} < 2")  # the list follows in its resize observer, before the next paint


async def test_a_live_receipt_does_not_scroll_a_page_read_higher_up(
    board: W.Board, served: tuple[Pages, Horizons]
) -> None:
    pages, _ = served
    chat = await pages.open(f"/chat/{T}", viewport={"width": 700, "height": 260})
    await until(chat, "document.querySelectorAll('#msgs > .msg').length === 6")
    await until(chat, f"{GAP} < 2 && document.getElementById('list').scrollTop > 0")
    await chat.evaluate(
        "(l => { l.scrollTop = 0; l.dispatchEvent(new Event('scroll')); })(document.getElementById('list'))"
    )
    await _live(board, BOB, 60)
    await until(chat, "document.querySelector('[data-id=\"60\"] .reads') !== null")
    assert await chat.evaluate("document.getElementById('list').scrollTop") == 0
