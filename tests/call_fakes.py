"""Fakes and synthetic data around one Call (spec 044): ids, SDP, rosters, the servers it talks to."""

import asyncio
import base64
import gzip
import json
from collections.abc import Callable
from typing import Any

import httpx

from miniteams import call as C
from miniteams.config import Settings

CALL = "00000000-0000-4000-8000-000000000001"
THREAD = "19:meeting_synthetic@thread.v2"
ME = "8:orgid:00000000-0000-4000-8000-0000000000a0"
OTHER = "8:orgid:00000000-0000-4000-8000-0000000000b0"
KEY_A, KEY_B, KEY_C = bytes(range(30)), bytes(range(1, 31)), bytes(range(2, 32))
SURL = "https://trouter.invalid/v4/f/s/"
# Under the one host suffix the call trusts (call.TRUSTED_HOSTS); synthetic names, never resolved.
CONV = "https://conv.test.flightproxy.teams.microsoft.com/api/v2/epconv"
CONTROLLER = "https://conv.test.flightproxy.teams.microsoft.com/conv/c1"
LEG = "https://cc.test.flightproxy.teams.microsoft.com/cc/v1/active/leg1"
ACK = "https://cc.test.flightproxy.teams.microsoft.com/cc/v1/callAcceptance/1/acknowledge"
ANSWER_URL = "https://cc.test.flightproxy.teams.microsoft.com/cc/v1/active/leg1/answer"
SERVER_PWD = "serverpassword0123456789"  # pragma: allowlist secret


def payload() -> dict[str, Any]:
    return {
        "correlationId": CALL,
        "conversationController": "https://cc.test.flightproxy.teams.microsoft.com/x",
        "userJoinTime": "2026-01-01T00:00:00Z",
        "groupContext": {"threadId": THREAD, "messageId": "0"},
        "meetingInfo": {"organizerId": "00000000-0000-4000-8000-0000000000c0", "tenantId": "t"},
    }


def one_to_one() -> dict[str, Any]:
    """A 1:1 push (seen live): a controller, the participants, neither groupContext nor meetingInfo."""
    return {
        "correlationId": CALL,
        "conversationController": CONTROLLER,
        "userJoinTime": "2026-01-01T00:00:00Z",
        "participants": [OTHER, ME],
        "additionalData": {"participantList": [{"id": OTHER}, {"id": ME}]},
    }


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def server_sdp(key: bytes, extra: str = "", msi: int = 4242, codec: int = 110) -> str:
    """The shape of the server's answer and renegotiation offers (S18), synthetic values."""
    return (
        "v=0\r\no=- 1 0 IN IP4 127.0.0.1\r\ns=session\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
        f"m=audio 3479 RTP/SAVP {codec} 111 0\r\nc=IN IP4 192.0.2.10\r\na=x-source-streamid:{msi}\r\n"
        f"a=x-source:main-audio\r\na=ice-ufrag:Ab+c\r\na=ice-pwd:{SERVER_PWD}\r\na=rtcp-mux\r\n"
        "a=candidate:1 1 UDP 54001663 192.0.2.10 3479 typ relay raddr 10.0.0.1 rport 3479 MTURNID 1\r\n"
        "a=candidate:3 1 tcp-pass 18087935 192.0.2.10 3478 typ relay raddr 10.0.0.1 rport 3478\r\n"
        "a=candidate:bad\r\n"
        f"a=label:main-audio\r\na=mid:0\r\na=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{b64(key)}|2^31\r\n"
        f"a=rtpmap:{codec} x-multichannel/48000\r\na=x-ssrc-range:7-7\r\n{extra}"
    )


def roster(seq: int, *people: tuple[str, str, list[tuple[str, int]]]) -> dict[str, Any]:
    return {
        "type": "Full",
        "sequenceNumber": seq,
        "participants": {
            mri: {
                "details": {
                    "id": mri,
                    "displayName": name,
                    "endpointId": "00000000-0000-0000-0000-000000000000",
                },
                "call": {
                    "mediaStreams": [
                        {"type": label.split("-")[-1], "label": label, "sourceId": sid}
                        for label, sid in streams
                    ]
                },
            }
            for mri, name, streams in people
        },
    }


class FakeIce:
    """aioice.Connection's surface the call uses; datagrams in through `incoming`, out into `sent`."""

    def __init__(self, connect_error: BaseException | None = None, default: bool = True) -> None:
        self.local_username, self.local_password = "uf12", "p" * 22  # pragma: allowlist secret
        self.local_candidates = [C.aioice.Candidate("1", 1, "udp", 99, "192.0.2.1", 5000, "host")]
        self.remote: list[Any] = []
        self.remote_username = self.remote_password = None
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False
        self._connect_error, self._default = connect_error, default

    async def gather_candidates(self) -> None:
        await asyncio.sleep(0)

    def get_default_candidate(self, component: int) -> Any:
        return self.local_candidates[0] if self._default else None

    async def add_remote_candidate(self, c: Any) -> None:
        self.remote.append(c)

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error

    async def recv(self) -> bytes:
        data = await self.incoming.get()
        if data is None:
            raise ConnectionError("closed")
        return data

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class FakeTokens:
    def __init__(self, ic3: str | None = "ic3-token", fail: BaseException | None = None) -> None:
        claims = (
            base64.urlsafe_b64encode(json.dumps({"oid": ME.split(":")[-1]}).encode()).decode().rstrip("=")
        )
        self.aad = {"access_token": f"h.{claims}.s"}
        self.ic3, self.fail = ic3, fail

    def refresh(self) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        return self.aad

    def ic3_token(self) -> str | None:
        return self.ic3

    def display_name(self) -> str:
        return "Me Synthetic"


class Server:
    """The conversation service, the call controller and the Trouter socket, faked around one Call."""

    def __init__(self, answer: str = server_sdp(KEY_B), phase1: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer, self.phase1 = answer, phase1
        self.call: C.Call | None = None
        self.status: dict[str, int] = {}
        self.accept_after_phase2: dict[str, Any] | None = {
            "callAcceptance": {
                "links": {"acknowledgement": ACK, "callLeg": LEG},
                "mediaContent": {"blob": answer, "contentType": "application/sdp"},
            }
        }
        self.trouter_kwargs: dict[str, Any] = {}
        self.conversation_url = CONV  # what the authz answer names

    def body(self, step: str) -> dict[str, Any]:
        return next(json.loads(r.content) for r in self.requests if self.step(r) == step)

    @staticmethod
    def step(r: httpx.Request) -> str:
        url = str(r.url)
        if r.method == "DELETE":
            return "delete"
        return {CONV: "phase1", CONTROLLER: "phase2", ACK: "ack", LEG: "register", ANSWER_URL: "answer"}.get(
            url, url
        )

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.step(request)
        status = self.status.get(step, 202)
        if step == "phase1":
            return httpx.Response(
                self.status.get(step, 200), json=self.phase1 or {"conversationController": CONTROLLER}
            )
        if step == "phase2" and self.accept_after_phase2 is not None and status < 300:
            asyncio.get_running_loop().call_later(
                0.01, self.deliver, "call/acceptance/", self.accept_after_phase2
            )
        return httpx.Response(status, text="{}")

    def deliver(self, path: str, body: Any, gzip_it: bool = False) -> None:
        assert self.call is not None
        raw = json.dumps(body)
        headers = {"MS-CV": "cv.1"}
        if gzip_it:
            raw = base64.b64encode(gzip.compress(raw.encode())).decode()
            headers["X-Microsoft-Skype-Content-Encoding"] = "gzip"
        url = f"{SURL}callAgent/{self.call.endpoint}/{self.call._tag}/{path}"
        asyncio.ensure_future(self.call.on_agent({"id": 1, "url": url, "headers": headers, "body": raw}))

    async def trouter(self, settings: Settings, **kw: Any) -> None:
        self.trouter_kwargs = kw
        settings.conversation_url = self.conversation_url  # run_forever's skype token exchange, first
        await kw["on_session"]("ep", SURL)
        await asyncio.Event().wait()


async def until(cond: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError(f"{what} not met")
        await asyncio.sleep(0.01)
