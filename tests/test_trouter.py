"""Trouter URL building, endpoint id persistence, info + handshake parsing."""

from typing import Any

from miniteams import trouter
from miniteams.config import Settings


def test_common_query_contains_required_params(settings: Settings) -> None:
    q = trouter.common_query(settings, {"sr": "x", "sig": "y"}, "epid-1")
    assert "tc=" in q
    assert "con_num=1234567890123_1" in q
    assert "epid=epid-1" in q
    assert "auth=true" in q
    assert "timeout=40" in q
    assert "sr=x" in q and "sig=y" in q
    assert q.endswith("&")


def test_get_or_create_epid_persists_and_reuses(settings: Settings) -> None:
    first = trouter.get_or_create_epid(settings)
    second = trouter.get_or_create_epid(settings)
    assert first == second
    assert (settings.config_dir / "endpoint_id").read_text().strip() == first


def test_trouter_info_parses_response(settings: Settings, monkeypatch) -> None:
    data = {"socketio": "https://t/", "surl": "https://t/v4/f/x/", "connectparams": {"sr": "1"}}

    class R:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return data

    monkeypatch.setattr(trouter.httpx, "post", lambda *a, **k: R())
    info = trouter.trouter_info(settings, "sk", "epid")
    assert info["surl"] == "https://t/v4/f/x/"
    assert info["socketio"] == "https://t/"
    assert info["connectparams"] == {"sr": "1"}


def test_trouter_info_falls_back_to_default_socketio(settings: Settings, monkeypatch) -> None:
    class R:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"surl": "https://t/v4/f/x/"}  # no socketio

    monkeypatch.setattr(trouter.httpx, "post", lambda *a, **k: R())
    info = trouter.trouter_info(settings, "sk", "epid")
    assert info["socketio"] == settings.trouter_socketio_fallback


def test_handshake_extracts_session_id(settings: Settings, monkeypatch) -> None:
    class R:
        text = "SESSION123:70:70:websocket,xhr-polling"

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(trouter.httpx, "get", lambda *a, **k: R())
    sid = trouter.handshake(settings, {"socketio": "https://t/", "connectparams": {}}, "sk", "epid")
    assert sid == "SESSION123"


def test_epid_is_per_consumer_and_stable(settings: Settings) -> None:
    stream_id = trouter.get_or_create_epid(settings)
    web_id = trouter.get_or_create_epid(settings, "endpoint_id-web")
    assert stream_id != web_id  # a shared epid lets one process steal the other's deliveries
    assert trouter.get_or_create_epid(settings) == stream_id
    assert trouter.get_or_create_epid(settings, "endpoint_id-web") == web_id
