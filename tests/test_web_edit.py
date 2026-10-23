"""Editing like composing, on the server (spec 019 phase 1): the `editable` verb and the rebuilt `edit`."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from test_web_chat import ANN, ME, _archive, _at, _chat_socket, _m, _Recorder, _send, _served

from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.archive_store import find_chat_dir
from miniteams.compose import compose_html, mention_ats, reply_quote
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
GONE_KEPT = {"name": "{Gone Person|Gone Person}", "mri": GONE}  # as the editor gives it back (spec 033)
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


def _message_16() -> dict[str, Any]:
    """Mine: Ann mentioned as `Lee`, then as `the boss`, as the Teams client may write them."""
    picks = [{"name": "Lee", "mri": ANN, "label": "Lee"}, {"name": "boss", "mri": ANN, "label": "the boss"}]
    body, props = compose_html("hi @Lee and @boss", picks, {}, full=True)
    return _m("16", _at(16), f"<p>{body}</p>", properties=props, **MINE)


def _message_17() -> dict[str, Any]:
    """Mine: plain `@Ann`, `@home` and `@{x}`, and Ann mentioned as `boss`."""
    picks = [{"name": "y", "mri": ANN, "label": "boss"}]
    body, props = compose_html("\\@Ann, \\@home, \\@{x} and @y", picks, {}, full=True)
    return _m("17", _at(17), f"<p>{body}</p>", properties=props, **MINE)


def _message_18() -> dict[str, Any]:
    """Mine: someone gone since, mentioned under labels holding an `@word`."""
    picks = [{"name": "x", "mri": GONE, "label": "@boss"}, {"name": "y", "mri": GONE, "label": "ask @Ann"}]
    body, props = compose_html("hi @x and @y", picks, {}, full=True)
    return _m("18", _at(18), f"<p>{body}</p>", properties=props, **MINE)


MOORE = "8:orgid:00000000-0000-0000-0000-0000000000b2"


def _message_19() -> dict[str, Any]:
    """Mine: Ann Moore mentioned as `Ann`, followed by ` Lee` (Ann Lee is in the chat too)."""
    body, props = compose_html("@x Lee", [{"name": "x", "mri": MOORE, "label": "Ann"}], {}, full=True)
    return _m("19", _at(19), f"<p>{body}</p>", properties=props, **MINE)


XAV = "8:orgid:00000000-0000-0000-0000-0000000000aa"
OTHER = "8:orgid:00000000-0000-0000-0000-0000000000bb"
# Xavier Doe mentioned under `label`, then `after`: once the roster is back, `other` reads longer.
XAVIER_SHAPES = {
    "20": ("Doe", ", John said", "Doe, John"),
    "21": ("Ann", "/Lee", "Ann/Lee"),
    "22": ("Ann", " (Sales)", "Ann (Sales)"),
    "23": ("Ann", "\u00a0Lee", "Ann\u00a0Lee"),
    "24": ("Ann", "-Lee", "Ann-Lee"),
}


# Xavier mentioned twice under `label`: the first one is followed by what makes `other` read longer.
XAVIER_TWICE = {"25": ("Doe", ", John said and ", "Doe, John"), "26": ("Ann", "/Lee and ", "Ann/Lee")}


def _labelled_once(msg_id: str, mri: str, label: str) -> dict[str, Any]:
    """Mine: `hi @label`, one mention of `mri`."""
    body, props = compose_html("hi @x", [{"name": "x", "mri": mri, "label": label}], {}, full=True)
    return _m(msg_id, _at(int(msg_id)), f"<p>{body}</p>", properties=props, **MINE)


def _message_27() -> dict[str, Any]:
    """Mine: namesakes both labelled `Ann`, Ann Lee then Ann Moore."""
    picks = [{"name": "x", "mri": ANN, "label": "Ann"}, {"name": "y", "mri": MOORE, "label": "Ann"}]
    body, props = compose_html("@x and @y", picks, {}, full=True)
    return _m("27", _at(27), f"<p>{body}</p>", properties=props, **MINE)


def _xavier_twice(msg_id: str, label: str, between: str) -> dict[str, Any]:
    picks = [{"name": "x", "mri": XAV, "label": label}]
    body, props = compose_html(f"@x{between}@x", picks, {}, full=True)
    return _m(msg_id, _at(int(msg_id)), f"<p>{body}</p>", properties=props, **MINE)


def _xavier(msg_id: str, label: str, after: str) -> dict[str, Any]:
    body, props = compose_html(f"@x{after}", [{"name": "x", "mri": XAV, "label": label}], {}, full=True)
    return _m(msg_id, _at(int(msg_id)), f"<p>{body}</p>", properties=props, **MINE)


@pytest.fixture
def edit_board(directory: Directory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> W.Board:
    """G archived: my message 10 (above), my table 11, my plain 12, my deleted 13, Ann's 14, my tag
    mention 15, my mentions of Ann as `Lee` and as `the boss` 16, my plain `@` text 17, labels with `@` 18,
    Ann Moore's `Ann` then ` Lee` 19, Xavier's shapes 20-26, namesakes both `Ann` 27, Ann Lee's `@Ann` 28,
    someone gone's `@Ann Lee` 29, Xavier's `@Xavier` then ` Jr said` 30; image A archived. The roster is me
    and Ann."""

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
            _message_16(),
            _message_17(),
            _message_18(),
            _message_19(),
            *(_xavier(msg, label, after) for msg, (label, after, _) in XAVIER_SHAPES.items()),
            *(_xavier_twice(msg, label, between) for msg, (label, between, _) in XAVIER_TWICE.items()),
            _message_27(),
            _labelled_once("28", ANN, "Ann"),
            _labelled_once("29", GONE, "Ann Lee"),
            _xavier("30", "Xavier", " Jr said"),
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


def _anchor(text: str, *picks: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's picks anchored at the `@`s the server reads in `text`, in order (spec 033)."""
    return [{**p, "at": at} for p, at in zip(picks, mention_ats(text), strict=True)]


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
        "text": "**hi** @{Gone Person|Gone Person} :fiesta:",
        "picks": [{**GONE_KEPT, "at": TEXT_10.index("@")}],
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
    text = "**hi** @{Gone Person|Gone Person} :fiesta: again"
    answer = await _edit(
        edit_board, "10", text, mentions=_anchor(text, GONE_KEPT), keep_quotes=["3"], keep_images=[IMG_A]
    )
    assert answer == {"edit": G, "msg": "10", "ok": True}
    body, props = _composed(text, [GONE_KEPT], edit_board)
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
    assert (
        await _edit(edit_board, "12", text, mentions=_anchor(text, ANN_PICK), keep_quotes=[], keep_images=[])
    )["ok"]
    assert await _send(edit_board, G, text, None, None, _anchor(text, ANN_PICK), "full") is None
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
        assert (await _edit(edit_board, "10", text, mentions=_anchor(text, forged), **keep))[
            "error"
        ] == "unknown mention"
    assert writes == []
    text = "@{Gone Person|Gone Person} @Ann Lee"
    answer = await _edit(edit_board, "10", text, mentions=_anchor(text, GONE_KEPT, ANN_PICK), **keep)
    assert answer["ok"]
    entries = json.loads(writes[0][3]["properties"]["mentions"])
    assert [(e["mri"], e["displayName"]) for e in entries] == [(GONE, "Gone Person"), (ANN, "Ann Lee")]


async def test_a_kept_mention_shows_the_copys_label(edit_board: W.Board, writes: list[Any]) -> None:
    """019-AC8: a kept mention cannot bring a label of the page's choosing."""
    pick = {**GONE_KEPT, "label": "Someone Else"}
    text = "@{Gone Person|Gone Person}"
    assert (
        await _edit(edit_board, "10", text, mentions=_anchor(text, pick), keep_quotes=[], keep_images=[])
    )["ok"]
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
    assert (await _edit(edit_board, "10", "x", mentions=[{**GONE_PICK, "at": 0}] * 51, **keep))["error"] == (
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
    assert (await _edit(edit_board, "10", "@Ann Lee", mentions=_anchor("@Ann Lee", ANN_PICK), **keep))[
        "error"
    ] == ("roster unavailable")
    assert [w[0] for w in writes] == ["edit"]  # the long one alone


async def test_editable_reports_a_failed_lookup(edit_board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """019-AC4: the copy cannot be read: the page learns why, the socket stays."""

    async def broken(thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(edit_board.chat, "_server_copies", broken)
    assert (await _editable(edit_board, "10"))["error"] == "network error"


# --- name forms and free labels (spec 033) ---


def _people(*people: tuple[str, str]) -> Any:
    async def roster(self: Directory, thread_id: str, max_age: float | None = None) -> dict[str, Any]:
        return {"topic": None, "members": [{"mri": mri, "name": name} for name, mri in people]}

    return roster


async def _no_roster(self: Directory, thread_id: str, max_age: float | None = None) -> None:
    return None


async def test_editable_braces_every_person_mention(edit_board: W.Board, writes: list[Any]) -> None:
    """033-AC8 (user decision): every kept person mention comes back braced with the full name, `Lee` as
    well as `the boss`: `@{Ann Lee|Lee}`, `@{Ann Lee|the boss}`."""
    answer = await _editable(edit_board, "16")
    assert answer["text"] == "hi @{Ann Lee|Lee} and @{Ann Lee|the boss}"
    assert answer["picks"] == _anchor(
        answer["text"], {"name": "{Ann Lee|Lee}", "mri": ANN}, {"name": "{Ann Lee|the boss}", "mri": ANN}
    )


async def test_resending_an_opened_edit_unchanged_keeps_its_mentions(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033-AC8: the text and picks the editor opened, sent back, give the same span labels and entries.
    `{Ann Lee|…}`, opened with the roster, cannot be checked once the roster is gone: refused, nothing
    written."""
    opened = await _editable(edit_board, "16")
    old = json.loads(_message_16()["properties"]["mentions"])
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, "16", opened["text"], mentions=opened["picks"], **keep))["ok"]
    monkeypatch.setattr(Directory, "thread", _no_roster)
    answer = await _edit(edit_board, "16", opened["text"], mentions=opened["picks"], **keep)
    assert answer["error"] == "roster unavailable" and len(writes) == 1
    [(_, _, content, kw)] = writes
    assert content == _message_16()["content"].removeprefix("<p>").removesuffix("</p>")
    assert json.loads(kw["properties"]["mentions"]) == old


async def test_editable_without_a_roster_braces_each_label_on_itself(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033-AC8 (user decision): no roster, so no full name: each kept mention comes back as
    `@{label|label}`, the pick keeping her MRI."""
    monkeypatch.setattr(Directory, "thread", _no_roster)
    answer = await _editable(edit_board, "16")
    assert answer["text"] == "hi @{Lee|Lee} and @{the boss|the boss}"
    assert answer["picks"] == _anchor(
        answer["text"], {"name": "{Lee|Lee}", "mri": ANN}, {"name": "{the boss|the boss}", "mri": ANN}
    )


async def test_an_edit_resolves_an_unpicked_form(edit_board: W.Board, writes: list[Any]) -> None:
    """033-AC8 + 033-AC6 (server half): a new `@lee` typed in the editor, unpicked, is Ann's."""
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, "12", "now @lee", mentions=[], **keep))["ok"]
    entries = json.loads(writes[0][3]["properties"]["mentions"])
    assert [(e["mri"], e["displayName"]) for e in entries] == [(ANN, "Lee")]
    assert (await _edit(edit_board, "12", "now @Nobody", mentions=[], **keep))["error"] == (
        "unknown mention @Nobody (\\@ for a literal @)"
    )


async def test_a_free_label_of_someone_gone_since_is_refused(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review: opened as `@{Ann Lee|the boss}`, saved after Ann left the chat: Who no longer names a
    member, so the save is refused, nothing written (re-opening gives `@{the boss|the boss}`, which saves)."""
    opened = await _editable(edit_board, "16")
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME)))
    keep = {"keep_quotes": [], "keep_images": []}
    answer = await _edit(edit_board, "16", opened["text"], mentions=opened["picks"], **keep)
    assert answer["error"] == "unknown mention" and writes == []
    reopened = await _editable(edit_board, "16")
    assert reopened["text"] == "hi @{Lee|Lee} and @{the boss|the boss}"
    assert (await _edit(edit_board, "16", reopened["text"], mentions=reopened["picks"], **keep))["ok"]
    entries = json.loads(writes[0][3]["properties"]["mentions"])
    assert [(e["mri"], e["displayName"]) for e in entries] == [(ANN, "Lee"), (ANN, "the boss")]


async def test_plain_at_text_stays_text_across_an_edit(edit_board: W.Board, writes: list[Any]) -> None:
    """033 review: an old message's plain `@Ann`, `@home`, `@{x}` come back escaped and the kept mention
    braced, so re-sending it unchanged adds no mention and refuses nothing."""
    opened = await _editable(edit_board, "17")
    assert opened["text"] == "\\@Ann, \\@home, \\@{x} and @{Ann Lee|boss}"
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, "17", opened["text"], mentions=opened["picks"], **keep))["ok"]
    [(_, _, content, kw)] = writes
    assert f"<p>{content}</p>" == _message_17()["content"]
    assert kw["properties"]["mentions"] == _message_17()["properties"]["mentions"]


@pytest.mark.parametrize(
    ("msg", "text", "picks"),
    [("10", "@{Gone Person|Gone Person} ok", [{**GONE_KEPT, "at": 0}]), ("16", None, None)],
)
async def test_kept_mentions_alone_need_no_roster(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, msg: str, text: Any, picks: Any
) -> None:
    """033: without a roster, an edit whose every `@` is a kept pick as typed is saved; one more `@form`
    needs the roster."""

    async def no_roster(self: Directory, thread_id: str, max_age: float | None = None) -> None:
        return None

    monkeypatch.setattr(Directory, "thread", no_roster)
    opened = await _editable(edit_board, msg)
    text, picks = text or opened["text"], picks or opened["picks"]
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, msg, text, mentions=picks, **keep))["ok"]
    assert (await _edit(edit_board, msg, text + " @Ann", mentions=picks, **keep))[
        "error"
    ] == "roster unavailable"


@pytest.mark.parametrize("roster", [True, False])
async def test_a_kept_label_holding_an_at_re_saves_unchanged(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, roster: bool
) -> None:
    """033 review: `@boss` and `ask @Ann` (someone gone, labels as Teams wrote them) come back braced on
    themselves and re-save unchanged, the inner `@` being the kept pick's, with the roster or without it."""
    if not roster:
        monkeypatch.setattr(Directory, "thread", _no_roster)
    opened = await _editable(edit_board, "18")
    assert opened["text"] == "hi @{@boss|@boss} and @{ask @Ann|ask @Ann}"
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, "18", opened["text"], mentions=opened["picks"], **keep))["ok"]
    [(_, _, content, kw)] = writes
    assert f"<p>{content}</p>" == _message_18()["content"]
    assert kw["properties"]["mentions"] == _message_18()["properties"]["mentions"]


@pytest.mark.parametrize("name", ["{x} @Ann Lee|the boss}", "{a}b|the boss}", "{x}|the boss}"])
async def test_a_kept_pick_takes_no_malformed_name_from_the_page(
    edit_board: W.Board, writes: list[Any], name: str
) -> None:
    """033 review: a page name showing the kept label but no well-formed `{Who|label}` is a new pick; the
    text at its `@` is no `{Who|label}` either, so the save is refused with the reader's reason."""
    keep = {"keep_quotes": [], "keep_images": []}
    text = f"hi @Lee and @{name}"
    picks = [{"name": "Lee", "mri": ANN, "at": 3}, {"name": name, "mri": ANN, "at": 12}]
    answer = await _edit(edit_board, "16", text, mentions=picks, **keep)
    assert answer["error"].startswith("mention @{") and writes == []


async def test_an_unchanged_edit_keeps_the_recipient_a_longer_name_would_take(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review: `Ann` (Ann Moore) then ` Lee`, Ann Lee in the chat: the editor opens
    `@{Ann Moore|Ann} Lee`, and an unchanged save mentions Ann Moore, not Ann Lee."""

    async def roster(self: Directory, thread_id: str, max_age: float | None = None) -> dict[str, Any]:
        people = [("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)]
        return {"topic": None, "members": [{"mri": mri, "name": name} for name, mri in people]}

    monkeypatch.setattr(Directory, "thread", roster)
    opened = await _editable(edit_board, "19")
    assert opened["text"] == "@{Ann Moore|Ann} Lee"
    keep = {"keep_quotes": [], "keep_images": []}
    assert (await _edit(edit_board, "19", opened["text"], mentions=opened["picks"], **keep))["ok"]
    [(_, _, content, kw)] = writes
    assert f"<p>{content}</p>" == _message_19()["content"]
    assert json.loads(kw["properties"]["mentions"]) == json.loads(_message_19()["properties"]["mentions"])


@pytest.mark.parametrize(
    "name",
    [
        "{Ann Moore|the boss}",
        "{totally @Bob|the boss}",
        "{Bob Moore|the boss}",
        "{totally @Bob @Ann Moore|the boss}",
    ],
)
async def test_a_kept_pick_renamed_is_checked_as_new(
    edit_board: W.Board, writes: list[Any], name: str
) -> None:
    """033 review: the copy's `{Ann Lee|the boss}` renamed by the page, even to a well-formed `{Who|label}`,
    is a new pick, read at its `@` by its own member's forms: a Who naming nobody else in the chat is
    refused, nothing written."""
    keep = {"keep_quotes": [], "keep_images": []}
    text = f"hi @Lee and @{name}"
    picks = [{"name": "Lee", "mri": ANN, "at": 3}, {"name": name, "mri": ANN, "at": 12}]
    answer = await _edit(edit_board, "16", text, mentions=picks, **keep)
    assert answer["error"].startswith("unknown mention @{") and writes == []


@pytest.mark.parametrize(
    ("msg", "message"),
    [("10", _message_10), ("16", _message_16), ("17", _message_17), ("18", _message_18), ("19", _message_19)],
)
async def test_opened_without_a_roster_and_saved_with_it_the_message_is_unchanged(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, msg: str, message: Any
) -> None:
    """033 review: the editor opened while the roster was down (`@{Lee|Lee}`, `@{the boss|the boss}`,
    `@{Ann|Ann} Lee`) and saved once it is back: each kept mention is recognised under its spellings, so
    the message saves unchanged, same people, same HTML."""

    async def no_roster(self: Directory, thread_id: str, max_age: float | None = None) -> None:
        return None

    async def roster(self: Directory, thread_id: str, max_age: float | None = None) -> dict[str, Any]:
        people = [("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)]
        return {"topic": None, "members": [{"mri": mri, "name": name} for name, mri in people]}

    monkeypatch.setattr(Directory, "thread", no_roster)
    opened = await _editable(edit_board, msg)
    monkeypatch.setattr(Directory, "thread", roster)
    keep = {
        "keep_quotes": [q["key"] for q in opened["quotes"]],
        "keep_images": [i["key"] for i in opened["images"]],
    }
    answer = await _edit(edit_board, msg, opened["text"], mentions=opened["picks"], **keep)
    assert answer.get("ok"), (opened["text"], answer)
    [(_, _, content, kw)] = writes
    original = message()
    if msg == "10":  # its quotes and images are put back around the text paragraph
        assert original["content"].split("<p>")[-2].removesuffix("</p>") in content
    else:
        assert f"<p>{content}</p>" == original["content"]
    assert json.loads(kw["properties"]["mentions"]) == json.loads(original["properties"]["mentions"])


@pytest.mark.parametrize(
    ("kept", "spellings"),
    [
        ({"name": "{Ann Lee|Lee}", "mri": ANN}, {"{Lee|Lee}", "{Ann Lee|Lee}"}),
        ({"name": "{Ann Lee|the boss}", "mri": ANN}, {"{the boss|the boss}", "{Ann Lee|the boss}"}),
        ({"name": "{Lee|Lee}", "mri": ANN}, {"{Lee|Lee}", "{Ann Lee|Lee}"}),
        ({"name": "{the boss|the boss}", "mri": GONE}, {"{the boss|the boss}"}),  # no name known
        ({"name": "{Ann Lee|{x}", "mri": ANN}, {"{{x|{x}", "{Ann Lee|{x}"}),  # a Teams label opening a brace
        ({"name": "everyone", "mri": G, "everyone": True}, {"everyone"}),
    ],
)
def test_a_kept_mention_is_known_under_its_spellings_only(kept: dict[str, Any], spellings: set[str]) -> None:
    """033 review: as the copy spells it, `{label|label}` or `{Full Name|label}` with the roster's name for
    that MRI, whichever the roster gave at opening and at saving; never the bare label, never another Who."""
    assert CV._spellings(kept, {ANN: "Ann Lee", MOORE: "Ann Moore"}) == spellings


@pytest.mark.parametrize("which", [0, 1])
async def test_a_kept_mention_given_another_mri_is_new(
    edit_board: W.Board, writes: list[Any], which: int
) -> None:
    """033 review: the copy's spelling sent under another person's MRI is a new pick, checked on the roster
    (Who names Ann, not Ann Moore): refused, nothing written."""
    text = "hi @{Ann Lee|Lee} and @{Ann Lee|the boss}"
    picks = _anchor(text, {"name": "{Ann Lee|Lee}", "mri": ANN}, {"name": "{Ann Lee|the boss}", "mri": ANN})
    picks[which] = {**picks[which], "mri": MOORE}
    answer = await _edit(edit_board, "16", text, mentions=picks, keep_quotes=[], keep_images=[])
    assert answer["error"] == "unknown mention" and writes == []


@pytest.mark.parametrize("msg", list(XAVIER_SHAPES))
async def test_an_edit_never_moves_a_kept_mention_to_someone_else(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, msg: str
) -> None:
    """033 review: opened without a roster (no name for Xavier), saved once it is back with a member whose
    name goes on past the label (`Doe, John`, `Ann/Lee`, `Ann (Sales)`…): the save keeps Xavier or is
    refused, never a silent mention of the other member."""
    label, after, other = XAVIER_SHAPES[msg]

    async def no_roster(self: Directory, thread_id: str, max_age: float | None = None) -> None:
        return None

    async def roster(self: Directory, thread_id: str, max_age: float | None = None) -> dict[str, Any]:
        people = [("Me Self", ME), ("Xavier Doe", XAV), (other, OTHER)]
        return {"topic": None, "members": [{"mri": mri, "name": name} for name, mri in people]}

    monkeypatch.setattr(Directory, "thread", no_roster)
    opened = await _editable(edit_board, msg)
    monkeypatch.setattr(Directory, "thread", roster)
    keep = {"keep_quotes": [], "keep_images": []}
    answer = await _edit(edit_board, msg, opened["text"], mentions=opened["picks"], **keep)
    if "error" in answer:
        assert answer["error"] == f"mention @{label} now needs its full name: reopen the message"
        assert writes == []
    else:
        [(_, _, _, kw)] = writes
        assert [e["mri"] for e in json.loads(kw["properties"]["mentions"])] == [XAV]


@pytest.mark.parametrize("open_with", ["no roster", "Xavier only"])
async def test_a_longer_pick_or_name_never_takes_a_kept_mention(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, open_with: str
) -> None:
    """033 review regression: Xavier's `@Doe, John said` saved once `Doe, John` is in the chat, untouched or
    with ` cc @Doe, John` picked: braced, Xavier's mention stays his, John gets only the one picked."""
    xavier, john = ("Xavier Doe", XAV), ("Doe, John", OTHER)
    monkeypatch.setattr(
        Directory, "thread", _no_roster if open_with == "no roster" else _people(("Me Self", ME), xavier)
    )
    opened = await _editable(edit_board, "20")
    assert opened["text"].endswith("|Doe}, John said")
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), xavier, john))
    keep = {"keep_quotes": [], "keep_images": []}
    cases = (
        (
            opened["text"] + " cc @Doe, John",
            [*opened["picks"], {"name": "Doe, John", "mri": OTHER, "at": len(opened["text"]) + 4}],
            [XAV, OTHER],
        ),
        (opened["text"], opened["picks"], [XAV]),
    )
    for text, picks, mris in cases:
        writes.clear()
        assert (await _edit(edit_board, "20", text, mentions=picks, **keep)).get("ok"), text
        assert _sent(writes) == mris, text


async def test_a_new_longer_mention_beside_a_kept_one_saves(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review: Ann's `@Lee` kept, ` cc @Lee Bob` typed unpicked: Ann keeps her mention, Lee Bob gets
    his."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Lee Bob", MOORE)))
    opened = await _editable(edit_board, "16")
    keep = {"keep_quotes": [], "keep_images": []}
    answer = await _edit(edit_board, "16", opened["text"] + " cc @Lee Bob", mentions=opened["picks"], **keep)
    assert answer.get("ok"), answer
    entries = json.loads(writes[0][3]["properties"]["mentions"])
    assert [(e["mri"], e["displayName"]) for e in entries] == [
        (ANN, "Lee"),
        (ANN, "the boss"),
        (MOORE, "Lee Bob"),
    ]


@pytest.mark.parametrize("which", [0, 1])
async def test_deleting_a_kept_mention_is_no_refusal(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, which: int
) -> None:
    """033 review: the page sends only the picks still in the text: one kept mention deleted, the other
    saves."""
    opened = await _editable(edit_board, "16")
    gone = opened["picks"][which]["name"]
    text = opened["text"].replace(f"@{gone}", "someone")
    picks = [p for n, p in enumerate(opened["picks"]) if n != which]
    answer = await _edit(edit_board, "16", text, mentions=picks, keep_quotes=[], keep_images=[])
    assert answer.get("ok"), answer
    assert [e["mri"] for e in json.loads(writes[0][3]["properties"]["mentions"])] == [ANN]


@pytest.mark.parametrize("n", [5_000, 10_000])
def test_matching_kept_mentions_stays_cheap_on_a_large_roster(n: int) -> None:
    """033 review: 50 page mentions against 50 kept ones and a large roster: spellings are worked out once
    per kept pick, never per pair over the roster."""
    import time

    names = {f"8:orgid:{k}": f"Person{k} Name{k}" for k in range(n)}
    kept = [{"name": f"Name{k}", "mri": f"8:orgid:{k}"} for k in range(50)]
    page = tuple({"name": f"{{Person{k} Name{k}|Name{k}}}", "mri": f"8:orgid:{k}"} for k in range(50))
    start = time.process_time()
    mine = CV._kept(page, kept, names)
    assert all(mine) and time.process_time() - start < 0.2


@pytest.mark.parametrize("msg", list(XAVIER_TWICE))
async def test_both_kept_mentions_stay_his_when_a_longer_name_joins(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, msg: str
) -> None:
    """033 review regression: Xavier mentioned twice, the first followed by what reads as another member
    once the roster is back: braced, both stay Xavier's."""
    _, _, other = XAVIER_TWICE[msg]
    monkeypatch.setattr(Directory, "thread", _no_roster)
    opened = await _editable(edit_board, msg)
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Xavier Doe", XAV), (other, OTHER)))
    answer = await _edit(
        edit_board, msg, opened["text"], mentions=opened["picks"], keep_quotes=[], keep_images=[]
    )
    assert answer.get("ok") and _sent(writes) == [XAV, XAV]


async def test_an_unpicked_shared_label_beside_a_kept_one_is_refused_as_ambiguous(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review regression: opened with Xavier only, saved once `Doe, John` joined, with ` cc @Doe`
    typed unpicked: Xavier's braced mention is his, the new `@Doe` names two people, so the save says so
    rather than guess."""
    xavier, john = ("Xavier Doe", XAV), ("Doe, John", OTHER)
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), xavier))
    opened = await _editable(edit_board, "20")
    assert opened["text"] == "@{Xavier Doe|Doe}, John said"
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), xavier, john))
    keep = {"keep_quotes": [], "keep_images": []}
    answer = await _edit(edit_board, "20", opened["text"] + " cc @Doe", mentions=opened["picks"], **keep)
    assert (
        answer.get("error", "").startswith("ambiguous mention @Doe: Doe, John, Xavier Doe") and writes == []
    )


@pytest.mark.parametrize("typed", ["-Bob", " Bob"])
async def test_text_typed_after_a_kept_mention_never_moves_it(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    """033 review regression: Ann's kept `@{Ann Lee|Lee}` followed by `-Bob` or ` Bob`, a member `Lee-Bob X`
    or `Lee Bob` in the chat: the brace ends her mention, it stays hers."""
    other = "Lee-Bob X" if typed == "-Bob" else "Lee Bob"
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), (other, MOORE)))
    opened = await _editable(edit_board, "16")
    text = opened["text"].replace("@{Ann Lee|Lee} and", f"@{{Ann Lee|Lee}}{typed} and")
    answer = await _edit(edit_board, "16", text, mentions=opened["picks"], keep_quotes=[], keep_images=[])
    assert answer.get("ok") and _sent(writes) == [ANN, ANN]


async def test_namesakes_sharing_a_label_come_back_braced(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review: Ann Lee and Ann Moore both `Ann`: each braced with her full name, so deleting one, or
    inserting a new `@Ann` before them, never hands one's mention to the other."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)))
    opened = await _editable(edit_board, "27")
    assert opened["text"] == "@{Ann Lee|Ann} and @{Ann Moore|Ann}"
    first, second = opened["picks"]
    keep = {"keep_quotes": [], "keep_images": []}
    shapes = (
        ("@{Ann Lee|Ann} and someone", [first], [ANN]),  # the second deleted
        ("someone and @{Ann Moore|Ann}", [second], [MOORE]),  # the first deleted
        (
            "@Ann hi @{Ann Lee|Ann} and @{Ann Moore|Ann}",
            [{"name": "Ann Moore", "mri": MOORE}, first, second],
            [MOORE, ANN, MOORE],
        ),
    )
    for text, picks, mris in shapes:
        writes.clear()
        # Anchored where the page moved them as the text changed.
        answer = await _edit(edit_board, "27", text, mentions=_anchor(text, *picks), **keep)
        assert answer.get("ok"), (text, answer)
        assert [e["mri"] for e in json.loads(writes[0][3]["properties"]["mentions"])] == mris, text


async def test_namesakes_sharing_a_label_with_no_name_known_are_not_offered(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review: no roster, so `{Ann|Ann}` twice: nothing tells them apart, so no editor."""
    monkeypatch.setattr(Directory, "thread", _no_roster)
    assert (await _editable(edit_board, "27"))["blocked"] == "a mention the edit would read as someone else"


@pytest.mark.parametrize(
    ("copy", "new"),
    [({"name": "{Ann Moore|Ann}", "mri": MOORE}, "Ann"), ({"name": "{Lee|Lee}", "mri": ANN}, "Lee")],
)
def test_a_kept_mention_given_back_as_spelled_wins_over_a_new_namesake_pick(
    copy: dict[str, str], new: str
) -> None:
    """033 review: a new pick under the kept one's bare label, listed first, stays new: the kept mention
    given back exactly as spelled is matched first, each kept mention once."""
    page = ({"name": new, "mri": copy["mri"]}, {"name": copy["name"], "mri": copy["mri"]})
    assert CV._kept(page, [copy], {ANN: "Ann Lee", MOORE: "Ann Moore"}) == [None, copy]


# --- the chat page's composer: picks anchored at their `@`, as ui.js keeps them (spec 033) ---


class _Page:
    """The composer's text and its picks as ui.js keeps them (spec 033): each pick anchored at the `@` it was
    inserted at; an edit drops the anchors inside the span it changes and shifts those after it.
    `mentions()` is what an edit sends."""

    def __init__(self, text: str, picks: list[dict[str, Any]]) -> None:
        self.text, self.picked = text, [dict(p) for p in picks]

    def mentions(self) -> list[dict[str, Any]]:
        return sorted(
            (p for p in self.picked if self.text[p["at"] : p["at"] + 1] == "@"), key=lambda p: p["at"]
        )

    def change(self, a: int, b: int, new: str) -> None:
        """`text[a:b]` becomes `new`: anchors inside it go, those after it shift."""
        d = len(new) - (b - a)
        self.picked = [
            p if p["at"] < a else {**p, "at": p["at"] + d} for p in self.picked if not a <= p["at"] < b
        ]
        self.text = self.text[:a] + new + self.text[b:]

    def pick(self, name: str, mri: str) -> None:
        """A member picked from the list at the end of the text: `@Name ` inserted, anchored at its `@`."""
        at = len(self.text) + 1
        self.change(len(self.text), len(self.text), f" @{name} ")
        self.picked.append({"name": name, "mri": mri, "at": at})

    def backspace(self) -> None:
        """Backspace at the end: right after a picked full name (and its space) its first name is left,
        else one character goes."""
        end = len(self.text) - self.text.endswith(" ")
        for p in self.picked:
            first = p["name"].split()[0] if p["name"].split() else ""
            if not p.get("everyone") and not p["name"].startswith("{") and first != p["name"]:
                if p["at"] + 1 + len(p["name"]) == end and self.text.startswith("@" + p["name"], p["at"]):
                    self.change(p["at"] + 1 + len(first), end, "")
                    return
        self.change(len(self.text) - 1, len(self.text), "")

    def delete_last(self, piece: str) -> None:
        cut = self.text.rindex(piece)
        self.change(cut, cut + len(piece), "")

    def delete_first(self, piece: str) -> None:
        cut = self.text.index(piece)
        self.change(cut, cut + len(piece), "")


async def _save(board: W.Board, msg: str, page: _Page) -> dict[str, Any]:
    return await _edit(board, msg, page.text, mentions=page.mentions(), keep_quotes=[], keep_images=[])


def _sent(writes: list[Any]) -> list[str]:
    return [e["mri"] for e in json.loads(writes[0][3]["properties"]["mentions"])] if writes else []


async def test_a_kept_label_a_namesake_shares_cannot_go_to_her_on_the_page(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review (a): Ann Lee's `@Ann` opened with Ann Moore in the chat comes back braced; Ann Moore
    picked, shortened to `@Ann`, then her `@Ann` deleted: the page sends Ann Lee's mention, never Moore."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)))
    opened = await _editable(edit_board, "28")
    assert opened["text"] == "hi @{Ann Lee|Ann}"
    page = _Page(opened["text"], opened["picks"])
    page.pick("Ann Moore", MOORE)
    page.backspace()
    assert page.text.endswith(" @Ann ")
    page.delete_last("@Ann ")
    assert (await _save(edit_board, "28", page)).get("ok")
    assert _sent(writes) == [ANN]


async def test_a_departed_persons_label_a_member_bears_cannot_go_to_the_member(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review (b): someone gone's `@Ann Lee` opened with a member Ann Lee comes back braced; the member
    picked, then that new mention deleted: the page sends the departed person's mention, never the member."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN)))
    opened = await _editable(edit_board, "29")
    assert opened["text"] == "hi @{Ann Lee|Ann Lee}"
    page = _Page(opened["text"], opened["picks"])
    page.pick("Ann Lee", ANN)
    page.delete_last("@Ann Lee ")
    assert (await _save(edit_board, "29", page)).get("ok")
    assert _sent(writes) == [GONE]


@pytest.mark.parametrize(
    ("braced", "name", "mri", "sent"),
    [("{Ann Lee|Ann}", "Ann Lee", ANN, [MOORE, ANN]), ("{Ann Moore|Ann}", "Ann Moore", MOORE, [ANN, MOORE])],
)
async def test_a_namesake_deleted_then_picked_again_and_shortened_saves(
    edit_board: W.Board,
    writes: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    braced: str,
    name: str,
    mri: str,
    sent: list[str],
) -> None:
    """033 review: namesakes opened braced; one's mention deleted, she is picked again and shortened to
    `@Ann`: a new pick naming her own person, read as her, so no "needs its full name" refusal."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)))
    opened = await _editable(edit_board, "27")
    page = _Page(opened["text"], opened["picks"])
    page.delete_first(f"@{braced}")
    page.pick(name, mri)
    page.backspace()
    answer = await _save(edit_board, "27", page)
    assert answer.get("ok"), (page.text, answer)
    assert _sent(writes) == sent


@pytest.mark.parametrize(("msg", "longer"), [("20", "Doe, John"), ("25", "Doe, John"), ("30", "Xavier Jr")])
async def test_a_longer_member_picked_beside_a_kept_mention_takes_only_his_own(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, msg: str, longer: str
) -> None:
    """033 review regression (page simulated): Xavier's mention opened with Xavier alone in the chat, then
    `Doe, John` (or `Xavier Jr`) joins and is picked from the list: braced, the page tells them apart, so
    Xavier keeps his and the new member gets the one picked."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Xavier Doe", XAV)))
    opened = await _editable(edit_board, msg)
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Xavier Doe", XAV), (longer, OTHER)))
    page = _Page(opened["text"], opened["picks"])
    page.pick(longer, OTHER)
    answer = await _save(edit_board, msg, page)
    assert answer.get("ok"), (page.text, answer)
    kept = len(opened["picks"])
    assert _sent(writes) == [XAV] * kept + [OTHER]


@pytest.mark.parametrize(("msg", "kept"), [("28", "@{Ann Lee|Ann} "), ("16", "@{Ann Lee|Lee} ")])
async def test_backspace_after_a_kept_brace_deletes_a_character_and_the_save_works(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, msg: str, kept: str
) -> None:
    """033 review (page simulated): Backspace right after a kept `@{Ann Lee|Ann} ` is an ordinary Backspace,
    never a shortening to `{Ann` (which left a pick no text matched and failed every save)."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN)))
    opened = await _editable(edit_board, msg)
    page = _Page(f"hi {kept}", opened["picks"][:1])
    page.backspace()
    assert page.text == f"hi {kept.rstrip()}" and page.mentions() == opened["picks"][:1]
    answer = await _save(edit_board, msg, page)
    assert answer.get("ok"), (page.text, answer)
    assert _sent(writes) == [ANN]


@pytest.mark.parametrize("name", ["Ann } Lee", "Ann\nLee"])
def test_a_full_name_no_brace_can_hold_gives_only_the_label_brace(name: str) -> None:
    """033 review: decompose and the save share one rule (brace_name): a roster name holding `}` or a line
    break is no Who, so the kept mention is known as `{label|label}` only."""
    assert CV._spellings({"name": "{Ann|Ann}", "mri": ANN}, {ANN: name}) == {"{Ann|Ann}"}


BOB_LEE = "8:orgid:00000000-0000-0000-0000-0000000000c3"


MISREAD_ = "a mention the edit would read as someone else"


@pytest.mark.parametrize(
    ("people", "text", "outcome"),
    [
        # Ann left, nobody answers to `Lee`: her `{Lee|Lee}` saved as shown, the page's picks lost, stays hers
        ((("Me Self", ME), ("Bob Roe", BOB_LEE)), None, [ANN, ANN]),
        # opened with her full name, saved as shown with the picks lost
        ((("Me Self", ME), ("Ann Lee", ANN), ("Bob Lee", BOB_LEE)), None, [ANN, ANN]),
        # the round-4 repro: Bob Lee answers to `Lee` now: whose it is cannot be told, refused
        ((("Me Self", ME), ("Bob Lee", BOB_LEE)), None, MISREAD_),
        # a member whose name is the kept label `the boss`: refused the same way
        ((("Me Self", ME), ("the boss", BOB_LEE)), None, MISREAD_),
        # `Lee` names two others (Bob Lee, Lee Smith): ambiguous, refused
        ((("Me Self", ME), ("Bob Lee", BOB_LEE), ("Lee Smith", OTHER)), None, MISREAD_),
        # the label edited: not the shown spelling, a new brace, the roster's (Who `Lee` is Bob Lee now)
        ((("Me Self", ME), ("Bob Lee", BOB_LEE)), "hi @{Lee|Leo} and @{the boss|the boss}", [BOB_LEE, ANN]),
    ],
)
async def test_a_kept_brace_whose_page_pick_was_lost(
    edit_board: W.Board,
    writes: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    people: tuple[tuple[str, str], ...],
    text: str | None,
    outcome: Any,
) -> None:
    """033 review r4/r5: the page lost its anchors (an undo) and sends no pick: a brace spelled exactly as the
    editor shows a kept mention stays the message's own while its Who names nobody else on the roster; naming
    someone else, it is refused; any other brace is read by the roster rule."""
    monkeypatch.setattr(Directory, "thread", _people(*people))
    opened = await _editable(edit_board, "16")
    answer = await _edit(
        edit_board, "16", text or opened["text"], mentions=[], keep_quotes=[], keep_images=[]
    )
    if isinstance(outcome, str):
        assert answer["error"] == outcome and writes == []
    else:
        assert answer.get("ok"), answer
        assert _sent(writes) == outcome


@pytest.mark.parametrize(
    ("msg", "people", "add", "outcome"),
    [
        # Xavier kept as `Ann`; a new `{Ann|Ann}`: labelled as he is, but its Who reads Ann Lee: refused
        ("22", (("Me Self", ME), ("Xavier Doe", XAV), ("Ann Lee", ANN)), " cc @{Ann|Ann}", MISREAD_),
        # someone gone kept as `{Ann Lee|Ann Lee}`; member Ann Lee's own brace typed: refused, not the gone
        ("29", (("Me Self", ME), ("Ann Lee", ANN)), " cc @{Ann Lee|Ann Lee}", MISREAD_),
        # Ann Lee kept as `Ann`; a new `{Ann|Ann}` with Ann Moore in the chat: Who ambiguous, refused
        ("28", (("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)), " cc @{Ann|Ann}", MISREAD_),
        # a label no kept mention bears: the roster rule
        ("22", (("Me Self", ME), ("Xavier Doe", XAV), ("Ann Lee", ANN)), " cc @{Ann|boss}", [XAV, ANN]),
    ],
)
async def test_a_new_brace_typed_beside_kept_mentions_is_never_taken_by_them(
    edit_board: W.Board,
    writes: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    msg: str,
    people: Any,
    add: str,
    outcome: Any,
) -> None:
    """033 review r5/r6: an edit opened, a brace typed (no page pick, `@{` opens no list) and saved with the
    opened picks: labelled as a kept mention, it refuses unless its Who names that kept person (or nobody, one
    so labelled); with another label it is the roster's; it never silently becomes a kept person's."""
    monkeypatch.setattr(Directory, "thread", _people(*people))
    opened = await _editable(edit_board, msg)
    keep = {"keep_quotes": [], "keep_images": []}
    answer = await _edit(edit_board, msg, opened["text"] + add, mentions=opened["picks"], **keep)
    if isinstance(outcome, str):
        assert answer["error"] == outcome and writes == []
    else:
        assert answer.get("ok"), answer
        assert _sent(writes) == outcome


@pytest.mark.parametrize("saving_with", [(("Ann Lee", ANN), ("Ann Moore", MOORE)), (("Ann Lee", ANN),)])
async def test_a_lost_pick_on_a_spelling_two_people_could_be_is_refused(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, saving_with: Any
) -> None:
    """033 review r4/r5: namesakes Ann Lee and Ann Moore both kept as `Ann`; a lost pick on `@{Ann|Ann}`:
    ambiguous on the full roster, and, with only Ann Lee left (Ann Moore's shown spelling now `{Ann|Ann}`),
    naming Ann Lee rather than Ann Moore: refused either way, nothing written; their own spellings save."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)))
    opened = await _editable(edit_board, "27")
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), *saving_with))
    keep = {"keep_quotes": [], "keep_images": []}
    answer = await _edit(edit_board, "27", "@{Ann|Ann} hi", mentions=[], **keep)
    assert "error" in answer and writes == []
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)))
    assert (await _edit(edit_board, "27", opened["text"], mentions=[], **keep)).get("ok")
    assert _sent(writes) == [ANN, MOORE]


async def test_a_lost_pick_whose_shown_who_two_members_answer_to_is_refused(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """033 review r5: someone gone kept as `{Ann Lee|Ann Lee}`, two members now called Ann Lee: a lost pick
    there cannot be told from them, refused."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Lee", OTHER)))
    opened = await _editable(edit_board, "29")
    answer = await _edit(edit_board, "29", opened["text"], mentions=[], keep_quotes=[], keep_images=[])
    assert answer["error"] == MISREAD_ and writes == []


NEWB = "8:orgid:00000000-0000-0000-0000-0000000000c4"


@pytest.mark.parametrize(
    ("msg", "opening", "saving", "outcome"),
    [
        # opened with Ann Lee (`{Ann Lee|Lee}`); she left and another Ann Lee joined: whose, cannot be told
        ("16", (("Ann Lee", ANN),), (("Ann Lee", NEWB),), MISREAD_),
        # namesakes kept as `Ann`, both still here: each brace's Who names its own
        (
            "27",
            (("Ann Lee", ANN), ("Ann Moore", MOORE)),
            (("Ann Lee", ANN), ("Ann Moore", MOORE)),
            [ANN, MOORE],
        ),
        # the same, Ann Moore gone: Ann Lee's brace could be the absent `Ann` too, so it refuses
        ("27", (("Ann Lee", ANN), ("Ann Moore", MOORE)), (("Ann Lee", ANN),), MISREAD_),
        # Ann Moore already gone at opening (`{Ann|Ann}`): its Who reads Ann Lee, yet it is the gone one's
        ("27", (("Ann Lee", ANN),), (("Ann Lee", ANN),), MISREAD_),
    ],
)
async def test_lost_picks_saved_unchanged_after_the_roster_moved(
    edit_board: W.Board,
    writes: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    msg: str,
    opening: Any,
    saving: Any,
    outcome: Any,
) -> None:
    """033 review r6: an edit opened on one roster, its picks lost (an undo), saved unchanged on another: a
    brace bearing a kept label is that kept person's only when its Who names them and no other so labelled
    is gone; else refused, never sent to whoever answers to the name now."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), *opening))
    opened = await _editable(edit_board, msg)
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), *saving))
    answer = await _edit(edit_board, msg, opened["text"], mentions=[], keep_quotes=[], keep_images=[])
    if isinstance(outcome, str):
        assert answer["error"] == outcome and writes == []
    else:
        assert answer.get("ok"), answer
        assert _sent(writes) == outcome


@pytest.mark.parametrize(
    "braces", [["@{Lee|Lee}"] * 25 + ["@{the boss|the boss}"] * 25, ["@{the boss|the boss}"] * 50]
)
async def test_lost_kept_braces_read_the_roster_once(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, braces: list[str]
) -> None:
    """033 review r6 M1: 1000 members; 1800 braces bearing a kept label are refused before any roster work,
    and 50 are read with the roster indexed once and each distinct Who once."""
    from miniteams import compose

    people = [("Me Self", ME), ("Bob Roe", BOB_LEE)] + [(f"P{n} N{n}", f"8:orgid:x-{n}") for n in range(1000)]
    monkeypatch.setattr(Directory, "thread", _people(*people))
    keep = {"keep_quotes": [], "keep_images": []}
    start = time.perf_counter()
    answer = await _edit(edit_board, "16", " ".join(["@{Lee|Lee}"] * 1800), mentions=[], **keep)
    assert answer["error"] == "too many mentions (> 50)" and time.perf_counter() - start < 0.5
    indexed: list[int] = []
    real_index = compose._forms_by_word
    monkeypatch.setattr(compose, "_forms_by_word", lambda *a, **k: indexed.append(1) or real_index(*a, **k))
    text = " ".join(braces)
    start = time.perf_counter()
    answer = await _edit(edit_board, "16", text, mentions=[], **keep)
    assert answer.get("ok"), answer
    assert len(indexed) == 2 and time.perf_counter() - start < 0.5  # the save's reader, then the resolver
    indexed.clear()
    answer = await _edit(edit_board, "16", "hi @Bob", mentions=[], **keep)
    assert answer.get("ok"), answer
    assert len(indexed) == 1  # no lost brace: the resolver only


@pytest.mark.parametrize("brace", ["@{Zed|Ann}", "@{Nobody Here|Ann}"])
async def test_a_lost_brace_naming_nobody_with_a_label_two_kept_share_is_refused(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, brace: str
) -> None:
    """033 review r6: namesakes kept as `Ann`; a brace labelled `Ann` whose Who names nobody could be either:
    refused."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN), ("Ann Moore", MOORE)))
    await _editable(edit_board, "27")
    answer = await _edit(edit_board, "27", f"{brace} hi", mentions=[], keep_quotes=[], keep_images=[])
    assert answer["error"] == MISREAD_ and writes == []


APP = "28:00000000-0000-0000-0000-0000000000d1"


@pytest.mark.parametrize(
    ("who", "roster", "outcome"),
    [
        ("Me Self", True, MISREAD_),  # Who naming me, or an app, names someone: never the kept Ann Lee
        ("Me", True, MISREAD_),
        ("Jira Cloud", True, MISREAD_),
        ("Jira", True, MISREAD_),
        ("Bob Smith", False, "roster unavailable"),  # without a roster, only `{label|label}` names nobody
        ("Ann Lee", False, "roster unavailable"),
        ("Lee", False, [ANN]),
        ("the boss", False, [ANN]),
    ],
)
async def test_a_lost_brace_whose_who_no_reader_saw_is_refused(
    edit_board: W.Board,
    writes: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    who: str,
    roster: bool,
    outcome: Any,
) -> None:
    """033 review r7: a brace bearing the kept label `Lee`, its pick lost (Who edited, or an undo), is Ann
    Lee's only when its Who names her or nobody: me and apps are somebody, and without a roster nobody can
    be told from somebody but by `{Lee|Lee}`."""
    people = (("Me Self", ME), ("Ann Lee", ANN), ("Bob Smith", OTHER), ("Jira Cloud", APP))
    monkeypatch.setattr(Directory, "thread", _people(*people))
    await _editable(edit_board, "16")
    if not roster:
        monkeypatch.setattr(Directory, "thread", _no_roster)
    label = "the boss" if who == "the boss" else "Lee"
    text = f"hi @{{{who}|{label}}}"
    answer = await _edit(edit_board, "16", text, mentions=[], keep_quotes=[], keep_images=[])
    if isinstance(outcome, str):
        assert answer["error"] == outcome and writes == []
    else:
        assert answer.get("ok"), answer
        assert _sent(writes) == outcome


@pytest.mark.parametrize("who", ["Gone Person", "zzz"])
async def test_a_lost_brace_whose_who_is_off_the_roster_is_refused(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    """033 review r8: a brace bearing the kept label `Lee` whose Who names nobody on the roster, yet is
    neither the label nor a form of Ann Lee's name, could be anyone: refused, never sent to Ann."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN)))
    await _editable(edit_board, "16")
    answer = await _edit(edit_board, "16", f"hi @{{{who}|Lee}}", mentions=[], keep_quotes=[], keep_images=[])
    assert answer["error"] == MISREAD_ and writes == []


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("@{Gone Person|Gone Person} ok", [GONE]),  # Who is the label: the one gone so labelled
        ("@{Gone|Gone Person} ok", [GONE]),  # a form of the copy's name for them
        ("@{Person|Gone Person} ok", [GONE]),
        ("@{gone|Gone Person} ok", [GONE]),  # folded, as the resolver reads names
        ("@{GÖNE|Gone Person} ok", [GONE]),
        ("@{zzz|Gone Person} ok", MISREAD_),
    ],
)
async def test_a_lost_brace_for_someone_gone_keeps_only_their_own_names(
    edit_board: W.Board, writes: list[Any], monkeypatch: pytest.MonkeyPatch, text: str, outcome: Any
) -> None:
    """033 review r8: Gone Person left; a lost brace labelled as them is theirs only under their names."""
    monkeypatch.setattr(Directory, "thread", _people(("Me Self", ME), ("Ann Lee", ANN)))
    await _editable(edit_board, "10")
    answer = await _edit(edit_board, "10", text, mentions=[], keep_quotes=[], keep_images=[])
    if isinstance(outcome, str):
        assert answer["error"] == outcome and writes == []
    else:
        assert answer.get("ok"), answer
        assert _sent(writes) == outcome
