"""Editing like composing, on the server (spec 019 phase 1): the `editable` verb and the rebuilt `edit`."""

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from test_web_chat import ANN, ME, _archive, _at, _chat_socket, _m, _Recorder, _send, _served

from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.archive_store import find_chat_dir
from miniteams.compose import compose_html, reply_quote
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.images import Image, images_html

G = "19:edit0000@thread.v2"  # on the board and archived
GONE = "8:orgid:00000000-0000-0000-0000-00000000000f"  # mentioned in message 10, has left the chat since
OID = "0-weu-d1-feedc0ffee"
EMOJIS = {"fiesta": OID}
IMG_A, IMG_B = "0-weu-d1-aaaa", "0-weu-d1-bbbb"  # A is archived, B is not
MINE = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Self"}
GONE_PICK = {"name": "Gone Person", "mri": GONE}
ANN_PICK = {"name": "Ann Lee", "mri": ANN}
TEXT_10 = "**hi** @Gone Person :fiesta:"
HOST = "gw.test"  # the host message 10's images were posted with


def _quoted(msg_id: str, name: str) -> str:
    return reply_quote({"id": msg_id, "messagetype": "RichText/Html", "content": f"<p>q{msg_id}</p>"}, name)


def _image(object_id: str) -> str:
    return images_html(HOST, [(object_id, Image("png", 3, 2))]).removeprefix("<p>").removesuffix("</p>")


def _message_10() -> dict[str, Any]:
    """Mine: two Reply blocks, text with a mention of someone gone and a custom emoji, two images."""
    body, props = compose_html(TEXT_10, [GONE_PICK], EMOJIS, full=True)
    assert props is not None
    content = (
        _quoted("1", "Ann Lee") + _quoted("3", "Bo") + f"<p>{body}</p><p>{_image(IMG_A)}{_image(IMG_B)}</p>"
    )
    return _m(
        "10",
        _at(10),
        content,
        version="1789466500001",
        properties={**props, "edittime": "1789466500000"},
        **MINE,
    )


@pytest.fixture
def edit_board(directory: Directory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> W.Board:
    """G archived: my message 10 (above), my table 11, my plain 12, my deleted 13, Ann's 14, my tag
    mention 15; image A archived. The roster is me and Ann."""

    async def roster(self: Directory, thread_id: str, max_age: float | None = None) -> dict[str, Any] | None:
        return {"topic": None, "members": [{"mri": ME, "name": "Me Self"}, {"mri": ANN, "name": "Ann Lee"}]}

    monkeypatch.setattr(Directory, "thread", roster)
    tag = '<span itemtype="http://schema.skype.com/Mention" itemscope itemid="0">Ops</span>'
    data = _archive(
        tmp_path / "edit",
        G,
        [
            _message_10(),
            _m("11", _at(11), "<table><tr><td>x</td></tr></table>", **MINE),
            _m("12", _at(12), "<p>old</p>", **MINE),
            _m("13", _at(13), "", **{**MINE, "properties": {"deletetime": "1789466600000"}}),
            _m("14", _at(14), "<p>Ann's</p>"),
            _m(
                "15",
                _at(15),
                tag,
                **{**MINE, "properties": {"mentions": '[{"itemid": 0, "mri": "t", "mentionType": "tag"}]'}},
            ),
        ],
    )
    (find_chat_dir(data, G) / "media" / f"{IMG_A}.png").write_bytes(b"x")
    row = {"id": G, "label": "G", "last_activity": _at(1), "last_id": "1", "seen_at": None, "typing": []}
    board = W.Board({G: row}, directory, me=ME, data_dir=data)
    board.emojis.names = dict(EMOJIS)
    return board


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every edit and send posted (API mocked); the API history read finds nothing."""
    made: list[tuple[Any, ...]] = []

    def fake_edit(s: Any, token: str, thread_id: str, msg: str, text: str, **kw: Any) -> dict[str, Any]:
        made.append(("edit", msg, text, kw))
        return {"status": 200}

    def fake_send(s: Any, token: str, thread_id: str, text: str, name: str, **kw: Any) -> dict[str, Any]:
        made.append(("send", text, kw))
        return {"status": 201}

    monkeypatch.setattr(CV, "edit_message", fake_edit)
    monkeypatch.setattr(CV, "send_message", fake_send)
    monkeypatch.setattr(CV, "fetch_history", lambda *a, **kw: [])
    return made


async def _editable(board: W.Board, msg: Any) -> dict[str, Any]:
    return await board.chat.editable_answer(None, {"editable": G, "msg": msg})


async def _edit(board: W.Board, msg: str, text: Any, **extra: Any) -> dict[str, Any]:
    return await board.chat.act_answer(None, "edit", {"edit": G, "msg": msg, "text": text, **extra})


def _composed(text: str, picks: list[dict[str, Any]], board: W.Board) -> tuple[str, dict[str, Any] | None]:
    host = board.chat.directory.settings.asyncgw_host  # type: ignore[union-attr]
    return compose_html(text, picks, EMOJIS, full=True, asyncgw_host=host)


# --- opening an edit ---


async def test_editable_answers_the_messages_editable_form(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC4 + 019-AC6 + 019-AC7 + 019-AC8 (server half): markdown, picks, quotes and images (archived
    on /media, else the AMS URL) by their stable keys, the edit signal; no HTML of the message."""
    answer = await _editable(edit_board, "10")
    assert answer == {
        "editable": G,
        "msg": "10",
        "ok": True,
        "text": TEXT_10,
        "picks": [GONE_PICK],
        "quotes": [{"key": "1", "name": "Ann Lee", "line": "q1"}, {"key": "3", "name": "Bo", "line": "q3"}],
        "images": [
            {"key": IMG_A, "src": f"/media/{quote(G, safe='')}/{IMG_A}.png", "alt": "image"},
            {"key": IMG_B, "src": f"https://{HOST}/v1/objects/{IMG_B}/views/imgo", "alt": "image"},
        ],
        "edittime": "1789466500000",
    }
    assert writes == []


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        ("11", {"blocked": "a table"}),
        ("15", {"blocked": "a tag or team mention"}),
        ("14", {"error": "not your message"}),
        ("13", {"error": "message deleted"}),
        ("99", {"error": "unknown message"}),
        ("1x", {"error": "invalid message"}),
        (None, {"error": "invalid message"}),
    ],
)
async def test_editable_refusals(
    edit_board: W.Board, writes: list[Any], msg: Any, expected: dict[str, Any]
) -> None:
    """019-AC9: a message compose cannot write says what blocks it; not mine, deleted, unknown: refused."""
    assert await _editable(edit_board, msg) == {"editable": G, "msg": msg, **expected}
    assert writes == []


async def test_editable_off_scope_is_an_unknown_chat(edit_board: W.Board, writes: list[Any]) -> None:
    answer = await edit_board.chat.editable_answer(None, {"editable": "19:zzzz@thread.v2", "msg": "10"})
    assert answer["error"] == "unknown chat"


async def test_editable_over_the_socket(edit_board: W.Board, settings: Settings, writes: list[Any]) -> None:
    """019-AC4: the page verb is wired: `{editable, msg}` answers the asking socket."""
    async with _served(edit_board, settings), _chat_socket(edit_board) as chat:
        await chat.send(json.dumps({"editable": G, "msg": "12"}))
        answer = json.loads(await asyncio.wait_for(chat.recv(), 2))
    assert (answer["editable"], answer["msg"], answer["text"], answer["quotes"]) == (G, "12", "old", [])


# --- saving an edit ---


async def test_edit_rebuilds_from_the_copy_kept_quotes_text_kept_images(
    edit_board: W.Board, writes: list[Any]
) -> None:
    """019-AC3 + 019-AC6 + 019-AC7 + 019-AC8: kept Reply block and image verbatim from the copy, the text
    composed as a send, the mention of someone who left kept without the roster."""
    text = "**hi** @Gone Person :fiesta: again"
    answer = await _edit(edit_board, "10", text, mentions=[GONE_PICK], keep_quotes=["3"], keep_images=[IMG_A])
    assert answer == {"edit": G, "msg": "10", "ok": True}
    body, props = _composed(text, [GONE_PICK], edit_board)
    assert writes == [
        (
            "edit",
            "10",
            _quoted("3", "Bo") + f"<p>{body}</p><p>{_image(IMG_A)}</p>",
            {"is_html": True, "properties": props},
        )
    ]


async def test_edit_posts_what_a_send_of_the_same_text_posts(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC3: markdown rendered, shortcodes expanded, mentions as mentions: byte for byte a send's."""
    text = "**b** _i_ `c` :fiesta: @Ann Lee\n- x\n> q"
    assert (await _edit(edit_board, "12", text, mentions=[ANN_PICK], keep_quotes=[], keep_images=[]))["ok"]
    assert await _send(edit_board, G, text, None, None, [ANN_PICK], "full") is None
    [(_, _, edited, edit_kw), (_, sent, send_kw)] = writes
    assert edited == sent and edit_kw["properties"] == send_kw["properties"]
    assert "<b>b</b>" in edited and 'itemtype="http://schema.skype.com/Mention"' in edited


async def test_edit_with_images_only(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC7: kept images alone are a message; quotes alone are not."""
    assert (await _edit(edit_board, "10", " ", keep_quotes=["1"], keep_images=[IMG_B]))["ok"]
    assert writes[0][2] == _quoted("1", "Ann Lee") + f"<p>{_image(IMG_B)}</p>"
    assert (await _edit(edit_board, "10", " ", keep_quotes=["1"], keep_images=[]))["error"] == "empty message"


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ({"keep_quotes": ["2"], "keep_images": []}, CV.CHANGED),
        ({"keep_quotes": [], "keep_images": ["0-weu-d1-cccc"]}, CV.CHANGED),
        ({"keep_quotes": ["1", "1"], "keep_images": []}, "invalid kept quote"),
        ({"keep_quotes": [], "keep_images": [IMG_A, IMG_A]}, "invalid kept image"),
        ({"keep_quotes": [0], "keep_images": []}, "invalid kept quote"),
        ({"keep_quotes": ["1x"], "keep_images": []}, "invalid kept quote"),
        ({"keep_quotes": [], "keep_images": ["../x"]}, "invalid kept image"),
        ({"keep_quotes": "1", "keep_images": []}, "invalid kept quote"),
        ({"keep_quotes": []}, "invalid kept image"),
        ({"mentions": []}, "invalid kept quote"),
        ({"keep_quotes": [], "keep_images": [], "mentions": [{"name": "x"}]}, "invalid mention"),
    ],
)
async def test_edit_keys_and_shapes_are_checked(
    edit_board: W.Board, writes: list[Any], extra: dict[str, Any], error: str
) -> None:
    """019-AC6 + 019-AC7: kept quotes and images name, each once, ones the copy holds."""
    assert (await _edit(edit_board, "10", "x", **extra))["error"] == error
    assert writes == []


async def test_mentions_new_ones_go_through_the_roster(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC8: a new mention of a member is posted, of a non-member refused; a mention not in the copy
    but presented as kept (another person, or the gone one under another name) is new, so refused."""
    keep = {"keep_quotes": [], "keep_images": []}
    for forged in (
        {"name": "Gone Person", "mri": "8:orgid:00000000-0000-0000-0000-0000000000ff"},
        {"name": "Boss", "mri": GONE},
        {"name": "everyone", "mri": GONE, "everyone": True},
    ):
        text = f"hi @{forged['name']}"
        assert (await _edit(edit_board, "10", text, mentions=[forged], **keep))["error"] == "unknown mention"
    assert writes == []
    answer = await _edit(edit_board, "10", "@Gone Person @Ann Lee", mentions=[GONE_PICK, ANN_PICK], **keep)
    assert answer["ok"]
    entries = json.loads(writes[0][3]["properties"]["mentions"])
    assert [(e["mri"], e["displayName"]) for e in entries] == [(GONE, "Gone Person"), (ANN, "Ann Lee")]


async def test_a_kept_mention_shows_the_copys_label(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC8: a kept mention cannot bring a label of the page's choosing."""
    pick = {**GONE_PICK, "label": "Someone Else"}
    assert (await _edit(edit_board, "10", "@Gone Person", mentions=[pick], keep_quotes=[], keep_images=[]))[
        "ok"
    ]
    assert ">Gone Person</span>" in writes[0][2] and "Someone Else" not in json.dumps(writes)


async def test_edit_of_a_message_compose_cannot_write_is_refused(
    edit_board: W.Board, writes: list[Any]
) -> None:
    """019-AC9: the server refuses on its own, whatever the page sends: an old page's edit included."""
    for msg, reason in (("11", "a table"), ("15", "a tag or team mention")):
        assert (await _edit(edit_board, msg, "x", keep_quotes=[], keep_images=[]))["error"] == (
            f"cannot edit here: {reason}"
        )
        assert (await _edit(edit_board, msg, "x"))["error"] == f"cannot edit here: {reason}"
    assert writes == []


async def test_old_page_edit_is_unchanged(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC3: an old page's `{edit, msg, text}` still posts its text as typed, escaped, no markdown."""
    assert (await _edit(edit_board, "10", "fixed **x** <b>"))["ok"]
    assert writes == [("edit", "10", "fixed **x** <b>", {})]


async def test_preview_reads_escapes(edit_board: W.Board) -> None:
    """019-AC5: in the composer, `\\*` previews (and posts) a literal `*`."""
    assert edit_board.chat.preview_answer({"preview": "\\*x\\* *y*", "format": "full"}) == {
        "preview": "*x* <i>y</i>"
    }


# --- edits made elsewhere, busy slots, failure paths ---


def _edited_elsewhere(board: W.Board, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Message 10 as Teams holds it after an edit made elsewhere since the editor opened."""
    changed = {**_message_10(), "content": content}

    async def copies(thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        return {"10": changed}

    monkeypatch.setattr(board.chat, "_server_copies", copies)


async def test_kept_items_are_named_by_key_across_an_edit_elsewhere(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """019-AC6 + 019-AC7 + 019-AC10: opened with quotes 1, 3 and images A, B, the user keeps 3 and A; an
    edit elsewhere drops quote 3 and swaps the images: the save is refused, never keeps another one."""
    opened = await _editable(edit_board, "10")
    keep = {"keep_quotes": [opened["quotes"][1]["key"]], "keep_images": [opened["images"][0]["key"]]}
    body, _ = compose_html(TEXT_10, [GONE_PICK], EMOJIS, full=True)
    _edited_elsewhere(
        edit_board,
        monkeypatch,
        _quoted("1", "Ann Lee") + f"<p>{body}</p><p>{_image(IMG_B)}{_image(IMG_A)}</p>",
    )
    assert (await _edit(edit_board, "10", "x", **keep))["error"] == CV.CHANGED
    assert writes == []
    # Still there, wherever they moved: kept, and only they.
    _edited_elsewhere(
        edit_board, monkeypatch, _quoted("3", "Bo") + f"<p>{body}</p><p>{_image(IMG_B)}{_image(IMG_A)}</p>"
    )
    assert (await _edit(edit_board, "10", "x", **keep))["ok"]
    assert writes[0][2] == _quoted("3", "Bo") + f"<p>x</p><p>{_image(IMG_A)}</p>"


async def test_opening_an_editor_has_its_own_busy_slot(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC4: a pending reaction, delete or send never makes opening an editor answer `busy`."""

    async def opened(ws: _Recorder) -> dict[str, Any]:
        await edit_board.chat._on_editable(ws, {"editable": G, "msg": "12"})  # type: ignore[arg-type]
        for _ in range(200):
            if ws.frames:
                return ws.frames[0]
            await asyncio.sleep(0.01)
        raise AssertionError("no answer")

    ws = _Recorder()
    edit_board.chat._action_busy.add(ws)  # type: ignore[arg-type]
    assert (await opened(ws))["ok"]
    other = _Recorder()
    edit_board.chat._editable_busy.add(other)  # type: ignore[arg-type]
    assert (await opened(other))["error"] == "busy"


def test_records_carry_the_edit_signal() -> None:
    """019-AC10: a record says when its message was last edited ("" when never), not its version."""
    assert CV.chat_record(_message_10(), G)["edittime"] == "1789466500000"
    assert CV.chat_record(_m("2", _at(2)), G)["edittime"] == ""


async def test_edit_failure_paths(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """019-AC3 + 019-AC8: no roster for a new mention, a compose refusal, a message too long once its
    kept quotes and images are added back: each refused, nothing posted."""
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, "10", "x", mentions=[GONE_PICK] * 51, **keep))["error"] == (
        "too many mentions (51 > 50)"
    )
    long = "y" * 28_500
    assert (await _edit(edit_board, "10", long, **keep))["ok"]
    assert (await _edit(edit_board, "10", long, keep_quotes=["1", "3"], keep_images=[IMG_A]))["error"] == (
        "message too long"
    )

    async def no_roster(self: Directory, thread_id: str, max_age: float | None = None) -> None:
        return None

    monkeypatch.setattr(Directory, "thread", no_roster)
    assert (await _edit(edit_board, "10", "@Ann Lee", mentions=[ANN_PICK], **keep))["error"] == (
        "roster unavailable"
    )
    assert [w[0] for w in writes] == ["edit"]  # the long one alone


async def test_editable_reports_a_failed_lookup(edit_board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """019-AC4: the copy cannot be read: the page learns why, the socket stays."""

    async def broken(thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(edit_board.chat, "_server_copies", broken)
    assert (await _editable(edit_board, "10"))["error"] == "network error"
