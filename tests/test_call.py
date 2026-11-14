"""Joining a call and hearing it (spec 044, phase 3 slice 1). Synthetic ids, keys and audio only."""

import asyncio
import base64
import gzip
import json
import logging
import struct
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import av
import httpx
import numpy as np
import numpy.typing as npt
import pylibsrtp
import pytest
import structlog
from structlog.testing import capture_logs

from miniteams import call as C
from miniteams import follow as F
from miniteams import stream as S
from miniteams.config import Settings
from miniteams.directory import Directory

CALL = "00000000-0000-4000-8000-000000000001"
THREAD = "19:meeting_synthetic@thread.v2"
ME = "8:orgid:00000000-0000-4000-8000-0000000000a0"
OTHER = "8:orgid:00000000-0000-4000-8000-0000000000b0"
KEY_A, KEY_B, KEY_C = bytes(range(30)), bytes(range(1, 31)), bytes(range(2, 32))
SURL = "https://trouter.invalid/v4/f/s/"
CONV = "https://conv.invalid/api/v2/epconv"
CONTROLLER = "https://conv.invalid/conv/c1"
LEG = "https://cc.invalid/cc/v1/active/leg1"
ACK = "https://cc.invalid/cc/v1/callAcceptance/1/acknowledge"
ANSWER_URL = "https://cc.invalid/cc/v1/active/leg1/answer"
SERVER_PWD = "serverpassword0123456789"  # pragma: allowlist secret


def payload() -> dict[str, Any]:
    return {
        "correlationId": CALL,
        "conversationController": "https://cc.invalid/x",
        "userJoinTime": "2026-01-01T00:00:00Z",
        "groupContext": {"threadId": THREAD, "messageId": "0"},
        "meetingInfo": {"organizerId": "00000000-0000-4000-8000-0000000000c0", "tenantId": "t"},
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


VIDEO_AND_DATA = (
    "m=video 3478 RTP/SAVP 114 122\r\na=x-source-streamid:4243\r\na=label:main-video\r\na=mid:1\r\n"
    "m=x-data 3480 RTP/SAVP 127 126\r\na=label:data\r\na=mid:4\r\n"
    f"a=cryptoscale:1 server AES_CM_128_HMAC_SHA1_80 inline:{b64(KEY_C)}|2^31|1:1\r\n"
)


def g722(seconds: float = 0.02, freq: float = 440.0) -> list[bytes]:
    ctx = av.CodecContext.create("g722", "w")
    ctx.sample_rate, ctx.layout, ctx.format = 16000, "mono", "s16"
    n = int(16000 * seconds)
    pcm = (np.sin(2 * np.pi * freq * np.arange(n) / 16000) * 8000).astype(np.int16)
    out = []
    for i in range(0, n, 320):
        frame = av.AudioFrame.from_ndarray(pcm[i : i + 320].reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 16000
        out += [bytes(p) for p in ctx.encode(frame)]
    return out


def multichannel(*parts: tuple[int, bytes]) -> bytes:
    """x-multichannel payload (S18 3.3), with the 8-byte-per-block trailer."""
    body = b"".join(struct.pack("<IH", msi, len(d)) + d for msi, d in parts)
    return bytes([0x20 + len(parts), 0x11]) + body + b"\x00" * 8 * len(parts)


def rtp(pt: int, seq: int, ts: int, ssrc: int, data: bytes, csrc: tuple[int, ...] = ()) -> bytes:
    head = struct.pack(">BBHII", 0x80 | len(csrc), pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    return head + b"".join(struct.pack(">I", c) for c in csrc) + data


def tx(key: bytes) -> pylibsrtp.Session:
    return C.srtp_session(key, inbound=False)


# --- SDP ---


def test_offer_puts_multichannel_first_and_declares_the_ssrc_it_sends() -> None:
    """044-AC1: the server sends the first codec listed; the mixer drops audio from an undeclared SSRC."""
    cands = ["a=candidate:1 1 UDP 2130706431 192.0.2.1 5000 typ host"]
    sdp = C.offer("192.0.2.1", 5000, "uf12", "p" * 22, cands, KEY_A, 1234)
    assert "m=audio 5000 RTP/SAVP 110 111 0\r\n" in sdp and "a=x-ssrc-range:1234-1234\r\n" in sdp
    assert "a=rtpmap:110 x-multichannel/48000" in sdp and "a=rtpmap:0 PCMU/8000" in sdp
    assert sdp.count("m=") == 1 and cands[0] in sdp and sdp.endswith("\r\n")
    line = next(x for x in sdp.splitlines() if x.startswith("a=crypto:"))
    assert C.crypto_key(line.removeprefix("a=crypto:")) == KEY_A  # SDES key round trip


def test_candidate_line_keeps_the_related_address() -> None:
    host = C.aioice.Candidate("1", 1, "udp", 99, "192.0.2.1", 5000, "host")
    srflx = C.aioice.Candidate("2", 1, "udp", 98, "198.51.100.1", 6000, "srflx", "192.0.2.1", 5000)
    assert C.candidate_line(host) == "a=candidate:1 1 UDP 99 192.0.2.1 5000 typ host"
    assert C.candidate_line(srflx).endswith("typ srflx raddr 192.0.2.1 rport 5000")


def test_answer_gives_ice_key_codec_and_our_own_msi() -> None:
    """044-AC4: the follower knows itself in the roster by the answer's x-source-streamid."""
    media = C.parse_media(server_sdp(KEY_B) + VIDEO_AND_DATA)
    assert (media.ufrag, media.pwd, media.key, media.codec, media.msi) == (
        "Ab+c",
        SERVER_PWD,
        KEY_B,
        110,
        4242,  # the audio section's, not the video's 4243
    )
    assert len(media.candidates) == 3 and media.candidates[0].startswith("1 1 UDP 54001663 192.0.2.10 3479")


@pytest.mark.parametrize(
    ("sdp", "error"),
    [
        ("v=0\r\nm=video 1 RTP/SAVP 122\r\na=mid:1\r\n", "no audio section"),
        (server_sdp(KEY_B).replace("a=crypto", "a=x-crypto"), "without ICE credentials or SDES key"),
        (server_sdp(KEY_B).replace("a=ice-pwd", "a=x-ice-pwd"), "without ICE credentials or SDES key"),
        (server_sdp(KEY_B).replace(b64(KEY_B), b64(KEY_B[:16])), "without ICE credentials or SDES key"),
        (
            server_sdp(KEY_B).replace("AES_CM_128_HMAC_SHA1_80", "AES_256_CM_HMAC_SHA1_80"),
            "without ICE credentials or SDES key",
        ),
    ],
)
def test_unusable_answers_are_refused(sdp: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        C.parse_media(sdp)


@pytest.mark.parametrize(
    "bad",
    [
        f"a=crypto:1 AES_256_CM_HMAC_SHA1_80 inline:{b64(KEY_C + KEY_C)}|2^31",  # another suite
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{b64(KEY_C[:16])}|2^31",  # short key
        "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:!!notbase64",
        "a=crypto:1",
    ],
)
def test_an_unusable_crypto_line_is_skipped_for_the_next(bad: str) -> None:
    """Untrusted SDP: one line we cannot use does not fail the join while another one keys it."""
    sdp = server_sdp(KEY_B).replace("a=crypto:2", f"{bad}\r\na=crypto:2")
    assert C.parse_media(sdp).key == KEY_B


def test_crypto_key_refuses_what_it_cannot_use() -> None:
    with pytest.raises(ValueError, match="not 30 bytes"):
        C.crypto_key(f"2 AES_CM_128_HMAC_SHA1_80 inline:{b64(KEY_B[:16])}")
    with pytest.raises(ValueError, match="unsupported"):
        C.crypto_key(f"2 AES_256_CM_HMAC_SHA1_80 inline:{b64(KEY_B)}")


def test_renegotiation_answer_keeps_ours_and_refuses_the_rest_with_their_mid() -> None:
    """044-AC1: no answer, no audio; the extra m-lines go back with port 0, each keeping its mid."""
    ours = C.offer("192.0.2.1", 5000, "uf12", "p" * 22, [], KEY_A, 1)
    answer = C.renegotiation_answer(ours, server_sdp(KEY_C) + VIDEO_AND_DATA)
    head, tail = answer.split("m=video", 1)
    assert head == ours  # same ports, ICE credentials and key: the running media goes on
    assert "m=video" + tail == "m=video 0 RTP/SAVP 114\r\na=mid:1\r\nm=x-data 0 RTP/SAVP 127\r\na=mid:4\r\n"
    # A server offer with one section past ours, and one without a mid line.
    two = C.renegotiation_answer(ours, server_sdp(KEY_C) + "m=video 3478 RTP/SAVP 122\r\na=label:v\r\n")
    assert two == ours + "m=video 0 RTP/SAVP 122\r\n"
    assert C.renegotiation_answer(ours, server_sdp(KEY_C)) == ours  # nothing extra to refuse
    # Untrusted: an m-line too short to name back is skipped, the next one still refused.
    short = server_sdp(KEY_C) + "m=video 3478\r\na=mid:1\r\nm=x-data 3480 RTP/SAVP 127\r\na=mid:4\r\n"
    assert C.renegotiation_answer(ours, short) == ours + "m=x-data 0 RTP/SAVP 127\r\na=mid:4\r\n"


def test_redact_drops_every_key() -> None:
    text = C.redact(server_sdp(KEY_B) + VIDEO_AND_DATA)
    assert b64(KEY_B) not in text and b64(KEY_C) not in text and text.count("inline:<redacted>") == 2


@pytest.mark.parametrize(
    "escape",
    [
        lambda k: k.replace("/", "\\/"),  # JSON's optional solidus escape
        lambda k: k.replace("+", "\\u002b").replace("/", "\\u002F"),  # unicode escapes
    ],
)
def test_redact_drops_json_escaped_keys(escape: Callable[[str], str]) -> None:
    key = b64(bytes([0xFB, 0xFF] * 15))  # base64 heavy in "+" and "/"
    assert "+" in key and "/" in key
    text = C.redact(f'{{"blob": "a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{escape(key)}|2^31"}}')
    assert text == '{"blob": "a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:<redacted>|2^31"}'


# --- RTP ---


def test_rtp_header_csrc_extension_and_padding() -> None:
    plain = C.parse_rtp(rtp(110, 7, 960, 5, b"abc", csrc=(201, 414)))
    assert plain == C.Rtp(110, 7, 960, 5, False, (201, 414), b"abc")
    ext = struct.pack(">HH", 0xBEDE, 1) + b"\x01\x02\x03\x04"
    padded = bytearray(rtp(0, 1, 2, 3, ext + b"xy" + b"\x00\x00\x03"))
    padded[0] |= 0x30  # extension and padding
    padded[1] |= 0x80  # marker
    assert C.parse_rtp(bytes(padded)) == C.Rtp(0, 1, 2, 3, True, (), b"xy")


@pytest.mark.parametrize(
    "data",
    [
        b"\x80\x6e",  # shorter than the fixed header
        bytes([0x40]) + rtp(0, 1, 2, 3, b"x")[1:],  # version 1
        bytes([0x82]) + rtp(0, 1, 2, 3, b"")[1:] + b"\x00\x00",  # two CSRCs announced, half of one
        bytes([0x90]) + rtp(0, 1, 2, 3, b"")[1:] + b"\xbe",  # extension header cut
        bytes([0xA0]) + rtp(0, 1, 2, 3, b"x")[1:] + b"\xff",  # more padding than the packet
    ],
)
def test_malformed_rtp_is_none(data: bytes) -> None:
    assert C.parse_rtp(data) is None


def test_rtcp_is_told_from_rtp_by_its_packet_type() -> None:
    assert C.is_rtcp(bytes([0x80, 200])) and C.is_rtcp(bytes([0x81, 201]))
    assert not C.is_rtcp(rtp(110, 1, 1, 1, b"")) and not C.is_rtcp(bytes([0x80, 0x80 | 110]))
    assert not C.is_rtcp(b"\x80")


def test_unwrap_sequence_and_timestamp_wraps() -> None:
    seqs, ext = [65534, 65535, 0, 65535, 1, 2], None  # a late packet from before the wrap
    out = []
    for s in seqs:
        ext = C.unwrap(ext, s, 16)
        out.append(ext)
    assert out == [65534, 65535, 65536, 65535, 65537, 65538]
    ts = C.unwrap(None, 2**32 - 960, 32)
    assert C.unwrap(ts, 0, 32) == 2**32 and C.unwrap(2**32, 2**32 - 960, 32) == 2**32 - 960


def test_timeline_follows_the_rtp_clock_across_a_wrap_and_reanchors_on_a_jump() -> None:
    tl = C.Timeline()
    assert tl.place(1, 2**32 - 960, 100.0) == 100.0
    assert tl.place(1, 0, 100.3) == pytest.approx(100.02)  # wrapped, 20 ms later on its own clock
    assert tl.place(1, 960 * 50, 101.1) == pytest.approx(101.02)  # one second of RTP clock
    assert tl.place(2, 5, 101.2) == 101.2  # each SSRC its own anchor
    assert tl.place(1, 960 * 50 + 48000 * 70, 102.0) == 102.0  # a 70 s jump: re-anchored on arrival
    assert tl.place(1, 960 * 51 + 48000 * 70, 102.5) == pytest.approx(102.02)


# --- x-multichannel and G.722 ---


def test_demux_drops_empty_blocks_and_keeps_the_rest() -> None:
    """An empty block would drain its speaker's G.722 decoder: the next one would raise EOFError."""
    payload = multichannel((201, b"a" * 160), (201, b""), (830, b""), (414, b"b" * 160))
    assert C.blocks(payload) == [(201, b"a" * 160), (414, b"b" * 160)]


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_demux_one_to_four_blocks_with_their_trailer(n: int) -> None:
    parts = [(201 + 213 * i, bytes([i]) * 160) for i in range(n)]
    assert C.blocks(multichannel(*parts)) == parts


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x21",
        b"\x21\x12" + struct.pack("<IH", 1, 1) + b"x",  # second byte is not 0x11
        b"\x20\x11",  # zero blocks
        b"\x21\x11" + struct.pack("<I", 1),  # block header cut
        b"\x22\x11" + struct.pack("<IH", 1, 2) + b"xy" + struct.pack("<IH", 2, 160) + b"z",  # data cut
    ],
)
def test_demux_refuses_malformed_payloads(payload: bytes) -> None:
    assert C.blocks(payload) == []


def test_decoder_keeps_one_g722_state_per_msi() -> None:
    """Interleaved speakers decode as if each were alone: the ADPCM state is per MSI."""
    a, b = g722(0.1, 440.0), g722(0.1, 880.0)
    mixed, alone = C.Decoder(), C.Decoder()
    out_a = []
    for x, y in zip(a, b, strict=True):
        out_a.append(mixed.decode(1, x))
        mixed.decode(2, y)
    ref_a = [alone.decode(1, x) for x in a]
    assert all(o.dtype == np.int16 and o.shape == (320,) for o in out_a)
    assert all(np.array_equal(o, r) for o, r in zip(out_a, ref_a, strict=True))
    assert C.Decoder().decode(1, b"").shape == (0,)


# --- SRTP ---


def test_srtp_round_trip_with_interleaved_ssrcs_keeps_one_roc_per_ssrc() -> None:
    """SDES AES_CM_128_HMAC_SHA1_80; SSRC 7 wraps its sequence while SSRC 9 stays low, interleaved."""
    send, recv = tx(KEY_B), C.srtp_session(KEY_B, inbound=True)
    plain = []
    for i in range(6):
        plain.append(rtp(110, 65533 + i, 960 * i, 7, b"seven %d" % i))
        plain.append(rtp(110, 10 + i, 960 * i, 9, b"nine %d" % i))
    protected = [send.protect(p) for p in plain]
    assert all(len(p) == len(q) + 10 for p, q in zip(protected, plain, strict=True))  # 80-bit tag
    assert [recv.unprotect(p) for p in protected] == plain
    with pytest.raises(pylibsrtp.Error):
        C.srtp_session(KEY_C, inbound=True).unprotect(protected[0])  # another key


# --- roster ---


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


ME_ALL = (
    ME,
    "Me Synthetic",
    [("main-audio", 201), ("main-video", 202), ("main-audio", 414), ("main-audio", 627)],
)
OTHER_ONE = (OTHER, "Other Synthetic", [("main-audio", 830), ("main-video", 831)])


def test_full_roster_names_msis_and_lists_my_endpoints_audio() -> None:
    """044-AC4, AC6: one entry per person, one main-audio stream per endpoint of mine."""
    r = C.Roster(me=ME, own=627)
    assert r.update(roster(5, ME_ALL, OTHER_ONE))
    assert r.mine == {201, 414, 627} and r.names[830] == ("Other Synthetic", OTHER)
    assert r.names[202] == ("Me Synthetic", ME)
    assert r.update(roster(7, (ME, "Me Synthetic", [("main-audio", 627)])))  # the others left
    assert r.mine == {627} and r.names[830] == ("Other Synthetic", OTHER)  # a late line keeps its name
    assert not r.update(roster(6, ME_ALL))  # stale
    assert r.mine == {627}
    assert not r.update({"type": "Delta"})
    odd = {"participants": {OTHER: "x", ME: {"details": None, "call": {"mediaStreams": [{"sourceId": "1"}]}}}}
    assert r.update(odd) and r.mine == set() and r.seq == 7


# --- Trouter callbacks ---


def test_agent_ack_echoes_the_request_headers() -> None:
    """044-AC1: without MS-CV + .0, trouter-request and trouter-client the call ends at ~40 s."""
    req = {"id": 5, "headers": {"MS-CV": "abc.1", "Trouter-Request": '{"id":"r1"}', "X": "y"}}
    assert json.loads(S.agent_ack(req)) == {
        "id": 5,
        "status": 200,
        "headers": {"MS-CV": "abc.1.0", "trouter-request": '{"id":"r1"}', "trouter-client": '{"cd":1}'},
        "body": "",
    }
    assert json.loads(S.agent_ack({"id": 6}))["headers"] == {"trouter-client": '{"cd":1}'}


class _Ws:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)


async def test_call_callbacks_go_to_the_agent_hook_with_the_echoing_ack(
    settings: Settings, directory: Directory
) -> None:
    got: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    async def agent(req: dict[str, Any]) -> None:
        got.append(req)
        raise RuntimeError("a bad callback")

    async def event(obj: dict[str, Any]) -> None:
        events.append(obj)

    client = S.TrouterClient(settings, {}, "sk", {}, "sid", "ep", directory, on_event=event, on_agent=agent)
    client._ws = _Ws()
    req = {
        "id": 3,
        "url": f"/v4/f/s{S.AGENT_PATH}e/t/call/acceptance/",
        "headers": {"MS-CV": "x"},
        "body": "",
    }
    with capture_logs() as logs:
        await client._handle_frame(f"3:::{json.dumps(req)}")
        await client._handle_frame('3:::{"id":4,"url":"/v4/f/s/messaging","body":""}')
    assert got == [req] and events == []
    assert json.loads(client._ws.sent[0][4:])["headers"]["MS-CV"] == "x.0"
    assert client._ws.sent[1] == '3:::{"id":4,"status":200,"body":""}'  # others keep the plain ack
    assert [e["event"] for e in logs] == ["agent_hook_failed"]


# --- the call, against fakes ---


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
        await kw["on_session"]("ep", SURL)
        await asyncio.Event().wait()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    monkeypatch.setattr(C, "token_source", lambda _s: FakeTokens())
    monkeypatch.setattr(
        C,
        "exchange_skype_token",
        lambda _s, _t: {"skype_token": "sk-token", "expires_in": 3600, "conversation_url": CONV},
    )
    return Server()


@pytest.fixture
def make_call(
    settings: Settings, directory: Directory, server: Server
) -> Iterator[Callable[..., tuple[C.Call, FakeIce, list[tuple[int, npt.NDArray[np.int16], float]]]]]:
    made: list[C.Call] = []

    def build(ice: FakeIce | None = None, capture: Path | None = None) -> Any:
        heard: list[tuple[int, npt.NDArray[np.int16], float]] = []
        conn = ice or FakeIce()
        call = C.Call(
            settings,
            payload(),
            lambda msi, pcm, t: heard.append((msi, pcm, t)),
            directory=directory,
            http=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
            ice=lambda: conn,
            trouter=server.trouter,
            capture=capture,
            clock=lambda: 1000.0,
        )
        server.call = call
        made.append(call)
        return call, conn, heard

    yield build


async def until(cond: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError(f"{what} not met")
        await asyncio.sleep(0.01)


async def test_join_hear_renegotiate_and_leave(make_call: Any, server: Server, tmp_path: Path) -> None:
    """044-AC1, AC4: phase 1 with meetingInfo, phase 2 with our offer, acceptance acknowledged and the
    callbacks registered with fresh message ids, ICE, SRTP, per-speaker audio, renegotiation answered and
    rekeyed, roster names, silence out as PCMU, DELETE on the call leg."""
    call, ice, heard = make_call()
    await call.join()
    steps = [server.step(r) for r in server.requests]
    assert steps == ["phase1", "phase2", "ack", "register"]
    ids = [r.headers["x-microsoft-skype-message-id"] for r in server.requests]
    assert len(set(ids)) == 4 and {r.headers["Authorization"] for r in server.requests} == {
        "Bearer ic3-token"
    }
    assert {r.headers["x-microsoft-skype-chain-id"] for r in server.requests} == {call._chain}
    p1, p2 = server.body("phase1"), server.body("phase2")
    assert p1["meetingInfo"] == payload()["meetingInfo"] and "callInvitation" not in p1
    assert p1["groupChat"] == {"threadId": THREAD, "messageId": "0"}
    assert p2["conversationRequest"]["roster"]["type"] == "Full" and "preheatProperties" not in json.dumps(p2)
    assert (
        p2["participants"]["from"]["id"] == ME and p2["participants"]["from"]["displayName"] == "Me Synthetic"
    )
    tc = p2["conversationRequest"]["roster"]["rosterUpdate"]
    assert tc == f"{SURL}callAgent/{call.endpoint}/{call._tag}/conversation/rosterUpdate/"
    ours = p2["callInvitation"]["mediaContent"]["blob"]
    assert ours == call._offer and "m=audio 5000 RTP/SAVP 110 111 0" in ours
    assert set(server.body("ack")) == {"callAcceptanceAcknowledgement"}
    assert set(server.body("register")) == {"callAcceptanceAcknowledgement", "callParticipantUpdate"}
    assert server.body("ack")["callAcceptanceAcknowledgement"]["links"]["call"].endswith(
        "call/updateMediaDescriptions"
    )
    assert (ice.remote_username, ice.remote_password) == ("Ab+c", SERVER_PWD)
    assert [c.port for c in ice.remote if c is not None] == [3479] and ice.remote[-1] is None  # UDP only
    assert call.roster.own == 4242 and call.roster.me == ME and call.leg == LEG
    assert server.trouter_kwargs["epid"] != call.endpoint and server.trouter_kwargs["prompt"] is False
    assert await server.trouter_kwargs["on_event"]({}) is None

    # Audio: two speakers in one packet, an RTCP packet, garbage, a mixed (PCMU) packet, a bad payload.
    send = tx(KEY_B)
    a, b = g722(0.04, 440.0), g722(0.04, 880.0)
    ice.incoming.put_nowait(bytes([0x80, 200]) + b"\x00" * 30)
    ice.incoming.put_nowait(b"\x80\x6e" + b"\x00" * 40)
    ice.incoming.put_nowait(send.protect(rtp(0, 1, 160, 8, b"\xff" * 160)))
    ice.incoming.put_nowait(send.protect(rtp(110, 1, 0, 7, multichannel((201, a[0]), (830, b[0])))))
    ice.incoming.put_nowait(send.protect(rtp(110, 2, 960, 7, multichannel((830, b[1])))))
    bad = bytearray(rtp(110, 3, 1920, 7, b"x" * 20))
    bad[0] |= 0x20
    bad[-1] = 0xFF  # padding past the header once decrypted
    ice.incoming.put_nowait(send.protect(bytes(bad)))
    await until(lambda: call.stats["malformed"] == 1, "packets read")
    assert [(m, p.shape, t) for m, p, t in heard] == [
        (201, (320,), 1000.0),
        (830, (320,), 1000.0),
        (830, (320,), 1000.02),
    ]
    assert call.stats == {
        "packets": 3,
        "rtcp": 1,
        "undecryptable": 1,
        "malformed": 1,
        "undecodable": 0,
        "blocks": 3,
    }

    # Renegotiation: answered with their extra m-lines refused, then the receiving side rekeyed.
    server.deliver(
        "call/mediaRenegotiation/",
        {
            "mediaNegotiation": {
                "mediaContent": {"blob": server_sdp(KEY_C) + VIDEO_AND_DATA, "mediaLegId": "leg-x"},
                "links": {"mediaAnswer": ANSWER_URL},
            }
        },
    )
    await until(lambda: call._remote_key == KEY_C, "rekeyed")
    answer = server.body("answer")["mediaAnswer"]["mediaContent"]
    assert answer["mediaLegId"] == "leg-x" and answer["blob"] == C.renegotiation_answer(
        ours, server_sdp(KEY_C) + VIDEO_AND_DATA
    )
    ice.incoming.put_nowait(send.protect(rtp(110, 4, 2880, 7, multichannel((201, a[1])))))  # old key
    ice.incoming.put_nowait(tx(KEY_C).protect(rtp(110, 5, 3840, 7, multichannel((201, a[1])))))
    await until(lambda: len(heard) == 4, "audio after the new key")
    assert call.stats["undecryptable"] == 2

    # A resent acceptance is acknowledged again; the roster names speakers (gzipped body, as Teams sends).
    server.deliver("call/acceptance/", server.accept_after_phase2)
    server.deliver("conversation/rosterUpdate/", roster(5, ME_ALL, OTHER_ONE), gzip_it=True)
    server.deliver("call/csrcInfo/", {"x": 1})
    await until(lambda: [server.step(r) for r in server.requests].count("ack") == 2, "second ack")
    await until(lambda: call.roster.names.get(830) == ("Other Synthetic", OTHER), "roster")
    assert call.roster.mine == {201, 414, 627}

    # Silence out: PCMU zeros from the SSRC the offer declared, every 20 ms.
    await until(lambda: len(ice.sent) >= 2, "silence sent")
    key = C.parse_media(ours).key
    out = C.parse_rtp(C.srtp_session(key, inbound=True).unprotect(ice.sent[0]))
    assert out is not None and (out.pt, out.ssrc, out.payload) == (0, call._ssrc, b"\xff" * 160)
    assert f"a=x-ssrc-range:{call._ssrc}-{call._ssrc}" in ours

    assert await call.leave() == 202
    delete = server.requests[-1]
    assert server.step(delete) == "delete" and str(delete.url) == LEG
    assert delete.headers["Authorization"] == "Bearer ic3-token" and delete.headers["X-Skypetoken"] == "sk"
    assert ice.closed and call._trouter is not None and call._trouter.done()


async def test_the_call_ends_on_its_end_callbacks(make_call: Any, server: Server) -> None:
    """044-AC5: a callEnd, or the conversation's end, ends the call for us."""
    call, ice, _ = make_call()
    await call.join()
    server.deliver("call/end/", {"callEnd": {"code": 0, "subCode": 5003, "phrase": "conversation has ended"}})
    await asyncio.wait_for(call.ended.wait(), 5)
    assert call.end_reason == "call/end: 5003 conversation has ended"
    server.deliver("conversation/conversationEnd/", {"code": 0, "subCode": 5010, "phrase": "only one left"})
    await asyncio.sleep(0.05)
    assert call.end_reason.startswith("call/end")  # the first reason stays
    await call.leave()


async def test_a_bad_block_or_a_failing_feed_costs_only_its_packet(make_call: Any) -> None:
    """An empty block, then a feed that raises once: reception goes on for every speaker."""
    call, ice, heard = make_call()
    fail = [True]

    def on_audio(msi: int, pcm: Any, t: float) -> None:
        if fail and msi == 414:
            fail.clear()
            raise RuntimeError("the session broke once")
        heard.append((msi, pcm, t))

    call.on_audio = on_audio
    await call.join()
    send, a = tx(KEY_B), g722(0.08)
    ice.incoming.put_nowait(
        send.protect(rtp(110, 1, 0, 7, multichannel((201, a[0]), (201, b""), (830, a[1]))))
    )
    ice.incoming.put_nowait(send.protect(rtp(110, 2, 960, 7, multichannel((201, a[1]), (830, a[2])))))
    ice.incoming.put_nowait(send.protect(rtp(110, 3, 1920, 7, multichannel((414, a[2])))))
    ice.incoming.put_nowait(send.protect(rtp(110, 4, 2880, 7, multichannel((414, a[3]), (201, a[2])))))
    with capture_logs() as logs:
        await until(lambda: len(heard) == 6, "every other block heard")
    assert [m for m, _, _ in heard] == [201, 830, 201, 830, 414, 201]
    assert call.stats["undecodable"] == 1 and not call.ended.is_set()
    assert [e["event"] for e in logs] == ["call_packet_failed"]
    await call.leave()
    assert not call.ended.is_set()  # leave()'s own cancel is no call end


async def test_a_receiver_that_dies_ends_the_call(make_call: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A follower must never sit joined and deaf: any receiver exit ends the call."""
    call, ice, _ = make_call()
    await call.join()

    async def broken() -> bytes:
        raise OSError("socket gone")

    monkeypatch.setattr(ice, "recv", broken)
    ice.incoming.put_nowait(b"")  # wakes the pending recv; the next one raises
    await asyncio.wait_for(call.ended.wait(), 5)
    assert call.end_reason == "media: receiver failed"
    await call.leave()


async def test_silence_from_the_server_ends_the_call(make_call: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """With consent checks off, a dead path is seen by nothing arriving, not even RTCP."""
    monkeypatch.setattr(C, "MEDIA_IDLE_S", 0.05)
    call, _, _ = make_call()
    await call.join()
    await asyncio.wait_for(call.ended.wait(), 5)
    assert call.end_reason == "media: nothing received"
    await call.leave()


async def test_media_loss_ends_the_call(make_call: Any) -> None:
    call, ice, _ = make_call()
    await call.join()
    ice.incoming.put_nowait(None)
    await asyncio.wait_for(call.ended.wait(), 5)
    assert call.end_reason == "media: ICE connection lost"
    await call.leave()


async def test_a_renegotiation_before_the_media_keys_it(make_call: Any, server: Server) -> None:
    """The server's renegotiation may land while ICE runs: its key wins over the first answer's."""
    call, ice, heard = make_call()
    server.accept_after_phase2 = None
    joining = asyncio.ensure_future(call.join())
    await until(lambda: "phase2" in [server.step(r) for r in server.requests], "phase 2")
    nego = {"mediaContent": {"blob": server_sdp(KEY_C)}, "links": {"mediaAnswer": ANSWER_URL}}
    server.deliver("call/mediaRenegotiation/", {"mediaNegotiation": nego})
    await until(lambda: call._remote_key == KEY_C, "rekeyed before the acceptance")
    server.deliver(
        "call/acceptance/", {"callAcceptance": {"links": {}, "mediaContent": {"blob": server_sdp(KEY_B)}}}
    )
    await joining
    assert call._remote_key == KEY_C and call.leg == ""  # no leg: nothing registered, nothing to DELETE
    ice.incoming.put_nowait(tx(KEY_C).protect(rtp(110, 1, 0, 7, multichannel((201, g722()[0])))))
    await until(lambda: len(heard) == 1, "audio on the renegotiated key")
    assert await call.leave() is None
    assert [server.step(r) for r in server.requests] == ["phase1", "phase2", "answer"]


@pytest.mark.parametrize(
    ("nego", "status", "reason"),
    [
        ({"mediaContent": {}}, 202, "renegotiation: ValueError"),  # nothing to answer
        (
            {"mediaContent": {"blob": "v=0\r\n"}, "links": {"mediaAnswer": ANSWER_URL}},
            202,
            "renegotiation: ValueError",
        ),
        (
            {"mediaContent": {"blob": server_sdp(KEY_C)}, "links": {"mediaAnswer": ANSWER_URL}},
            500,
            "renegotiation: JoinError",
        ),
    ],
)
async def test_a_failed_renegotiation_ends_the_call(
    make_call: Any, server: Server, nego: dict[str, Any], status: int, reason: str
) -> None:
    """Unanswered or unkeyed, the mixer's audio never reaches us: the follower must not sit deaf."""
    call, _, _ = make_call()
    await call.join()
    server.status["answer"] = status
    server.deliver("call/mediaRenegotiation/", {"mediaNegotiation": nego})
    await asyncio.wait_for(call.ended.wait(), 5)
    assert call.end_reason == reason and call._remote_key == KEY_B  # never keyed with what failed
    await call.leave()


async def test_capture_dumps_rtp_callbacks_and_redacted_sdp(
    make_call: Any, server: Server, tmp_path: Path
) -> None:
    """044-AC12: audio is written only under --capture, in S18's layout; SDP keys never are."""
    call, ice, _ = make_call(capture=tmp_path / "cap")
    cap = tmp_path / "cap" / call.endpoint
    await call.join()
    ice.incoming.put_nowait(bytes([0x80, 201, 0, 1]) + b"\x00" * 26)  # an (S)RTCP receiver report
    ice.incoming.put_nowait(tx(KEY_B).protect(rtp(110, 1, 0, 7, multichannel((201, g722()[0])))))
    server.deliver("conversation/rosterUpdate/", roster(1, OTHER_ONE))
    nego = {
        "mediaContent": {"blob": server_sdp(KEY_C) + VIDEO_AND_DATA},
        "links": {"mediaAnswer": ANSWER_URL},
    }
    server.deliver("call/mediaRenegotiation/", {"mediaNegotiation": nego})
    await until(lambda: call.stats["blocks"] == 1 and call.roster.seq == 1, "captured")
    await until(lambda: call._remote_key == KEY_C, "renegotiated")
    await call.leave()
    names = sorted(p.name for p in cap.iterdir())
    assert names == sorted(
        [
            "answer.sdp",
            "offer.sdp",
            "phase1.json",
            "phase2.json",
            "acknowledge.json",
            "register.json",
            "reneg-offer.sdp",
            "reneg-answer.sdp",
            "renegotiate.json",
            "rtcp.jsonl",
            "rtp.jsonl",
            "ssrc-7.pay",
            "trouter.jsonl",
        ]
    )
    for sdp in ("offer.sdp", "answer.sdp", "reneg-offer.sdp", "reneg-answer.sdp", "trouter.jsonl"):
        assert "inline:<redacted>" in (cap / sdp).read_text()
    keys = [b64(k).encode() for k in (KEY_A, KEY_B, KEY_C)]
    leaks = [(f.name, k) for f in cap.iterdir() for k in keys if k in f.read_bytes()]
    assert leaks == []  # no SRTP master key in any file, the callbacks' SDP blobs included
    rtcp = json.loads((cap / "rtcp.jsonl").read_text())
    assert (rtcp["pt"], rtcp["len"]) == (201, 30)
    row = json.loads((cap / "rtp.jsonl").read_text())
    assert (row["ssrc"], row["seq"], row["pt"], row["plen"]) == (
        7,
        1,
        110,
        len(multichannel((201, g722()[0]))),
    )
    assert (cap / "ssrc-7.pay").read_bytes() == multichannel((201, g722()[0]))
    trouter = [json.loads(x) for x in (cap / "trouter.jsonl").read_text().splitlines()]
    assert [r["path"] for r in trouter] == [
        "call/acceptance",
        "conversation/rosterUpdate",
        "call/mediaRenegotiation",
    ]


async def test_each_call_of_one_follow_captures_into_its_own_dir(settings: Settings, tmp_path: Path) -> None:
    """A retry or a re-attach builds another Call on the same capture dir: its files never overwrite the
    first one's (both write offer.sdp, phase1.json...)."""
    made = [C.Call(settings, payload(), lambda *_a: None, capture=tmp_path / "cap") for _ in range(2)]
    for c in made:
        assert c.capture is not None
        c.capture.file("offer.sdp", c.endpoint)
        await c.leave()
    offers = {p.parent.name: p.read_text() for p in (tmp_path / "cap").glob("*/offer.sdp")}
    assert offers == {c.endpoint: c.endpoint for c in made} and len(offers) == 2


async def test_a_mixed_codec_answer_is_logged(make_call: Any, server: Server) -> None:
    """Without x-multichannel the server mixes every voice into one stream: no per-speaker lines."""
    accepted = server.accept_after_phase2
    assert accepted is not None
    accepted["callAcceptance"]["mediaContent"]["blob"] = server_sdp(KEY_B, codec=0)
    call, _, _ = make_call()
    with capture_logs() as logs:
        await call.join()
    assert {"event": "call_codec_not_multichannel", "codec": 0, "log_level": "warning"} in logs
    await call.leave()


async def test_no_capture_writes_nothing(make_call: Any, tmp_path: Path) -> None:
    """044-AC12"""
    call, ice, _ = make_call()
    await call.join()
    await call.leave()
    written = [p.name for p in tmp_path.rglob("*") if p.is_file() and p.parent.name != "cfg"]
    assert call.capture is None and written == []  # cfg/: the directory's names.db


def one_to_one() -> dict[str, Any]:
    """A 1:1 push (seen live): a controller, the participants, neither groupContext nor meetingInfo."""
    return {
        "correlationId": CALL,
        "conversationController": CONTROLLER,
        "userJoinTime": "2026-01-01T00:00:00Z",
        "participants": [OTHER, ME],
        "additionalData": {"participantList": [{"id": OTHER}, {"id": ME}]},
    }


def bare_call(settings: Settings, directory: Directory, server: Server, push: dict[str, Any]) -> C.Call:
    ice = FakeIce()
    call = C.Call(
        settings,
        push,
        lambda *_a: None,
        directory=directory,
        http=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        ice=lambda: ice,
        trouter=server.trouter,
    )
    server.call = call
    return call


@pytest.mark.parametrize(
    "push",
    [
        one_to_one(),
        {**one_to_one(), "groupContext": None, "meetingInfo": None},
        {**payload(), "meetingInfo": None, "conversationController": CONTROLLER},
        {
            **payload(),
            "groupContext": {"threadId": "", "messageId": "0"},
            "conversationController": CONTROLLER,
        },
    ],
)
async def test_a_1to1_push_joins_its_live_call_on_its_controller(
    settings: Settings, directory: Directory, server: Server, push: dict[str, Any]
) -> None:
    """044-AC2: no phase 1 (without meetingInfo it creates an ad hoc call of ours), phase 2 straight on the
    push's controller, without groupChat nor meetingInfo (null, the controller answers 400; seen live)."""
    call = bare_call(settings, directory, server, push)
    await call.join()
    assert [server.step(r) for r in server.requests] == ["phase2", "ack", "register"]
    p2 = server.body("phase2")
    assert "groupChat" not in p2 and "meetingInfo" not in p2 and p2["groupContext"] is None
    assert p2["callInvitation"]["mediaContent"]["blob"] == call._offer
    assert p2["participants"]["from"]["id"] == ME and p2["participants"]["to"] == []
    assert await call.leave() == 202


@pytest.mark.parametrize(
    "push",
    [{k: v for k, v in one_to_one().items() if k != "conversationController"}, {"correlationId": CALL}],
)
async def test_a_push_naming_no_call_places_none(
    settings: Settings, directory: Directory, server: Server, push: dict[str, Any]
) -> None:
    """Neither a meeting nor a controller: nothing to attach to, nothing sent, nothing to retry."""
    call = bare_call(settings, directory, server, push)
    with pytest.raises(C.JoinError) as err:
        await asyncio.wait_for(call.join(), 5)
    await call.leave()
    assert str(err.value) == "push names no meeting nor conversation controller" and not err.value.retry
    assert server.requests == [] and server.trouter_kwargs == {}


async def test_only_a_gone_1to1_is_not_worth_a_retry(
    settings: Settings, directory: Directory, server: Server, make_call: Any
) -> None:
    """044-AC14: a 1:1 controller answers 404 once its call is over (seen live, subCode 5907); a meeting's
    404 or a 1:1's other refusals stay retried."""
    server.status["phase2"] = 404
    for push, retry in ((one_to_one(), False), (payload(), True)):
        call = bare_call(settings, directory, server, push)
        with pytest.raises(C.JoinError) as err:
            await call.join()
        await call.leave()
        assert (str(err.value), err.value.retry) == ("phase2 answered 404", retry)
    server.status["phase2"] = 400
    call = bare_call(settings, directory, server, one_to_one())
    with pytest.raises(C.JoinError) as err:
        await call.join()
    await call.leave()
    assert err.value.retry


@pytest.mark.parametrize(
    ("path", "body", "retry"),
    [
        ("call/end/", {"callEnd": {"code": 0, "subCode": 5003, "phrase": "conversation has ended"}}, False),
        ("conversation/conversationEnd/", {"code": 0, "subCode": 5010, "phrase": "ended"}, False),
        ("call/end/", {"sessionRejection": {"code": 403, "subCode": 10065, "phrase": "refused"}}, True),
    ],
)
async def test_a_call_over_during_the_join_is_not_retried(
    make_call: Any, server: Server, path: str, body: dict[str, Any], retry: bool
) -> None:
    """044-AC14: the call ended while we joined: a retry's phase 1 would start a new call as me. A
    rejection (the lobby refusing us) is still retried."""
    server.accept_after_phase2 = None
    call = make_call()[0]
    joining = asyncio.ensure_future(call.join())
    await until(lambda: "phase2" in [server.step(r) for r in server.requests], "in the lobby")
    server.deliver(path, body)
    with pytest.raises(C.JoinError) as err:
        await asyncio.wait_for(joining, 5)
    assert err.value.retry is retry
    await call.leave()


async def test_a_known_controller_skips_phase_1_and_a_404_there_is_final(
    settings: Settings, directory: Directory, server: Server
) -> None:
    """A re-attach or a retry on a meeting goes straight to the controller the first join reached, with the
    meeting body (seen live: meetings and group calls accept it); its 404 is not retried."""
    call = bare_call(settings, directory, server, payload())
    call.controller, call._direct = CONTROLLER, True
    await call.join()
    assert [server.step(r) for r in server.requests] == ["phase2", "ack", "register"]
    p2 = server.body("phase2")
    assert p2["meetingInfo"] == payload()["meetingInfo"] and p2["groupChat"]["threadId"] == THREAD
    await call.leave()
    server.status["phase2"] = 404
    call = C.Call(
        settings,
        payload(),
        lambda *_a: None,
        directory=directory,
        http=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        ice=FakeIce,
        trouter=server.trouter,
        controller=CONTROLLER,
    )
    server.call = call
    with pytest.raises(C.JoinError) as err:
        await call.join()
    await call.leave()
    assert not err.value.retry


async def test_phase_1_names_the_controller_a_later_join_reuses(make_call: Any, server: Server) -> None:
    call = make_call()[0]
    await call.join()
    assert call.controller == CONTROLLER
    await call.leave()


@pytest.mark.parametrize(
    ("path", "body", "retryable"),
    [
        ("call/end/", {"callEnd": {"code": 0, "subCode": 5003, "phrase": "conversation has ended"}}, False),
        ("conversation/conversationEnd/", {"code": 0, "subCode": 5010, "phrase": "ended"}, False),
        ("call/end/", {"sessionRejection": {"code": 403, "subCode": 10065, "phrase": "refused"}}, True),
    ],
)
async def test_the_first_end_keeps_its_verdict_after_the_acceptance(
    make_call: Any, server: Server, path: str, body: dict[str, Any], retryable: bool
) -> None:
    """044-AC14: an end delivered once accepted (the join still on its ICE step) is remembered: a failure
    after it is not retried when the call is over; a later end never changes the first verdict."""
    call = make_call()[0]
    await call.join()
    server.deliver(path, body)
    await until(call.ended.is_set, "ended")
    server.deliver("call/end/", {"sessionRejection": {"code": 403, "subCode": 1, "phrase": "x"}})
    await asyncio.sleep(0.05)
    assert call.retryable is retryable
    await call.leave()


async def test_a_fired_leave_and_a_reconnect_keep_their_verdicts(joined: Any) -> None:
    call, roster_of = await joined()
    await roster_of(me(OWN), OTHER_ONE)
    await asyncio.wait_for(call.ended.wait(), 2)
    assert (call.end_reason, call.retryable) == ("last_device_left", False)
    await call.leave()
    call, _ = await joined()
    await call._on_session("ep", SURL)
    assert (call.end_reason, call.retryable) == (C.TROUTER_MOVED, True)
    await call.leave()


async def test_a_trouter_that_stops_after_the_join_ends_the_call(make_call: Any, server: Server) -> None:
    """run_forever returns on a dead login: the follower would sit in the call deaf as me. It ends, for
    good (a new join would sign in with the same dead login)."""
    stopping = asyncio.Event()

    async def trouter(settings: Settings, **kw: Any) -> None:
        await kw["on_session"]("ep", SURL)
        await stopping.wait()

    call = make_call()[0]
    call._trouter_fn = trouter
    await call.join()
    assert not call.ended.is_set()
    stopping.set()
    await asyncio.wait_for(call.ended.wait(), 2)
    assert (call.end_reason, call.retryable) == ("trouter: stopped", False)
    await call.leave()
    call = make_call()[0]
    await call.join()
    await call.leave()  # its own cancel at leave() is not a stop
    assert call.end_reason == ""


async def test_a_trouter_that_stops_in_the_lobby_fails_the_join_for_good(
    make_call: Any, server: Server
) -> None:
    server.accept_after_phase2 = None
    stopping = asyncio.Event()

    async def trouter(settings: Settings, **kw: Any) -> None:
        await kw["on_session"]("ep", SURL)
        await stopping.wait()

    call = make_call()[0]
    call._trouter_fn = trouter
    joining = asyncio.ensure_future(call.join())
    await until(lambda: "phase2" in [server.step(r) for r in server.requests], "in the lobby")
    stopping.set()
    with pytest.raises(C.JoinError) as err:
        await asyncio.wait_for(joining, 5)
    assert (str(err.value), err.value.retry) == ("trouter: stopped", False)
    await call.leave()


class SlowIce(FakeIce):
    """ICE that takes a moment: callbacks land between the acceptance and join() returning."""

    def __init__(self, during: Callable[[], None]) -> None:
        super().__init__()
        self.during = during

    async def connect(self) -> None:
        self.during()
        await asyncio.sleep(0.1)


async def test_my_last_device_gone_before_join_returns_fails_it_for_good(
    make_call: Any, server: Server, settings: Settings
) -> None:
    """The leave fired between the acceptance and join() returning: no `join: ok`, no engines, no retry."""
    settings.follow_leave_grace_s = 0.01
    call = make_call(SlowIce(lambda: None))[0]
    original = server.deliver

    def roster_first(path: str, body: Any, gzip_it: bool = False) -> None:
        if path == "call/acceptance/":
            original("conversation/rosterUpdate/", roster(1, me(OWN), OTHER_ONE))
        original(path, body, gzip_it)

    server.deliver = roster_first  # type: ignore[method-assign]
    with pytest.raises(C.JoinError) as err:
        await call.join()
    assert await call.leave() == 202  # the leg the acceptance named is hung up
    assert (str(err.value), err.value.retry) == ("last_device_left", False)


async def test_a_renegotiation_failing_before_join_returns_fails_it_for_a_retry(
    make_call: Any, server: Server
) -> None:
    def renegotiate() -> None:
        nego = {"mediaContent": {"blob": ""}, "links": {"mediaAnswer": ANSWER_URL}}  # nothing to answer
        server.deliver("call/mediaRenegotiation/", {"mediaNegotiation": nego})

    call = make_call(SlowIce(renegotiate))[0]
    with pytest.raises(C.JoinError) as err:
        await call.join()
    assert await call.leave() == 202
    assert (str(err.value), err.value.retry) == ("renegotiation: ValueError", True)


@pytest.mark.parametrize(
    ("target", "shown"),
    [
        ("https://TOKEN.cc.invalid/x", "<str, 26 chars>"),
        ("TOKEN://cc.invalid", "<str, 18 chars>"),
        ("https://cc.invalid\nTOKEN/x", "<str, 26 chars>"),
        ("cc.invalid/cc/v1/TOKEN?x=TOKEN", "<str, 30 chars>"),
        ("http://[x/TOKEN?token=TOKEN", "<str, 27 chars>"),
        ({"token": "TOKEN"}, "<dict, 18 chars>"),
        (["TOKEN"], "<list, 9 chars>"),
    ],
)
async def test_a_redirection_is_logged_not_followed(
    make_call: Any, server: Server, target: Any, shown: str
) -> None:
    """A peer-supplied target may carry a token anywhere, host included: only its shape is logged."""
    call = make_call()[0]
    await call.join()
    with capture_logs() as logs:
        server.deliver("call/redirection/", {"redirection": {"target": target}})
        await until(lambda: any(e["event"] == "call_redirection" for e in logs), "logged")
    (event,) = [e for e in logs if e["event"] == "call_redirection"]
    assert (event["target"], event["log_level"]) == (shown, "warning")
    assert "TOKEN" not in str(logs) and "path" not in event
    assert not call.ended.is_set()


async def test_a_redirection_target_at_the_top_level_is_read_too(make_call: Any, server: Server) -> None:
    call = make_call()[0]
    await call.join()
    with capture_logs() as logs:
        server.deliver("call/redirection/", {"target": "https://cc.invalid/leg?token=TOKEN"})
        await until(lambda: any(e["event"] == "call_redirection" for e in logs), "logged")
    assert [e["target"] for e in logs if e["event"] == "call_redirection"] == ["<str, 34 chars>"]


# --- 044-AC4: the leave rule ---

ME_B, ME_C, OWN = 414, 1256, 4242  # OWN: server_sdp's x-source-streamid, our audio MSI


def me(*audio: int) -> tuple[str, str, list[tuple[str, int]]]:
    """My roster entry: one main-audio (plus a video) per endpoint."""
    return ME, "Me Synthetic", [s for a in audio for s in (("main-audio", a), ("main-video", a + 1))]


@pytest.fixture
def joined(make_call: Any, server: Server, settings: Settings) -> Any:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))  # info events
    settings.follow_leave_grace_s = 0.1
    seq = iter(range(1, 1000))

    async def build() -> tuple[C.Call, Callable[..., Any]]:
        call = make_call()[0]
        await call.join()

        async def roster_of(*people: Any) -> None:
            n = call.roster.seq
            server.deliver("conversation/rosterUpdate/", roster(next(seq), *people))
            await until(lambda: call.roster.seq > n, "roster applied")

        return call, roster_of

    return build


async def test_one_of_two_devices_leaving_keeps_the_follower_the_last_one_ends_it(joined: Any) -> None:
    """044-AC4: PC and phone in, the PC leaves: the follower stays; the phone leaves too: it leaves after
    the grace, with the reason logged."""
    call, roster_of = await joined()
    await roster_of(me(ME_B, ME_C, OWN), OTHER_ONE)
    await roster_of(me(ME_C, OWN), OTHER_ONE)
    await asyncio.sleep(0.25)  # past the grace
    assert not call.ended.is_set()
    with capture_logs() as logs:
        await roster_of(me(OWN), OTHER_ONE)
        assert not call.ended.is_set()  # the grace runs first
        await asyncio.wait_for(call.ended.wait(), 2)
        assert call.end_reason == "last_device_left"
        await call.leave()
    events = [(e["event"], e.get("reason")) for e in logs]
    assert ("call_last_device_gone", None) in events and ("call_ended", "last_device_left") in events
    assert ("call_left", "last_device_left") in events


async def test_a_device_back_within_the_grace_cancels_the_leave(joined: Any) -> None:
    """044-AC4: a transfer, B leaves and C joins a moment later: the pending leave is dropped."""
    call, roster_of = await joined()
    await roster_of(me(ME_B, OWN), OTHER_ONE)
    await roster_of(me(OWN), OTHER_ONE)
    with capture_logs() as logs:
        await roster_of(me(ME_C, OWN), OTHER_ONE)
    await asyncio.sleep(0.25)
    assert not call.ended.is_set() and "call_device_back" in [e["event"] for e in logs]
    await roster_of(me(OWN), OTHER_ONE)  # C leaves: a new grace, then the leave
    await asyncio.wait_for(call.ended.wait(), 2)
    await call.leave()


async def test_a_roster_with_only_our_stream_leaves_after_the_grace(joined: Any) -> None:
    """044-AC4: no device of mine beside us, even in the first roster: the grace absorbs a roster that lags
    my device, then the follower leaves (never sits in the call as me alone)."""
    call, roster_of = await joined()
    await roster_of(me(OWN), OTHER_ONE)
    await asyncio.wait_for(call.ended.wait(), 2)
    assert call.end_reason == "last_device_left"
    await call.leave()
    call, roster_of = await joined()
    await roster_of(me(OWN), OTHER_ONE)
    await roster_of(me(ME_B, OWN), OTHER_ONE)  # my device shows a moment later: no leave
    await asyncio.sleep(0.25)
    assert not call.ended.is_set()
    await call.leave()


@pytest.mark.parametrize(
    "mine",
    [None, (ME, "Me Synthetic", []), (ME, "Me Synthetic", [("main-audio", ME_B + 7)])],
    ids=["no-entry", "no-streams", "without-us"],
)
async def test_a_roster_not_showing_us_proves_nothing(joined: Any, mine: Any) -> None:
    """044-AC4: a roster whose entry for me is missing or lacks our own stream is not a full view of my
    devices: ignored, never "last device gone", and no pending leave is cancelled by it either."""
    call, roster_of = await joined()
    await roster_of(me(ME_B, OWN), OTHER_ONE)
    await roster_of(*([mine] if mine else []), OTHER_ONE)
    await asyncio.sleep(0.25)
    assert not call.ended.is_set() and call._leaving is None
    await roster_of(me(OWN), OTHER_ONE)
    await roster_of(*([mine] if mine else []), OTHER_ONE)  # does not cancel the pending leave
    await asyncio.wait_for(call.ended.wait(), 2)
    await call.leave()


async def test_a_device_back_after_the_leave_fired_is_logged_too_late(joined: Any) -> None:
    call, roster_of = await joined()
    await roster_of(me(ME_B, OWN))
    await roster_of(me(OWN))
    await asyncio.wait_for(call.ended.wait(), 2)
    with capture_logs() as logs:
        await roster_of(me(ME_C, OWN))
        await roster_of(me(ME_C, OWN))
    events = [(e["event"], e.get("call")) for e in logs]
    assert events == [("call_device_back_too_late", CALL)] and call.end_reason == "last_device_left"
    await call.leave()


async def test_other_people_and_my_video_are_not_my_devices(joined: Any) -> None:
    """044-AC4: several participants, their audio and my non-audio streams never keep the follower."""
    call, roster_of = await joined()
    await roster_of(me(ME_B, OWN), OTHER_ONE)
    third = ("8:orgid:00000000-0000-4000-8000-0000000000d0", "Third", [("main-audio", 900)])
    await roster_of((ME, "Me Synthetic", [("main-audio", OWN), ("main-video", ME_B + 1)]), OTHER_ONE, third)
    await asyncio.wait_for(call.ended.wait(), 2)
    await call.leave()


async def test_a_roster_before_the_acceptance_counts_once_accepted(
    make_call: Any, server: Server, settings: Settings
) -> None:
    """Our own MSI comes with the acceptance: a roster that arrived before it is judged then."""
    settings.follow_leave_grace_s = 0.1
    call = make_call()[0]
    original = server.deliver

    def roster_first(path: str, body: Any, gzip_it: bool = False) -> None:
        if path == "call/acceptance/":
            original("conversation/rosterUpdate/", roster(1, me(ME_B, OWN), OTHER_ONE))
        original(path, body, gzip_it)

    server.deliver = roster_first  # type: ignore[method-assign]
    await call.join()
    server.deliver = original  # type: ignore[method-assign]
    assert call.roster.mine == {ME_B, OWN} and call._leaving is None  # judged once accepted: B is in
    server.deliver("conversation/rosterUpdate/", roster(2, me(OWN), OTHER_ONE))
    await asyncio.wait_for(call.ended.wait(), 2)
    await call.leave()


async def test_a_roster_with_only_us_before_the_acceptance_arms_the_grace_once_accepted(
    make_call: Any, server: Server, settings: Settings
) -> None:
    settings.follow_leave_grace_s = 0.1
    call = make_call()[0]
    original = server.deliver

    def roster_first(path: str, body: Any, gzip_it: bool = False) -> None:
        if path == "call/acceptance/":
            original("conversation/rosterUpdate/", roster(1, me(OWN), OTHER_ONE))
        original(path, body, gzip_it)

    server.deliver = roster_first  # type: ignore[method-assign]
    await call.join()
    server.deliver = original  # type: ignore[method-assign]
    await asyncio.wait_for(call.ended.wait(), 2)
    await call.leave()


async def test_a_stale_roster_or_one_before_our_msi_decides_nothing(make_call: Any, server: Server) -> None:
    call = make_call()[0]
    call.roster.me = ME
    url = f"{SURL}callAgent/{call.endpoint}/{call._tag}/conversation/rosterUpdate/"
    await call.on_agent({"url": url, "headers": {}, "body": json.dumps(roster(1, me(OWN)))})
    assert call.roster.mine == {OWN} and call._leaving is None  # own MSI unknown before the acceptance
    await call.on_agent({"url": url, "headers": {}, "body": json.dumps(roster(2, me(ME_B, OWN)))})
    call.roster.own = OWN
    await call.on_agent({"url": url, "headers": {}, "body": json.dumps(roster(0, me(OWN)))})
    assert call._leaving is None  # stale: not applied
    await call.leave()


async def test_leave_drops_a_pending_leave(joined: Any) -> None:
    call, roster_of = await joined()
    await roster_of(me(ME_B, OWN))
    await roster_of(me(OWN))
    timer = call._leaving
    assert timer is not None
    await call.leave()
    assert timer.cancelled()


async def test_persistent_feed_failures_are_logged_at_powers_of_two(make_call: Any) -> None:
    call, ice, _ = make_call()

    def broken(*_a: Any) -> None:
        raise RuntimeError("session gone")

    call.on_audio = broken
    await call.join()
    send, a = tx(KEY_B), g722(0.2)
    with capture_logs() as logs:
        for i in range(9):
            ice.incoming.put_nowait(send.protect(rtp(110, i, 960 * i, 7, multichannel((201, a[i])))))
        await until(lambda: call.stats["undecodable"] == 9, "every packet tried")
    assert [e["count"] for e in logs if e["event"] == "call_packet_failed"] == [1, 2, 4, 8]
    assert not call.ended.is_set()
    await call.leave()


def test_capture_files_redact_escaped_keys(tmp_path: Path) -> None:
    """Raw response bodies go through the same redaction, JSON escapes included."""
    cap = C.Capture(tmp_path / "cap")
    key = b64(bytes([0xFB, 0xFF] * 15))
    cap.file("phase2.json", f'{{"blob": "inline:{key.replace("/", chr(92) + "/")}|2^31"}}')
    cap.trouter("call/acceptance", {}, {"blob": f"inline:{key}|2^31"})
    cap.close()
    for name in ("phase2.json", "trouter.jsonl"):
        text = (tmp_path / "cap" / name).read_text()
        assert "inline:<redacted>" in text and key not in text and key.replace("/", "\\/") not in text


async def failed(call: C.Call) -> str:
    with pytest.raises(C.JoinError) as err:
        await call.join()
    await call.leave()
    return str(err.value)


async def test_join_failures_name_their_cause(
    make_call: Any, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC14: each failed join says why."""
    monkeypatch.setattr(C, "token_source", lambda _s: FakeTokens(fail=RuntimeError("no account")))
    assert await failed(make_call()[0]) == "sign-in: RuntimeError"
    monkeypatch.setattr(C, "token_source", lambda _s: FakeTokens(ic3=None))
    assert await failed(make_call()[0]) == "no IC3 token"
    monkeypatch.setattr(C, "token_source", lambda _s: FakeTokens())
    assert await failed(make_call(FakeIce(default=False))[0]) == "no local ICE candidate"
    assert await failed(make_call(FakeIce(connect_error=ConnectionError("ICE negotiation failed")))[0]) == (
        "ICE: ConnectionError"
    )
    server.status["phase2"] = 403
    assert await failed(make_call()[0]) == "phase2 answered 403"
    server.status.pop("phase2")
    server.phase1 = {"links": {}}
    assert await failed(make_call()[0]) == "phase1 named no conversation controller"
    server.phase1 = {"conversationResponse": {"conversationController": CONTROLLER}}
    server.accept_after_phase2 = {"callAcceptance": {"links": {}, "mediaContent": {"blob": "v=0\r\n"}}}
    assert await failed(make_call()[0]) == "answer SDP: no audio section"
    server.accept_after_phase2 = None
    call = make_call()[0]
    joining = asyncio.ensure_future(failed(call))
    await until(lambda: [server.step(r) for r in server.requests].count("phase2") == 4, "in the lobby")
    server.deliver("call/end/", {"sessionRejection": {"code": 403, "subCode": 10065, "phrase": "refused"}})
    assert await joining == "call/end: 10065 refused"


async def test_a_lobby_left_hangs_up_a_leg_named_by_phase_2(
    make_call: Any, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never admitted: a call leg phase 2 named is still deleted (the live answers named none)."""
    monkeypatch.setattr(C, "ACCEPT_TIMEOUT_S", 0.05)
    server.accept_after_phase2 = None
    original = server.handler

    async def with_leg(request: httpx.Request) -> httpx.Response:
        if server.step(request) == "phase2":
            server.requests.append(request)
            return httpx.Response(200, json={"links": {"callLeg": LEG}})
        return await original(request)

    call = make_call()[0]
    call.http = httpx.AsyncClient(transport=httpx.MockTransport(with_leg))
    assert await failed(call) == "not admitted (lobby timeout)"
    assert [server.step(r) for r in server.requests][-2:] == ["phase2", "delete"]


async def test_join_timeouts(make_call: Any, server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC14: no Trouter session, nobody admitting us."""
    monkeypatch.setattr(C, "STEP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(C, "ACCEPT_TIMEOUT_S", 0.05)
    server.accept_after_phase2 = None
    assert await failed(make_call()[0]) == "not admitted (lobby timeout)"

    async def silent(settings: Settings, **kw: Any) -> None:
        await asyncio.Event().wait()

    call = make_call()[0]
    call._trouter_fn = silent
    assert await failed(call) == "no Trouter session"


async def test_a_trouter_that_fails_names_why_at_once(
    make_call: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not 30 s later as "no Trouter session": the socket's own failure, or its stop on a dead login."""
    monkeypatch.setattr(C, "STEP_TIMEOUT_S", 30.0)

    async def broken(settings: Settings, **kw: Any) -> None:
        raise httpx.ConnectError("trouter down")

    async def signed_out(settings: Settings, **kw: Any) -> None:
        return None

    for fn, cause in ((broken, "Trouter: ConnectError"), (signed_out, "Trouter: stopped")):
        call = make_call()[0]
        call._trouter_fn = fn
        assert await asyncio.wait_for(failed(call), 5) == cause


@pytest.mark.parametrize("url", ["http://conv.invalid/conv/c1", "file:///etc/passwd", "https:///nohost"])
async def test_no_bearer_goes_to_a_non_https_url(make_call: Any, server: Server, url: str) -> None:
    """Server-given URLs get the IC3 bearer and the skype token only over https."""
    server.phase1 = {"conversationController": url}
    assert await failed(make_call()[0]) == "refused a non-https URL"
    assert "phase2" not in [server.step(r) for r in server.requests]


async def test_no_delete_to_a_non_https_leg(make_call: Any, server: Server) -> None:
    call, _, _ = make_call()
    await call.join()
    call.leg = "http://cc.invalid/cc/v1/active/leg1"
    with capture_logs() as logs:
        assert await call.leave() is None
    assert "delete" not in [server.step(r) for r in server.requests]
    assert {"event": "call_leave_failed", "error": "refused a non-https URL"} | {
        "error_type": "JoinError",
        "log_level": "warning",
    } in logs


async def test_no_conversation_service(make_call: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        C, "exchange_skype_token", lambda _s, _t: {"skype_token": "sk", "conversation_url": ""}
    )
    assert await failed(make_call()[0]) == "no conversation service in the authz answer"


async def test_a_failed_leave_is_logged_and_still_closes(make_call: Any, server: Server) -> None:
    """044-AC4: the DELETE status is reported; a refused or failed one is logged, never raised."""
    call, ice, _ = make_call()
    await call.join()
    server.status["delete"] = 404
    assert await call.leave() == 404
    call, ice, _ = make_call()
    await call.join()

    async def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    call.http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    with capture_logs() as logs:
        assert await call.leave() is None
    assert ice.closed and "call_leave_failed" in [e["event"] for e in logs]


async def test_a_token_failure_at_leave_still_closes_everything(
    make_call: Any, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-JoinError from the IC3 refresh (MSAL) is logged; ICE, Trouter, HTTP and capture still close."""
    call, ice, _ = make_call()
    await call.join()

    class Broken(FakeTokens):
        def ic3_token(self) -> str | None:
            raise RuntimeError("msal refresh failed")

    monkeypatch.setattr(C, "token_source", lambda _s: Broken())
    with capture_logs() as logs:
        assert await call.leave() is None
    assert ice.closed and call._trouter is not None and call._trouter.done() and call.http.is_closed
    assert ("call_leave_failed", "RuntimeError") in [(e["event"], e.get("error_type")) for e in logs]


async def test_a_failing_close_step_does_not_skip_the_next(make_call: Any) -> None:
    call, ice, _ = make_call()
    await call.join()

    async def broken() -> None:
        raise OSError("close failed")

    ice.close = broken  # type: ignore[method-assign]
    with capture_logs() as logs:
        await call.leave()
    assert call.http.is_closed and call._trouter is not None and call._trouter.done()
    assert ("call_close_failed", "ice") in [(e["event"], e.get("step")) for e in logs]


async def test_a_failed_background_request_is_logged(make_call: Any, server: Server) -> None:
    call, _, _ = make_call()
    await call.join()
    server.status["ack"] = 500
    with capture_logs() as logs:
        server.deliver("call/acceptance/", server.accept_after_phase2)
        await until(lambda: any(e["event"] == "call_task_failed" for e in logs), "logged")
    await call.leave()


async def test_an_unreadable_callback_body_is_dropped(make_call: Any, server: Server) -> None:
    call, _, _ = make_call()
    await call.join()
    url = f"{SURL}callAgent/{call.endpoint}/{call._tag}/conversation/rosterUpdate/"
    headers = {"x-microsoft-skype-content-encoding": "gzip"}
    await call.on_agent({"id": 1, "url": url, "headers": headers, "body": "not base64 gzip"})
    assert call.roster.seq == -1
    await call.leave()


@pytest.mark.parametrize("surl", ["https://trouter.invalid/v4/f/other/", SURL])
async def test_a_trouter_reconnect_ends_the_call_for_a_re_attach(make_call: Any, surl: str) -> None:
    """044-AC13: a second session is a reconnect: the callbacks sent meanwhile are lost (a new surl gets
    none at all), so the call ends with trouter_moved and follow() re-attaches; on the same surl too."""
    call, _, _ = make_call()
    await call.join()
    with capture_logs() as logs:
        await call._on_session("ep", surl)
    assert [e["event"] for e in logs] == ["call_trouter_moved"]
    assert call.ended.is_set() and call.end_reason == C.TROUTER_MOVED
    await call.leave()


async def test_a_reconnect_with_a_leave_pending_leaves_instead_of_re_attaching(joined: Any) -> None:
    """044-AC4: my last device already left: a re-attach would sit in the call as me for grace + join +
    grace, past 15 s. The call ends as last_device_left."""
    call, roster_of = await joined()
    call.settings.follow_leave_grace_s = 30.0
    await roster_of(me(ME_B, OWN), OTHER_ONE)
    assert not call.leaving
    await roster_of(me(OWN), OTHER_ONE)
    assert call.leaving
    await call._on_session("ep", SURL)
    assert call.ended.is_set() and call.end_reason == "last_device_left"
    await call.leave()
    assert not call.leaving  # the timer dropped with the hang-up


async def test_trouter_moving_during_the_join_fails_it_for_a_retry(make_call: Any, server: Server) -> None:
    server.accept_after_phase2 = None  # waiting in the lobby
    call = make_call()[0]
    joining = asyncio.ensure_future(call.join())
    await until(lambda: "phase2" in [server.step(r) for r in server.requests], "in the lobby")
    await call._on_session("ep", "https://trouter.invalid/v4/f/other/")
    with pytest.raises(C.JoinError) as err:
        await asyncio.wait_for(joining, 5)
    assert (str(err.value), err.value.retry) == (C.TROUTER_MOVED, True)
    await call.leave()


async def test_an_answer_without_our_msi_is_logged(make_call: Any, server: Server) -> None:
    """AC4's leave rule needs our own MSI: without it the follower can only end with the call."""
    accepted = server.accept_after_phase2
    assert accepted is not None
    accepted["callAcceptance"]["mediaContent"]["blob"] = server_sdp(KEY_B).replace(
        "a=x-source-streamid:4242\r\n", ""
    )
    call = make_call()[0]
    with capture_logs() as logs:
        await call.join()
    assert {"event": "call_no_own_msi", "call": CALL, "log_level": "warning"} in logs
    assert call.roster.own is None
    await call.leave()


@pytest.mark.parametrize("bad", ["stun.example.invalid", ":3478", "h:0", "h:65536", "h:x", "h:"])
def test_a_bad_stun_server_fails_at_startup(bad: str) -> None:
    with pytest.raises(ValueError, match="stun_server must be empty or host:port"):
        Settings(tenant_id="t", stun_server=bad)  # type: ignore[call-arg]


def test_stun_server_from_the_settings(settings: Settings) -> None:
    assert C.stun(settings) == ("stun.l.google.com", 19302)
    settings.stun_server = "stun.example.invalid:3478"
    assert C.stun(settings) == ("stun.example.invalid", 3478)
    settings.stun_server = ""
    assert C.stun(settings) is None  # host candidates only


async def test_the_default_ice_uses_the_stun_setting(settings: Settings) -> None:
    settings.stun_server = ""
    call = C.Call(settings, payload(), lambda *_a: None)
    conn = call._ice()
    assert conn.stun_server is None
    await conn.close()
    await call.http.aclose()


async def test_the_default_ice_runs_no_consent_checks(settings: Settings) -> None:
    """Their expiry hung up a call whose media still flowed (seen live)."""
    call = C.Call(settings, payload(), lambda *_a: None)
    conn = call._ice()
    assert isinstance(conn, C._Ice) and conn.ice_controlling
    assert await asyncio.wait_for(conn.query_consent(), 1) is None
    await conn.close()
    await call.http.aclose()


def test_callback_path_from_its_url() -> None:
    url = f"{SURL}callAgent/ep/1234abcd/call/acceptance/?x=1"
    assert C._tc_path(url) == "call/acceptance"


# --- the follower: join, transcribe, leave ---


class FakeSession:
    """Session's surface; refuses frames before start() ends, as the real one would lose them."""

    instances: list[FakeSession] = []
    start_error: BaseException | None = None
    start_hangs = False

    def __init__(
        self, settings: Settings, data_dir: Path, call: str, thread: str, resolve: Any, **kw: Any
    ) -> None:
        self.args, self.resolve, self.kw = (data_dir, call, thread), resolve, kw
        self.fed: list[tuple[int, int, float]] = []
        self.started = self.closed = False
        FakeSession.instances.append(self)

    async def start(self) -> None:
        # Audio arriving while the engines load: it must be held, never fed into this session yet.
        FakeCall.instances[-1].on_audio(202, np.zeros(320, np.int16), 0.5)
        await asyncio.sleep(0.02)
        if FakeSession.start_hangs:
            await asyncio.Event().wait()
        if FakeSession.start_error is not None:
            raise FakeSession.start_error
        self.started = True

    def feed(self, msi: int, pcm: Any, t: float) -> None:
        assert self.started and not self.closed, "fed outside start()..close()"
        self.fed.append((msi, len(pcm), t))

    async def close(self) -> None:
        self.closed = True


class FakeCall:
    """Call's surface; audio starts right after join() returns, as the receiver task does."""

    instances: list[FakeCall] = []
    join_error: BaseException | None = None
    join_errors: list[BaseException] = []  # one per join, before join_error
    join_hangs = False
    join_slow_cancel = False  # the first cancel is held: the join winds down slowly, as a real one may
    directory: Any = None

    def __init__(
        self,
        settings: Settings,
        payload: dict[str, Any],
        on_audio: Any,
        capture: Path | None = None,
        directory: Any = None,
        controller: str = "",
    ) -> None:
        self.on_audio, self.capture, self.controller = on_audio, capture, controller
        self.roster = C.Roster(me=ME)
        self.directory, self.directory_given = directory or FakeCall.directory, directory
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.retryable = True
        self.left = 0
        self.joined = False
        self.cancel_held = False
        FakeCall.instances.append(self)

    async def join(self) -> None:
        if FakeCall.join_errors:
            raise FakeCall.join_errors.pop(0)
        if FakeCall.join_error is not None:
            raise FakeCall.join_error
        if FakeCall.join_hangs:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not FakeCall.join_slow_cancel:
                    raise
                self.cancel_held = True
                await asyncio.Event().wait()
        self.joined = True
        asyncio.get_running_loop().call_soon(self.on_audio, 201, np.zeros(320, np.int16), 1.0)

    def move(self) -> None:
        """What Call does when its Trouter socket reconnects on another surl."""
        self.end_reason = C.TROUTER_MOVED
        self.ended.set()

    async def leave(self) -> int:
        self.left += 1
        return 202


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, directory: Directory) -> Iterator[None]:
    FakeSession.instances, FakeSession.start_error, FakeSession.start_hangs = [], None, False
    FakeCall.instances, FakeCall.join_error, FakeCall.join_hangs = [], None, False
    FakeCall.join_slow_cancel, FakeCall.join_errors = False, []
    monkeypatch.setattr(C, "JOIN_RETRY_PAUSE_S", 0.01)
    FakeCall.directory = directory
    monkeypatch.setattr(C, "Session", FakeSession)
    monkeypatch.setattr(C, "Call", FakeCall)
    yield


def session_up() -> bool:
    return bool(FakeSession.instances) and FakeSession.instances[0].started


async def test_follow_feeds_the_session_and_ends_with_the_call(
    settings: Settings, tmp_path: Path, fakes: None, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC6: frames heard while the engines load are held and fed first, once started; names come from
    the roster, then the names store, then the MRI; the call's end ends it."""
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(lambda: session_up() and len(FakeSession.instances[0].fed) == 2, "held frames fed")
    call, session = FakeCall.instances[0], FakeSession.instances[0]
    assert session.args == (tmp_path, CALL, THREAD) and session.kw == {"header": {"join": "ok"}}
    # Both frames heard during start() are held, then fed once it returned.
    assert sorted(session.fed) == [(201, 320, 1.0), (202, 320, 0.5)] and call.capture is None
    call.on_audio(830, np.zeros(320, np.int16), 1.02)
    assert session.fed[-1] == (830, 320, 1.02)
    call.roster.update(
        roster(1, (OTHER, "Other Synthetic", [("main-audio", 830)]), (ME, "", [("main-audio", 5)]))
    )
    monkeypatch.setattr(directory, "known_name", lambda mri: "Me From Store" if mri == ME else "")
    assert session.resolve(830) == ("Other Synthetic", OTHER)
    assert session.resolve(5) == ("Me From Store", ME) and session.resolve(9) is None
    call.ended.set()
    assert await asyncio.wait_for(task, 5) == "call_ended"
    assert call.left == 1 and session.closed
    assert F.ended_joins(tmp_path, CALL) == {payload()["userJoinTime"]}  # written before the hang-up


async def test_the_ended_marker_is_written_before_the_hang_up(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC4: evt 116 comes once the DELETE is done and my rejoin's 115 may follow at once: by then the
    join must read as ended, or the rejoin's push is taken for this follower and dropped."""
    seen: list[set[str]] = []

    async def leave(self: FakeCall) -> int:
        seen.append(F.ended_joins(tmp_path, CALL))
        return 202

    monkeypatch.setattr(FakeCall, "leave", leave)
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    FakeCall.instances[0].ended.set()
    assert await asyncio.wait_for(task, 5) == "call_ended"
    assert seen == [{payload()["userJoinTime"]}]


async def test_a_marker_that_cannot_be_written_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    (tmp_path / "follow").write_text("not a directory")
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    with capture_logs() as logs:
        FakeCall.instances[0].ended.set()
        assert await asyncio.wait_for(task, 5) == "call_ended"
    assert "follow_ended_marker_failed" in [e["event"] for e in logs] and FakeCall.instances[0].left == 1


async def test_a_trouter_move_re_attaches_and_the_transcript_goes_on(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: the moved call is hung up (one DELETE), a fresh one joins the same push, the same session
    keeps transcribing it with the speakers' names; a second move re-attaches again."""
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    first, session = FakeCall.instances[0], FakeSession.instances[0]
    first.roster.update(roster(1, (OTHER, "Other Synthetic", [("main-audio", 830)])))
    fed = len(session.fed)
    with capture_logs() as logs:
        first.move()
        await until(lambda: len(FakeCall.instances) == 2 and FakeCall.instances[1].joined, "re-attached")
    second = FakeCall.instances[1]
    assert first.left == 1 and second.left == 0 and FakeSession.instances == [session] and not session.closed
    assert "follow_reattach" in [e["event"] for e in logs]
    await until(lambda: len(session.fed) > fed, "the new call's audio in the same session")
    assert session.resolve(830) == ("Other Synthetic", OTHER)  # a line still in the engines keeps its name
    assert F.ended_joins(tmp_path, CALL) == set()  # a re-attach never reads as the join's end
    second.move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached again")
    assert F.ended_joins(tmp_path, CALL) == set()
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert [c.left for c in FakeCall.instances] == [1, 1, 1] and session.closed


async def test_a_stop_during_the_re_attach_pause_joins_nothing(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-attach waits like the retry (a call that ended meanwhile gets its evt 116, the trigger's stop,
    before the direct phase 2 brings the ended conversation back as me): a stop then ends it cleanly,
    nothing joined."""
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    monkeypatch.setattr(C, "JOIN_RETRY_PAUSE_S", 30.0)
    FakeCall.instances[0].move()
    await until(lambda: len(FakeCall.instances) == 2, "pausing")
    await asyncio.sleep(0.05)
    assert not FakeCall.instances[1].joined
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    first, second = FakeCall.instances
    assert (first.left, second.left, second.joined) == (1, 1, False) and FakeSession.instances[0].closed


async def test_a_re_attach_failing_twice_ends_the_follow(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: fail safe, no follower sitting deaf in the call as me: a retryable failure gets one more
    re-attach, a second ends it, marked and hung up."""
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    FakeCall.join_errors = [C.JoinError("phase2 answered 500"), C.JoinError(C.TROUTER_MOVED)]
    with capture_logs() as logs:
        FakeCall.instances[0].move()
        assert await asyncio.wait_for(task, 5) == "trouter_moved"
    failed = [(e["cause"], e["attempt"], e["retry"]) for e in logs if e["event"] == "follow_reattach_failed"]
    assert failed == [("phase2 answered 500", 1, True), (C.TROUTER_MOVED, 2, False)]
    assert [c.left for c in FakeCall.instances] == [1, 1, 1] and FakeSession.instances[0].closed
    assert F.ended_joins(tmp_path, CALL) == {payload()["userJoinTime"]}


async def test_a_re_attach_interrupted_once_gets_one_more(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: the re-attach's own join ended retryably (another move mid-join, a failed renegotiation):
    one more try, and the follow goes on."""
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    FakeCall.join_errors = [C.JoinError("renegotiation: ValueError")]
    FakeCall.instances[0].move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached")
    assert not task.done() and not FakeSession.instances[0].closed
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


@pytest.mark.parametrize(
    ("end", "outcome"),
    [(None, C.TROUTER_MOVED), (("last_device_left", False), "last_device_left")],
)
async def test_a_final_re_attach_failure_ends_at_once_with_its_real_cause(
    settings: Settings,
    tmp_path: Path,
    fakes: None,
    monkeypatch: pytest.MonkeyPatch,
    end: tuple[str, bool] | None,
    outcome: str,
) -> None:
    """044-AC13: a failure ruling out a new join (a gone call's 404, my last device gone during the join)
    gets no second re-attach; the outcome is the call's own end when it had one."""
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    original = FakeCall.join

    async def join(self: FakeCall) -> None:
        if self is FakeCall.instances[0]:
            await original(self)
            return
        if end is not None:
            self.end_reason, self.retryable = end
            self.ended.set()
            raise C.JoinError(end[0], retry=end[1])
        raise C.JoinError("phase2 answered 404", retry=False)

    monkeypatch.setattr(FakeCall, "join", join)
    FakeCall.instances[0].move()
    assert await asyncio.wait_for(task, 5) == outcome
    assert len(FakeCall.instances) == 2 and [c.left for c in FakeCall.instances] == [1, 1]


async def test_follow_stops_on_the_signal(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC4: SIGTERM (the trigger's stop) → DELETE, then the transcript closes."""
    settings.follow_capture = tmp_path / "cap"
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert FakeCall.instances[0].left == 1 and FakeSession.instances[0].closed
    assert FakeCall.instances[0].capture == tmp_path / "cap" / CALL


async def test_follow_stopped_while_joining(settings: Settings, tmp_path: Path, fakes: None) -> None:
    FakeCall.join_hangs = True
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(lambda: bool(FakeCall.instances), "joining")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert FakeCall.instances[0].left == 1 and FakeSession.instances == []


async def test_follow_stopped_while_the_engines_load(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC4: the stop is not held until the engines are up; it hangs up and closes the session."""
    FakeSession.start_hangs = True
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(lambda: bool(FakeSession.instances), "loading")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    session = FakeSession.instances[0]
    assert FakeCall.instances[0].left == 1 and session.closed and session.fed == []


async def test_follow_cancelled_itself_leaves_no_body_behind(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC4: cancelling follow() mid-join stops its body before the hang-up; nothing is left pending."""
    FakeCall.join_hangs = True
    before = asyncio.all_tasks()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances), "joining")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and FakeCall.instances[0].left == 1
    assert [t for t in asyncio.all_tasks() - before if not t.done()] == []


async def test_a_second_cancel_while_the_body_winds_down_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC4: follow() cancelled again while its body is still cancelling: the hang-up still happens."""
    FakeCall.join_hangs = FakeCall.join_slow_cancel = True
    before = asyncio.all_tasks()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances), "joining")
    task.cancel()
    await until(lambda: FakeCall.instances[0].cancel_held, "body cancelling")
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert task.cancelled() and FakeCall.instances[0].left == 1
    assert [t for t in asyncio.all_tasks() - before if not t.done()] == []


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (C.JoinError("not admitted (lobby timeout)"), "not admitted (lobby timeout)"),
        (OSError("boom"), "OSError: boom"),
    ],
)
async def test_a_failed_join_lands_in_the_transcript_header(
    settings: Settings, tmp_path: Path, fakes: None, error: BaseException, cause: str
) -> None:
    """044-AC14: the cause is logged, retried once, then written to the header; nothing is transcribed."""
    FakeCall.join_error = error
    with capture_logs() as logs:
        assert await C.follow(settings, tmp_path, payload(), threading.Event()) == "join_failed"
    failures = [(e["cause"], e["attempt"], e["retry"]) for e in logs if e["event"] == "follow_join_failed"]
    assert failures == [(cause, 1, True), (cause, 2, False)]
    (path,) = (tmp_path / "follow").rglob("*.ndjson")
    header = json.loads(path.read_text())
    assert header["join"] == "failed" and header["error"] == cause and header["call"] == CALL
    assert (header["thread"], header["kind"], header["engine"]) == (THREAD, "meeting", "parakeet")
    assert [c.left for c in FakeCall.instances] == [1, 1] and FakeSession.instances == []


async def test_a_join_that_fails_once_is_retried_and_followed(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC14: the first try hung up, a fresh call joined after the pause; the transcript is the
    second call's; the wind-down leaves that one."""
    FakeCall.join_errors = [C.JoinError("ICE: ConnectionError")]
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    first, second = FakeCall.instances
    assert first.left == 1 and second.left == 0
    await until(lambda: (201, 320, 1.0) in FakeSession.instances[0].fed, "the second call's audio")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert (first.left, second.left) == (1, 1) and not list((tmp_path / "follow").rglob("*.ndjson"))


@pytest.mark.parametrize(
    "reason",
    ["call/end: 5003 conversation has ended", "conversation/conversationEnd: 5010", "last_device_left"],
)
async def test_no_retry_after_the_call_itself_ended(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """044-AC14: the call ended (over, or my last device gone) after the acceptance, then the join failed
    on something retryable (ICE): the call's own end rules the retry out."""

    async def join(self: FakeCall) -> None:
        self.end_reason, self.retryable = reason, False
        self.ended.set()
        raise C.JoinError("ICE: ConnectionError")

    monkeypatch.setattr(FakeCall, "join", join)
    with capture_logs() as logs:
        assert await asyncio.wait_for(C.follow(settings, tmp_path, payload(), threading.Event()), 5) == (
            "join_failed"
        )
    assert len(FakeCall.instances) == 1
    assert [e["retry"] for e in logs if e["event"] == "follow_join_failed"] == [False]


async def test_a_retry_and_a_re_attach_go_straight_to_the_reached_controller(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No phase 1 once a join reached the call's controller, and the names cache is kept (no new store
    per Call); a first try that never got that far leaves the retry free to run phase 1."""
    tries: list[str] = []
    original = FakeCall.join

    async def join(self: FakeCall) -> None:
        tries.append(self.controller)
        if len(tries) == 1:
            self.controller = CONTROLLER  # phase 1 answered, then the join failed
            raise C.JoinError("ICE: ConnectionError")
        await original(self)

    monkeypatch.setattr(FakeCall, "join", join)
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    FakeCall.instances[1].move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached")
    assert tries == ["", CONTROLLER, CONTROLLER]
    first = FakeCall.instances[0]
    assert [c.directory_given for c in FakeCall.instances[1:]] == [first.directory, first.directory]
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


async def test_a_retry_before_any_controller_runs_phase_1(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """A first try that failed before reaching the controller (phase 1 refused): the retry has none to go
    straight to, so it runs phase 1 itself, never the push's controller."""
    FakeCall.join_errors = [C.JoinError("phase1 answered 503")]
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    assert [c.controller for c in FakeCall.instances] == ["", ""]
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


async def test_no_retry_when_the_call_is_gone(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC14: a refusal proving the call is over (a 1:1's controller answering 404) is given up at once."""
    FakeCall.join_errors = [C.JoinError("phase2 answered 404", retry=False)]
    assert await asyncio.wait_for(C.follow(settings, tmp_path, payload(), threading.Event()), 5) == (
        "join_failed"
    )
    assert len(FakeCall.instances) == 1 and FakeCall.instances[0].left == 1


async def test_a_stop_during_the_retry_pause_joins_nothing_more(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC14: never retried after a stop."""
    monkeypatch.setattr(C, "JOIN_RETRY_PAUSE_S", 30.0)
    FakeCall.join_errors = [C.JoinError("not admitted (lobby timeout)")]
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(lambda: len(FakeCall.instances) == 2, "pausing")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    first, second = FakeCall.instances
    assert first.left == 1 and second.left == 1  # the fresh one, never joined, still closed
    assert FakeSession.instances == []


async def test_a_header_that_cannot_be_written_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeCall.join_error = C.JoinError("refused")
    (tmp_path / "follow").write_text("not a directory")
    with pytest.raises(OSError):
        await C.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeCall.instances[0].left == 1


async def test_engines_that_fail_to_load_still_hang_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeSession.start_error = RuntimeError("engine died")
    with pytest.raises(RuntimeError):
        await C.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeCall.instances[0].left == 1 and FakeSession.instances[0].closed


async def test_a_start_failure_outranks_a_failing_leave(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engines' failure is the cause worth reporting, not the hang-up's that followed it."""

    async def broken(self: FakeCall) -> int:
        raise OSError("leave broke")

    monkeypatch.setattr(FakeCall, "leave", broken)
    FakeSession.start_error = RuntimeError("engine died")
    with pytest.raises(RuntimeError, match="engine died"):
        await C.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeSession.instances[0].closed


async def test_a_failing_leave_still_closes_the_session(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript's last lines are flushed whatever the hang-up did."""

    async def broken(self: FakeCall) -> int:
        raise RuntimeError("leave broke")

    monkeypatch.setattr(FakeCall, "leave", broken)
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    stop.set()
    with pytest.raises(RuntimeError):
        await task
    assert FakeSession.instances[0].closed


async def test_pending_audio_is_bounded_and_its_drops_counted(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(C, "PENDING_MAX", 1)
    stop = threading.Event()
    with capture_logs() as logs:
        task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
        await until(lambda: session_up() and bool(FakeSession.instances[0].fed), "session up")
    assert len(FakeSession.instances[0].fed) == 1
    full = [e for e in logs if e["event"] == "follow_pending_full"]
    assert [(e["dropped"], e["held"]) for e in full] == [(1, 1)]
    stop.set()
    await task


async def test_leave_and_close_run_at_once(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both must fit in the follower's leave budget: a slow hang-up does not delay the transcript's close."""
    order: list[str] = []

    async def slow_leave(self: FakeCall) -> int:
        order.append("leave")
        await asyncio.sleep(0.5)
        return 202

    async def slow_close(self: FakeSession) -> None:
        order.append("close")
        await asyncio.sleep(0.5)
        self.closed = True

    monkeypatch.setattr(FakeCall, "leave", slow_leave)
    monkeypatch.setattr(FakeSession, "close", slow_close)
    stop = threading.Event()
    task = asyncio.ensure_future(C.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert loop.time() - t0 < 0.5 + 0.2 + 0.2  # the longer of the two, one stop poll, slack; both: 1.0+
    assert sorted(order) == ["close", "leave"] and FakeSession.instances[0].closed
