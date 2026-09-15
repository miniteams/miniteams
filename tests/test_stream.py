"""Trouter client dispatch: the `on_event` hook replaces stdout printing when set."""

import json
from typing import Any

from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.stream import TrouterClient


class _Ws:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _client(settings: Settings, directory: Directory, **kw: Any) -> TrouterClient:
    c = TrouterClient(settings, {"access_token": "a"}, "sk", {}, "sid", "epid", directory, **kw)
    c._ws = _Ws()
    return c


def _delivery(body: dict[str, Any], endpoint: str = "messaging") -> str:
    return json.dumps({"id": 7, "url": f"https://h/x/{endpoint}", "body": json.dumps(body)})


async def test_hook_receives_decoded_event_and_frame_is_acked(
    settings: Settings, directory: Directory, capsys
) -> None:
    got: list[dict[str, Any]] = []

    async def hook(obj: dict[str, Any]) -> None:
        got.append(obj)

    client = _client(settings, directory, on_event=hook)
    event = {"type": "EventMessage", "resourceType": "NewMessage", "resource": {"id": "1"}}
    await client._on_delivery(_delivery(event))
    await client._on_delivery(_delivery({"type": "Other"}))  # not an EventMessage → hook not called
    await client._on_delivery(_delivery(event, endpoint="unifiedPresenceService"))
    assert got == [event]
    assert len(client._ws.sent) == 3 and all(s.startswith("3:::") for s in client._ws.sent)  # always ack
    assert capsys.readouterr().out == ""  # nothing printed when a hook is set


async def test_without_hook_stdout_path_still_prints(
    settings: Settings, directory: Directory, capsys
) -> None:
    client = _client(settings, directory)
    event = {
        "type": "EventMessage",
        "resourceType": "NewMessage",
        "resource": {"id": "1", "messagetype": "Text", "content": "hello", "from": "8:x", "to": "48:notes"},
    }
    await client._on_delivery(_delivery(event))
    assert "hello" in capsys.readouterr().out


async def test_hook_exception_is_logged_not_raised(settings: Settings, directory: Directory) -> None:
    async def bad_hook(obj: dict[str, Any]) -> None:
        raise ValueError("poison event")

    client = _client(settings, directory, on_event=bad_hook)
    event = {"type": "EventMessage", "resourceType": "NewMessage", "resource": {"id": "1"}}
    await client._on_delivery(_delivery(event))  # must not raise: the socket stays up
    assert client._ws.sent and client._ws.sent[0].startswith("3:::")
