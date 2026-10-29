"""A Reply block's author reads as the name known for its MRI, not the "Display Name" placeholder."""

from miniteams import chat_verbs as CV
from miniteams.compose import reply_quote
from miniteams.mcp import _scheduled_record, message_record
from miniteams.messages import message_text, quote_names

ANN = "8:orgid:00000000-0000-0000-0000-00000000000a"
BOT = "28:00000000-0000-0000-0000-00000000000b"
NAMES = {ANN: "Ann <Lee>"}


def known(mri: str) -> str:
    return NAMES.get(mri, "")


def _quote(mri: str, name: str = "Display Name", line: str = "q") -> str:
    quoted = {"id": "1", "messagetype": "RichText/Html", "content": f"<p>{line}</p>", "from": mri}
    return reply_quote(quoted, name)


def _reply(content: str, msgtype: str = "RichText/Html") -> dict[str, object]:
    return {"id": "2", "messagetype": msgtype, "content": content, "from": ANN}


def test_the_placeholder_takes_the_known_name_escaped() -> None:
    assert ">Ann &lt;Lee&gt;</strong>" in quote_names(_quote(ANN) + "<p>a</p>", known)


def test_every_quote_is_named_an_unknown_author_keeps_its_text() -> None:
    named = quote_names(_quote(ANN) + _quote(BOT, "Some Bot") + _quote(ANN), known)
    assert named.count(">Ann &lt;Lee&gt;</strong>") == 2
    assert ">Some Bot</strong>" in named


def test_no_directory_leaves_the_content_as_is() -> None:
    content = _quote(ANN)
    assert quote_names(content, None) == content


def test_message_text_names_the_quote() -> None:
    assert message_text(_reply(_quote(ANN) + "<p>aze</p>"), known) == "Ann <Lee> q aze"
    assert message_text(_reply(_quote(ANN) + "<p>aze</p>")) == "Display Name q aze"  # no directory


def test_a_plain_text_message_is_never_rewritten() -> None:
    text = _quote(ANN)
    assert "Display Name" in message_text(_reply(text, "Text"), known)


def test_the_chat_page_and_mcp_show_the_name() -> None:
    message = _reply(_quote(ANN) + "<p>aze</p>")
    html = CV.chat_record(message, "19:t@thread.v2", known_name=known)["html"]
    assert "<strong>Ann &lt;Lee&gt;</strong>" in html
    assert "Display Name" not in html
    assert message_record(message, known)["text"] == "Ann <Lee> q aze"


def test_a_scheduled_reply_names_the_quote() -> None:
    draft = {"id": "d", "innerThreadId": "19:t@thread.v2", "content": _quote(ANN) + "<p>later</p>"}
    assert _scheduled_record(draft, {}, known)["text"] == "Ann <Lee> q later"
