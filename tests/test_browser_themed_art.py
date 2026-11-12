"""Teams' art for themed reactions and body emojis on the chat page (spec 042), in a headless browser.

Run: `uv run --with playwright pytest tests/test_browser_themed_art.py`. Synthetic data, CDN mocked."""

from collections.abc import AsyncIterator
from pathlib import Path

import browser_helpers
import pytest
import test_web_chat
from browser_helpers import PNG, Page, Pages, until  # skips without playwright
from test_web_chat import ANN, T, _archive, _at, _m, _served

from miniteams import emojis
from miniteams import web as W
from miniteams.config import Settings

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
board, quiet_directory = test_web_chat.board, test_web_chat.quiet_directory

KEYS = ["rock", "1f918_signofthehorns"]
# Per chip: its image's alt (None: no image), its text, its tooltip lines' emoji; the body's alts and text.
CHIPS = """[...document.querySelectorAll('.reacts > *')].map(c =>
  [c.querySelector('img')?.alt ?? null, c.textContent, c.title.split('\\n').map(l => [...l][0]).join('')])"""
BODY = """(() => { const h = document.querySelector('.msg .html');
  return [[...h.querySelectorAll('img')].map(i => i.alt), h.textContent]; })()"""


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    reacts = {"emotions": [{"key": k, "users": [{"mri": ANN}]} for k in KEYS]}
    body = '<p>hi <emoji id="rock" alt="🤘" title="Rock"></emoji> <emoji id="like" alt="👍"></emoji></p>'
    return _archive(tmp_path / "data", T, [_m("1", _at(1), body, properties=reacts)])


@pytest.fixture
async def served(
    board: W.Board, settings: Settings, pages: Pages, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Pages, list[str]]]:
    fetched: list[str] = []

    async def fake_download(emoticon: str) -> bytes:
        fetched.append(emoticon)
        raise OSError("CDN down")

    monkeypatch.setattr(emojis, "_download_art", fake_download)
    async with _served(board, settings) as host:
        pages.serve(host, board)
        yield pages, fetched
        await pages.close()
    assert pages.external == []  # 042: the page never loads Teams' CDN itself


async def _open(pages: Pages) -> Page:
    chat = await pages.open(f"/chat/{T}")
    await until(chat, "document.querySelectorAll('.reacts > *').length === 2")
    return chat


async def test_themed_chip_and_body_show_teams_art(
    served: tuple[Pages, list[str]], settings: Settings
) -> None:
    """042-AC1, 042-AC5: `rock` shows its art, alt and tooltip 🤘; the plain 🤘 and 👍 stay characters."""
    pages, fetched = served
    cache = settings.cache_dir / "teams-emojis"
    cache.mkdir(parents=True)
    (cache / "rock.png").write_bytes(PNG)
    chat = await _open(pages)
    await until(
        chat, "[...document.querySelectorAll('img.emoji')].every(i => i.complete && i.naturalWidth > 0)"
    )
    assert await chat.evaluate(CHIPS) == [["🤘", " 1", "🤘🤘"], [None, "🤘 1", "🤘🤘"]]
    assert await chat.evaluate(BODY) == [["🤘"], "hi  👍"]
    assert fetched == []  # served from the cache
    # A quote's line, the text a copy takes, reads the art as its character.
    await chat.locator(".msg").hover()
    await chat.locator('.msg [data-act="reply"]').click()
    assert await chat.locator("#quotes .quote span").inner_text() == "hi 🤘 👍"
    assert await chat.locator(".msg .html img.emoji").count() == 1  # the art is back in place


async def test_failed_art_falls_back_on_the_character(served: tuple[Pages, list[str]]) -> None:
    """042-AC4: the CDN fails, the chip and the body show 🤘 instead of a broken image."""
    pages, fetched = served
    chat = await _open(pages)
    await until(chat, "document.querySelectorAll('img.emoji').length === 0")
    assert await chat.evaluate(CHIPS) == [[None, "🤘 1", "🤘🤘"], [None, "🤘 1", "🤘🤘"]]
    assert await chat.evaluate(BODY) == [[], "hi 🤘 👍"]
    assert fetched == ["rock"]  # both images asked once: the failure is remembered
