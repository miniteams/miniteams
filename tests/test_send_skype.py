"""Outbound send + skype-token exchange (httpx mocked)."""

from typing import Any

import pytest

from miniteams import send, skype
from miniteams.config import Settings


class _Resp:
    def __init__(self, data: dict[str, Any], status: int = 200, content: bytes = b"{}") -> None:
        self._data = data
        self.status_code = status
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._data


def test_send_message_builds_request(settings: Settings, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kw: Any) -> _Resp:
        captured["url"] = url
        captured.update(kw)
        return _Resp({}, status=201)

    monkeypatch.setattr(send._CLIENT, "post", fake_post)
    send.send_message(settings, "sk", "48:notes", "a<b\nc", "Me")

    # thread id is url-encoded (`:` → %3A), matching the working dump path.
    assert captured["url"].endswith("/v1/users/ME/conversations/48%3Anotes/messages")
    assert captured["headers"]["X-Skypetoken"] == "sk"
    body = captured["json"]
    assert body["messagetype"] == "RichText/Html"
    assert body["contenttype"] == "text"
    assert body["content"] == "a&lt;b<br>c"  # escaped + newline→<br>
    assert body["clientmessageid"].isdigit()
    assert body["imdisplayname"] == "Me"


def test_send_message_html_mode_sends_raw(settings: Settings, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kw: Any) -> _Resp:
        captured.update(kw)
        return _Resp({}, status=201)

    monkeypatch.setattr(send._CLIENT, "post", fake_post)
    send.send_message(settings, "sk", "48:notes", "<b>hi</b> & <i>x</i>", "Me", is_html=True)
    assert captured["json"]["content"] == "<b>hi</b> & <i>x</i>"  # verbatim, not escaped


def test_parse_message_link() -> None:
    url = (
        "https://teams.cloud.microsoft/l/message/48:notes/1781920776714"
        "?context=%7B%22contextType%22%3A%22chat%22%7D"
    )
    assert send.parse_message_link(url) == ("48:notes", "1781920776714")
    assert send.parse_message_link("just-an-id") is None


def test_edit_message_puts_with_skypeeditedid(settings: Settings, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_put(url: str, **kw: Any) -> _Resp:
        captured["url"] = url
        captured.update(kw)
        return _Resp({}, status=200)

    monkeypatch.setattr(send._CLIENT, "put", fake_put)
    send.edit_message(settings, "sk", "48:notes", "1781920776714", "new <b>text</b>", is_html=True)
    assert captured["url"].endswith("/conversations/48%3Anotes/messages/1781920776714")
    assert captured["json"]["skypeeditedid"] == "1781920776714"
    assert captured["json"]["content"] == "new <b>text</b>"


def test_send_message_raises_on_error_envelope(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        send._CLIENT, "post", lambda *a, **k: _Resp({"errorCode": 1, "message": "nope"}, content=b"{...}")
    )
    with pytest.raises(RuntimeError, match="send rejected"):
        send.send_message(settings, "sk", "t", "x", "Me")


def test_exchange_skype_token_tokens_shape(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        skype.httpx,
        "post",
        lambda *a, **k: _Resp({"tokens": {"skypeToken": "ST", "expiresIn": 3600}, "region": "fr"}),
    )
    out = skype.exchange_skype_token(settings, "aad")
    assert out == {"skype_token": "ST", "expires_in": 3600, "region": "fr"}


def test_exchange_skype_token_legacy_shape(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(skype.httpx, "post", lambda *a, **k: _Resp({"skypeToken": {"skypetoken": "ST2"}}))
    assert skype.exchange_skype_token(settings, "aad")["skype_token"] == "ST2"


def test_exchange_skype_token_missing_raises(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(skype.httpx, "post", lambda *a, **k: _Resp({"nope": 1}))
    with pytest.raises(RuntimeError, match="no skype token"):
        skype.exchange_skype_token(settings, "aad")


def test_mark_read_puts_consumption_horizon(settings: Settings, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_put(url: str, **kw: Any) -> _Resp:
        captured["url"] = url
        captured.update(kw)
        return _Resp({}, status=200)

    monkeypatch.setattr(send._CLIENT, "put", fake_put)
    monkeypatch.setattr(send.time, "time", lambda: 1789470000.5)
    send.mark_read(settings, "sk", "19:a@thread.v2", "1789466400000")
    assert captured["url"].endswith("/conversations/19%3Aa%40thread.v2/properties?name=consumptionhorizon")
    assert captured["json"] == {"consumptionhorizon": "1789466400000;1789470000500;1789466400000"}
    assert captured["headers"]["X-Skypetoken"] == "sk"


def test_exchange_skype_token_follows_the_account_region(settings: Settings, monkeypatch) -> None:
    """The APAC default host costs ~2 s a call from the EU; the authz reply names the right one."""
    region = {"chatService": "https://fr.ng.msg.teams.microsoft.com"}
    reply = {"tokens": {"skypeToken": "ST"}, "regionGtms": region}
    monkeypatch.setattr(skype.httpx, "post", lambda *a, **k: _Resp(reply))
    skype.exchange_skype_token(settings, "aad")
    assert settings.contacts_host == "fr.ng.msg.teams.microsoft.com"


def test_exchange_skype_token_keeps_an_explicit_host(monkeypatch) -> None:
    monkeypatch.setenv("MINITEAMS_CONTACTS_HOST", "pinned.example")
    pinned = Settings(tenant_id="t")
    reply = {"tokens": {"skypeToken": "ST"}, "regionGtms": {"chatService": "https://fr.example"}}
    monkeypatch.setattr(skype.httpx, "post", lambda *a, **k: _Resp(reply))
    skype.exchange_skype_token(pinned, "aad")
    assert pinned.contacts_host == "pinned.example"


def test_exchange_skype_token_without_region_keeps_the_default(settings: Settings, monkeypatch) -> None:
    default = settings.contacts_host
    monkeypatch.setattr(skype.httpx, "post", lambda *a, **k: _Resp({"tokens": {"skypeToken": "ST"}}))
    skype.exchange_skype_token(settings, "aad")
    assert settings.contacts_host == default
