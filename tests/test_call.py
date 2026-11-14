"""Joining a call and hearing it (spec 044, phase 3 slice 1). Synthetic ids, keys and audio only."""

import asyncio
import base64
import gzip
import json
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
from structlog.testing import capture_logs

from miniteams import call as C
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
        await kw["on_session"]("ep", SURL)  # a re-register on the same socket
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
    cap = tmp_path / "cap"
    call, ice, _ = make_call(capture=cap)
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


@pytest.mark.parametrize(
    "push",
    [
        {k: v for k, v in payload().items() if k not in ("groupContext", "meetingInfo")},  # a 1:1 push
        {**payload(), "meetingInfo": None},
        {**payload(), "groupContext": None},
        {**payload(), "groupContext": {"threadId": "", "messageId": "0"}},
    ],
)
async def test_a_push_without_meeting_info_never_places_a_call(
    settings: Settings, directory: Directory, server: Server, push: dict[str, Any]
) -> None:
    """Phase 1 without meetingInfo creates an ad hoc call of ours: a 1:1 push stops before any request."""
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
    assert await asyncio.wait_for(failed(call), 5) == "1:1 calls not supported yet"
    assert server.requests == [] and server.trouter_kwargs == {}


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


async def test_trouter_moving_mid_call_is_logged(make_call: Any) -> None:
    call, _, _ = make_call()
    await call.join()
    with capture_logs() as logs:
        await call._on_session("ep", "https://trouter.invalid/v4/f/other/")
    assert [e["event"] for e in logs] == ["call_trouter_moved"]
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
    join_hangs = False
    join_slow_cancel = False  # the first cancel is held: the join winds down slowly, as a real one may
    directory: Any = None

    def __init__(
        self, settings: Settings, payload: dict[str, Any], on_audio: Any, capture: Path | None = None
    ) -> None:
        self.on_audio, self.capture = on_audio, capture
        self.roster = C.Roster(me=ME)
        self.directory = FakeCall.directory
        self.ended = asyncio.Event()
        self.left = 0
        self.cancel_held = False
        FakeCall.instances.append(self)

    async def join(self) -> None:
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
        asyncio.get_running_loop().call_soon(self.on_audio, 201, np.zeros(320, np.int16), 1.0)

    async def leave(self) -> int:
        self.left += 1
        return 202


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, directory: Directory) -> Iterator[None]:
    FakeSession.instances, FakeSession.start_error, FakeSession.start_hangs = [], None, False
    FakeCall.instances, FakeCall.join_error, FakeCall.join_hangs = [], None, False
    FakeCall.join_slow_cancel = False
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
    """044-AC14: the cause is logged and written to the header; nothing is transcribed."""
    FakeCall.join_error = error
    with capture_logs() as logs:
        assert await C.follow(settings, tmp_path, payload(), threading.Event()) == "join_failed"
    assert {"event": "follow_join_failed", "call": CALL, "cause": cause, "log_level": "warning"} in logs
    (path,) = (tmp_path / "follow").rglob("*.ndjson")
    header = json.loads(path.read_text())
    assert header["join"] == "failed" and header["error"] == cause and header["call"] == CALL
    assert (header["thread"], header["kind"], header["engine"]) == (THREAD, "meeting", "parakeet")
    assert FakeCall.instances[0].left == 1 and FakeSession.instances == []


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
