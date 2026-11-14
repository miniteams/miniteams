"""Join a call as a second endpoint of my account and hear it, one 16 kHz track per speaker (spec 044).

Protocol as observed in grill-room spike S18 (ost + patch): phase 1 `epconv` with the push's
`meetingInfo`, phase 2 on the conversation controller with our SDP offer, the call controller's
callbacks on our Trouter socket (`callAgent/...`: acceptance to acknowledge, roster `Full`, a media
renegotiation to answer), ICE (aioice, host + STUN), SDES SRTP (pylibsrtp), `x-multichannel` audio
(one G.722 block per active speaker, tagged by MSI), PCMU silence out, `DELETE` on the call leg to
leave. Imported only by the follower process: it needs the `transcript` extra.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import struct
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse

import aioice
import av
import httpx
import numpy as np
import numpy.typing as npt
import pylibsrtp
import structlog

from .auth import self_mri, token_source
from .config import Settings
from .directory import Directory
from .follow import call_id, join_time, mark_ended, thread_of
from .messages import _decode_body
from .skype import exchange_skype_token
from .stream import AGENT_PATH, off_loop, run_forever
from .stt.session import Session, Transcript, transcript_header

log = structlog.get_logger()

# Headers and capability masks seen from Teams clients (ost); the AMER region headers worked for a
# European tenant in S18.
SKYPE_CLIENT = "SkypeSpaces/1415/teams-cli/TsCallingVersion=2025.49.01.15"
REGION = {"ms-teams-partition": "amer03", "ms-teams-region": "amer", "ms-teams-ring": "general"}
PT_MULTICHANNEL, PT_PCMU = 110, 0
RTP_CLOCK = 48000  # x-multichannel: 960 ticks per 20 ms packet (S18 3.3)
FRAME_S = 0.02
PCMU_SILENCE = b"\xff" * 160  # mu-law zero, 20 ms at 8 kHz
RESYNC_S = 2.0  # an RTP clock this far from the arrival time jumped: re-anchor it
ACCEPT_TIMEOUT_S = 300.0  # a meeting lobby waits for someone to admit us
STEP_TIMEOUT_S = 30.0  # Trouter session, ICE
# RTCP reaches us every ~5 s even in silence: the longest gap between two packets of any kind over every
# live capture (S18 runs up to 30 min, Phase 3 runs) was 6.2 s; 4x that, floored at a minute.
MEDIA_IDLE_S = 60.0
# A key's base64, also as JSON may escape it (`\/`, `\u002b`).
_KEY = re.compile(r"inline:(?:[A-Za-z0-9+/=]|\\/|\\u[0-9A-Fa-f]{4})+")
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
PENDING_MAX = 60 * 4 * 50  # frames held while the engines load: 60 s of 4 speakers
JOIN_RETRY_PAUSE_S = 5.0
LAST_DEVICE_LEFT = "last_device_left"
TROUTER_MOVED = "trouter_moved"  # our socket reconnected on a new surl: the callbacks name the old one
_CC_LINKS = (
    "mediaAcknowledgement rejection acknowledgement mediaRenegotiation replacement progress mediaAnswer "
    "newMediaOffer redirection balanceUpdate acceptance controlVideoStreaming dominantSpeakerInfo csrcInfo "
    "end retargetCompletion transfer transferAcceptance transferCompletion holdCompletion resumeCompletion "
    "monitorCompletion"
).split()

Audio = Callable[[int, npt.NDArray[np.int16], float], None]  # (msi, 20 ms of 16 kHz PCM, wall time)


class JoinError(RuntimeError):
    """The join failed; the message is its cause. `retry` False: a second try cannot do better."""

    def __init__(self, cause: str, retry: bool = True) -> None:
        super().__init__(cause)
        self.retry = retry


# --- SDP ---


def _sections(sdp: str) -> list[list[str]]:
    """Lines of the session part, then of each m-section."""
    out: list[list[str]] = [[]]
    for line in sdp.splitlines():
        if line.startswith("m="):
            out.append([])
        if line:
            out[-1].append(line)
    return out


def offer(ip: str, port: int, ufrag: str, pwd: str, candidates: list[str], key: bytes, ssrc: int) -> str:
    """Audio only. `110 111 0`: the server sends the first codec we list, x-multichannel; PCMU is there
    for the silence we send (RFC 3264 6.1). The audio SSRC must match x-ssrc-range or the mixer drops it."""
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {ip}",
        "s=session",
        f"c=IN IP4 {ip}",
        "b=CT:99980",
        "t=0 0",
        f"m=audio {port} RTP/SAVP {PT_MULTICHANNEL} 111 {PT_PCMU}",
        f"a=x-ssrc-range:{ssrc}-{ssrc}",
        "a=rtcp-fb:* x-message app send:dsh recv:dsh",
        "a=rtcp-rsize",
        "a=mid:0",
        f"a=rtpmap:{PT_MULTICHANNEL} x-multichannel/48000",
        "a=rtpmap:111 OPUS/48000/2",
        f"a=rtpmap:{PT_PCMU} PCMU/8000",
        "a=ptime:20",
        "a=sendrecv",
        "a=rtcp-mux",
        "a=label:main-audio",
        "a=x-source:main-audio",
        f"a=ice-ufrag:{ufrag}",
        f"a=ice-pwd:{pwd}",
        *candidates,
        f"a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{base64.b64encode(key).decode()}|2^31",
    ]
    return "\r\n".join(lines) + "\r\n"


def candidate_line(c: aioice.Candidate) -> str:
    related = f" raddr {c.related_address} rport {c.related_port}" if c.related_address else ""
    return (
        f"a=candidate:{c.foundation} {c.component} UDP {c.priority} {c.host} {c.port} typ {c.type}{related}"
    )


def crypto_key(line: str) -> bytes:
    """The 30-byte master key + salt of an `a=crypto:N AES_CM_128_HMAC_SHA1_80 inline:...` line."""
    parts = line.split()
    if len(parts) < 3 or parts[1] != "AES_CM_128_HMAC_SHA1_80" or not parts[2].startswith("inline:"):
        raise ValueError("unsupported crypto line")
    key = base64.b64decode(parts[2].removeprefix("inline:").split("|")[0], validate=True)
    if len(key) != 30:
        raise ValueError("crypto key is not 30 bytes")
    return key


@dataclass
class Media:
    """The audio section of the other side's SDP."""

    ufrag: str = ""
    pwd: str = ""
    candidates: list[str] = field(default_factory=list)
    key: bytes = b""
    codec: int | None = None  # first payload type of the m-line: the codec the server sends
    msi: int | None = None  # a=x-source-streamid: our own audio MSI in an answer to our offer


def parse_media(sdp: str) -> Media:
    """Raises ValueError when the SDP has no usable audio section."""
    audio = next((s for s in _sections(sdp)[1:] if s[0].startswith("m=audio ")), None)
    if audio is None:
        raise ValueError("no audio section")
    m = Media()
    fmt = audio[0].split()[3:]
    m.codec = int(fmt[0]) if fmt and fmt[0].isdigit() else None
    for line in audio:
        name, _, value = line.partition(":")
        if name == "a=ice-ufrag":
            m.ufrag = value
        elif name == "a=ice-pwd":
            m.pwd = value
        elif name == "a=candidate":
            m.candidates.append(value)
        elif name == "a=crypto" and not m.key:
            try:
                m.key = crypto_key(value)
            except ValueError:  # another suite or a broken line: a later one may do
                continue
        elif name == "a=x-source-streamid" and value.isdigit():
            m.msi = int(value)
    if not (m.ufrag and m.pwd and m.key):
        raise ValueError("audio section without ICE credentials or SDES key")
    return m


def renegotiation_answer(ours: str, theirs: str) -> str:
    """Our offer again (same ports, ICE credentials and key: the running media goes on), then every
    m-section of theirs past ours refused with port 0, its mid kept. Without an answer no audio flows."""
    out = [line for part in _sections(ours) for line in part]
    for section in _sections(theirs)[len(_sections(ours)) :]:
        fields = section[0].removeprefix("m=").split()
        if len(fields) < 4:  # not an m-line we can name back
            continue
        media, _port, proto, fmt = fields[:4]
        out.append(f"m={media} 0 {proto} {fmt}")
        out += [line for line in section if line.startswith("a=mid:")][:1]
    return "\r\n".join(out) + "\r\n"


def redact(text: str) -> str:
    """`text` (an SDP, or a JSON row carrying one) without its SRTP keys, for a capture."""
    return _KEY.sub("inline:<redacted>", text)


# --- RTP ---


@dataclass(frozen=True, slots=True)
class Rtp:
    pt: int
    seq: int
    ts: int
    ssrc: int
    marker: bool
    csrc: tuple[int, ...]
    payload: bytes


def parse_rtp(data: bytes) -> Rtp | None:
    """RFC 3550 header, CSRCs, extension and padding stripped; None when malformed."""
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    cc, off = data[0] & 0x0F, 12 + 4 * (data[0] & 0x0F)
    if len(data) < off:
        return None
    csrc = struct.unpack_from(f">{cc}I", data, 12)
    if data[0] & 0x10:
        if len(data) < off + 4:
            return None
        off += 4 + 4 * struct.unpack_from(">H", data, off + 2)[0]
    end = len(data) - (data[-1] if data[0] & 0x20 else 0)
    if end < off:
        return None
    seq, ts, ssrc = struct.unpack_from(">HII", data, 2)
    return Rtp(data[1] & 0x7F, seq, ts, ssrc, bool(data[1] & 0x80), csrc, data[off:end])


def rtp_packet(pt: int, seq: int, ts: int, ssrc: int, payload: bytes) -> bytes:
    return struct.pack(">BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


def is_rtcp(data: bytes) -> bool:
    """RFC 5761: RTCP and RTP share the socket (rtcp-mux); RTCP packet types are 192-223."""
    return len(data) > 1 and 192 <= data[1] <= 223


def unwrap(last: int | None, value: int, bits: int) -> int:
    """`value` (a wrapping counter of `bits`) extended next to `last`, the previous extended value."""
    if last is None:
        return value
    span = 1 << bits
    diff = (value - last) % span
    return last + (diff - span if diff >= span // 2 else diff)


class Timeline:
    """Each SSRC's RTP clock placed on the wall clock, anchored at its first packet; a jump (a sender
    restarting its clock, a long hole) re-anchors it on the arrival time."""

    def __init__(self) -> None:
        self._ssrc: dict[
            int, tuple[int, int, float]
        ] = {}  # ssrc → (last extended ts, anchor ts, anchor wall)

    def place(self, ssrc: int, ts: int, now: float) -> float:
        last, anchor_ts, anchor_wall = self._ssrc.get(ssrc, (None, 0, now))
        ext = unwrap(last, ts, 32)
        if last is None:
            anchor_ts = ext
        t = anchor_wall + (ext - anchor_ts) / RTP_CLOCK
        if abs(t - now) > RESYNC_S:
            anchor_ts, anchor_wall, t = ext, now, now
        self._ssrc[ssrc] = (ext, anchor_ts, anchor_wall)
        return t


def blocks(payload: bytes) -> list[tuple[int, bytes]]:
    """`x-multichannel` payload → [(msi, G.722 block)]: byte 0 = 0x20 + n, byte 1 = 0x11, then n times
    msi u32 LE, len u16 LE, data; an 8-byte trailer per block follows (S18 3.3). [] when malformed."""
    if len(payload) < 2 or payload[1] != 0x11 or not 0x21 <= payload[0] <= 0x2F:
        return []
    out, i = [], 2
    for _ in range(payload[0] - 0x20):
        if i + 6 > len(payload):
            return []
        msi, length = struct.unpack_from("<IH", payload, i)
        i += 6
        if i + length > len(payload):
            return []
        if length:  # an empty block would drain that speaker's decoder: the next one raises EOFError
            out.append((msi, payload[i : i + length]))
        i += length
    return out


class Decoder:
    """G.722 → 16 kHz int16, one decoder per MSI: G.722 is stateful (ADPCM)."""

    def __init__(self) -> None:
        # ponytail: one per MSI for the call's life; a call sees tens of MSIs, not thousands.
        self._ctx: dict[int, Any] = {}

    def decode(self, msi: int, data: bytes) -> npt.NDArray[np.int16]:
        ctx = self._ctx.get(msi)
        if ctx is None:
            ctx = self._ctx[msi] = av.CodecContext.create("g722", "r")
            ctx.sample_rate, ctx.layout = 16000, "mono"
        frames = [f.to_ndarray().reshape(-1) for f in ctx.decode(av.Packet(data))]
        return np.concatenate(frames).astype(np.int16) if frames else np.zeros(0, np.int16)


def srtp_session(key: bytes, inbound: bool) -> pylibsrtp.Session:
    """libsrtp's ANY_INBOUND / ANY_OUTBOUND template: each SSRC gets its own stream, so its own rollover
    counter and replay window (one shared counter dropped packets once sequence spaces interleaved)."""
    kind = pylibsrtp.Policy.SSRC_ANY_INBOUND if inbound else pylibsrtp.Policy.SSRC_ANY_OUTBOUND
    return pylibsrtp.Session(pylibsrtp.Policy(key=key, ssrc_type=kind))


# --- roster ---


@dataclass
class Roster:
    """MSI → (displayName, MRI) from `Full` roster updates. MSIs are per session: never cached across calls.
    `mine` (my entry's main-audio MSIs, one per endpoint of mine, the follower's included) is what the
    leave rule reads: leave when it holds nothing but `own`."""

    me: str = ""
    own: int | None = None  # the follower's own audio MSI, from the server's answer
    names: dict[int, tuple[str, str]] = field(default_factory=dict)  # every MSI seen in this call
    mine: set[int] = field(default_factory=set)
    seq: int = -1

    def update(self, body: dict[str, Any]) -> bool:
        """False when the body is stale or not a roster."""
        participants = body.get("participants")
        seq = body.get("sequenceNumber")
        if not isinstance(participants, dict) or (isinstance(seq, int) and seq < self.seq):
            return False
        self.seq = seq if isinstance(seq, int) else self.seq
        mine: set[int] = set()
        for mri, p in participants.items():
            if not isinstance(p, dict):
                continue
            details, media = p.get("details"), p.get("call")
            name = str(details.get("displayName") or "") if isinstance(details, dict) else ""
            streams = media.get("mediaStreams") if isinstance(media, dict) else None
            for s in streams if isinstance(streams, list) else []:
                if isinstance(s, dict) and isinstance(s.get("sourceId"), int):
                    self.names[s["sourceId"]] = (name, str(mri))
                    if mri == self.me and s.get("label") == "main-audio":
                        mine.add(s["sourceId"])
        self.mine = mine
        return True


# --- capture (--capture only) ---


class Capture:
    """S18's layout, so its analyze.py reads it: rtp.jsonl + ssrc-<n>.pay (payloads back to back),
    trouter.jsonl, the SDPs (keys redacted)."""

    def __init__(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        self._t0 = time.monotonic()
        self._rtp = (path / "rtp.jsonl").open("a")
        self._rtcp = (path / "rtcp.jsonl").open("a")
        self._trouter = (path / "trouter.jsonl").open("a")
        self._pay: dict[int, IO[bytes]] = {}

    def _t(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def file(self, name: str, text: str) -> None:
        (self.path / name).write_text(redact(text))

    def rtp(self, p: Rtp) -> None:
        row = {"t": self._t(), "ssrc": p.ssrc, "seq": p.seq, "ts": p.ts, "pt": p.pt, "m": p.marker}
        self._rtp.write(json.dumps({**row, "csrc": list(p.csrc), "plen": len(p.payload)}) + "\n")
        if p.ssrc not in self._pay:
            self._pay[p.ssrc] = (self.path / f"ssrc-{p.ssrc}.pay").open("ab")
        self._pay[p.ssrc].write(p.payload)

    def rtcp(self, data: bytes) -> None:
        """When it came and its type: SRTCP keeps the first 8 bytes clear."""
        self._rtcp.write(json.dumps({"t": self._t(), "pt": data[1], "len": len(data)}) + "\n")

    def trouter(self, path: str, headers: Any, body: Any) -> None:
        # The acceptance and renegotiation bodies carry SDP: their keys go too.
        row = json.dumps({"t": self._t(), "path": path, "headers": headers, "body": body})
        self._trouter.write(redact(row) + "\n")
        self._trouter.flush()

    def close(self) -> None:
        self._rtp.close()
        self._rtcp.close()
        self._trouter.close()
        for f in self._pay.values():
            f.close()


# --- the call ---


class _Ice(aioice.Connection):
    async def query_consent(self) -> None:
        """No RFC 7675 consent checks: a Teams relay stopped answering them on a pair still carrying
        media, and their expiry hung up a working call 30 s in (seen live); the S18 client sent none."""


def stun(settings: Settings) -> tuple[str, int] | None:
    """`host:port` from the settings; empty: host candidates only."""
    if not settings.stun_server:
        return None
    host, _, port = settings.stun_server.rpartition(":")
    return host, int(port)


def _https(url: str) -> str:
    """A server-given URL we send a bearer to: https only."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise JoinError("refused a non-https URL")
    return url


def _tc_path(url: str) -> str:
    """`.../callAgent/<endpoint>/<tag>/call/acceptance/` → `call/acceptance`."""
    return "/".join(url.split(AGENT_PATH, 1)[-1].split("?")[0].strip("/").split("/")[2:])


class Call:
    """join(), then audio flows to `on_audio` until `ended` is set or leave() is called."""

    def __init__(
        self,
        settings: Settings,
        payload: dict[str, Any],
        on_audio: Audio,
        *,
        directory: Directory | None = None,
        http: httpx.AsyncClient | None = None,
        ice: Callable[[], aioice.Connection] | None = None,
        trouter: Callable[..., Coroutine[Any, Any, None]] = run_forever,
        capture: Path | None = None,
        clock: Callable[[], float] = time.time,
        controller: str = "",
    ) -> None:
        self.settings = settings
        self.payload = payload
        self.on_audio = on_audio
        self.directory = directory or Directory(settings)
        self.http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._ice = ice or (lambda: _Ice(ice_controlling=True, stun_server=stun(settings), use_ipv6=False))
        self._trouter_fn = trouter
        self.endpoint = str(uuid.uuid4())  # this call's endpoint id, in every callback URL
        # One directory per Call: a retry or a re-attach of the same call never mixes its files in.
        self.capture = Capture(capture / self.endpoint) if capture is not None else None
        self._clock = clock
        self.roster = Roster()
        self._leaving: asyncio.TimerHandle | None = None
        self.ended = asyncio.Event()  # the call is over for us: call end, conversation end, media lost
        self.end_reason = ""
        self.retryable = True  # False once the first end said a new join cannot do better
        self._chain = str(uuid.uuid4())
        self._tag = secrets.token_hex(4)
        self._surl: asyncio.Future[str] | None = None
        self._accepted: asyncio.Future[dict[str, Any]] | None = None
        self._trouter: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Future[Any]] = set()
        self._conn: aioice.Connection | None = None
        self._offer = ""
        # The call's controller, given by an earlier join of this call, or read from a 1:1's push in
        # join(): phase 2 goes straight to it. An ended 1:1 then answers 404; an ended meeting or group
        # call comes back with only us in it (seen live), which the leave rule ends after its grace.
        self.controller = controller
        self._direct = bool(controller)
        self._one_to_one = False
        self._key = os.urandom(30)
        self._ssrc = secrets.randbits(32)
        self._rx: pylibsrtp.Session | None = None
        self._remote_key = b""
        self.leg = ""  # links.callLeg: DELETE leaves
        self.skype = ""
        self.stats = {
            "packets": 0,
            "rtcp": 0,
            "undecryptable": 0,
            "malformed": 0,
            "undecodable": 0,
            "blocks": 0,
        }

    # --- tokens and requests ---

    async def _ic3(self) -> str:
        # Silent each time: a call outlives the token; MSAL answers from its cache while it is valid.
        token = await off_loop(token_source(self.settings).ic3_token)
        if not token:
            raise JoinError("no IC3 token")
        return token

    async def _post(
        self, url: str, body: dict[str, Any], what: str, migration: bool = False
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {await self._ic3()}",
            "x-microsoft-skype-chain-id": self._chain,
            # A fresh id per request: reusing one made the server end the call at ~40 s (408/10056).
            "x-microsoft-skype-message-id": str(uuid.uuid4()),
            "x-microsoft-skype-client": SKYPE_CLIENT,
            "Referer": "https://teams.microsoft.com/",
            **REGION,
        }
        if migration:
            headers["x-ms-migration"] = "True"
        resp = await self.http.post(_https(url), json=body, headers=headers)
        log.info("call_request", step=what, status=resp.status_code)
        if self.capture is not None:
            self.capture.file(f"{what}.json", resp.text)
        if not resp.is_success:
            # A 1:1's controller answers 404 once its call is over (seen live, subCode 5907): no retry.
            gone = self._direct and what == "phase2" and resp.status_code == 404
            raise JoinError(f"{what} answered {resp.status_code}", retry=not gone)
        return resp

    def _tc(self, path: str) -> str:
        assert self._surl is not None and self._surl.done()
        return f"{self._surl.result()}callAgent/{self.endpoint}/{self._tag}/{path}"

    def _conversation(self, me: str, name: str, sdp: str | None) -> dict[str, Any]:
        """Phase 1 body (sdp None) or phase 2 (our offer): roster `Full` lists every participant with
        its MSIs (`Delta` only ever listed ourselves), and no `preheatProperties` (a preheated endpoint
        gets no audio from the mixer)."""
        tc = self._tc
        links = "conversationEnd conversationUpdate localParticipantUpdate addParticipantSuccess "
        links += "addParticipantFailure receiveMessage"
        if sdp is not None:
            links += " addModalitySuccess addModalityFailure confirmUnmute"
        group = self.payload.get("groupContext") or {}
        message_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "conversationRequest": {
                "subject": None if sdp is None else "",
                "roster": {"type": "Full", "rosterUpdate": tc("conversation/rosterUpdate/")},
                "properties": {
                    "allowConversationWithoutHost": True,
                    "enableGroupCallEventMessages": True,
                    "enableGroupCallUpgradeMessage": False,
                    "enableGroupCallMeetupGeneration": True,
                },
                "links": {k: tc(f"conversation/{k}/") for k in links.split()},
            },
            "groupContext": None,
            "groupChat": {"threadId": group.get("threadId"), "messageId": group.get("messageId") or "0"},
            "participants": {
                "from": {
                    "id": me,
                    "displayName": name,
                    "endpointId": self.endpoint,
                    "participantId": str(uuid.uuid4()),
                    "languageId": "en-US",
                }
            },
            "capabilities": None,
            "endpointCapabilities": 73463,
            "clientEndpointCapabilities": 9336554,
            "endpointMetadata": {"holographicCapabilities": 3},
            # Without it phase 1 creates an ad hoc call of ours instead of joining the meeting.
            "meetingInfo": self.payload.get("meetingInfo"),
            "endpointState": {
                "endpointStateSequenceNumber": 0,
                "endpointProperties": {
                    "additionalEndpointProperties": {"infoShownInReportMode": "FullInformation"}
                },
            },
            "debugContent": {"ecsEtag": '"0"', "causeId": message_id[:8]},
        }
        if self._one_to_one:  # a 1:1 has neither: with them null its controller answers 400 (seen live)
            del body["groupChat"], body["meetingInfo"]
        if sdp is not None:
            body["conversationRequest"] |= {"conversationType": None, "suppressDialout": True}
            body["participants"]["to"] = []
            body["callInvitation"] = {
                "callModalities": ["Audio"],
                "replaces": None,
                "transferor": None,
                "links": {
                    k: tc(f"call/{k}/")
                    for k in ("progress", "mediaAnswer", "acceptance", "redirection", "end")
                },
                "clientContentForMediaController": {
                    k: tc(f"call/{k}/") for k in ("controlVideoStreaming", "csrcInfo", "dominantSpeakerInfo")
                },
                "pstnContent": {
                    "emergencyCallCountry": "",
                    "platformName": "teams-cli",
                    "publicApiCall": False,
                },
                "mediaContent": {"contentType": "application/sdp", "blob": sdp},
            }
        return body

    def _cc_links(self) -> dict[str, Any]:
        links = {k: self._tc(f"call/{k}/") for k in _CC_LINKS}
        links["call"] = self._tc("call/updateMediaDescriptions")
        media = {k: self._tc(f"call/{k}/") for k in ("controlVideoStreaming", "csrcInfo")}
        return {"links": links, "clientContentForMediaController": media}

    # --- Trouter ---

    @property
    def leaving(self) -> bool:
        """My last device is gone: a leave is pending (or fired)."""
        return self._leaving is not None and not self._leaving.cancelled()

    async def _on_session(self, _epid: str, surl: str) -> None:
        assert self._surl is not None
        if not self._surl.done():
            self._surl.set_result(surl)
            return
        # A second session is a reconnect: callbacks sent meanwhile are lost, even on the same surl (and
        # a new surl gets none): deaf, and blind to my leaving.
        log.warning("call_trouter_moved", call=call_id(self.payload), leaving=self.leaving)
        # My last device already left: re-attaching would sit in the call as me past AC4's 15 s.
        if self.leaving:
            self._end(LAST_DEVICE_LEFT, retry=False)
        else:
            self._end(TROUTER_MOVED)

    async def on_agent(self, req: dict[str, Any]) -> None:
        """One call callback, already acked by the socket. Long work goes to tasks: the socket's read
        loop awaits this."""
        path = _tc_path(str(req.get("url") or ""))
        try:
            body = _decode_body(req) or {}
        except ValueError, OSError, EOFError:
            body = {}
        if self.capture is not None:
            self.capture.trouter(path, req.get("headers"), body)
        if path == "conversation/rosterUpdate":
            if self.roster.update(body):
                self._check_devices()
            return
        accepted = body.get("callAcceptance")
        if isinstance(accepted, dict):
            if self._accepted is not None and not self._accepted.done():
                self._accepted.set_result(accepted)
            elif url := (accepted.get("links") or {}).get("acknowledgement"):
                # The controller resends callAcceptance until acknowledged, then drops the call (408/10056).
                self._spawn(self._acknowledge(url))
            return
        nego = body.get("mediaNegotiation")
        if isinstance(nego, dict):
            self._spawn(self._renegotiate(nego))
            return
        end = body.get("callEnd") or body.get("sessionRejection")
        if isinstance(end, dict) or path == "conversation/conversationEnd":
            info = end if isinstance(end, dict) else body
            # The call is over: a retry's phase 1 would start a new call as me. A rejection is not.
            over = path == "conversation/conversationEnd" or isinstance(body.get("callEnd"), dict)
            self._end(f"{path}: {info.get('subCode')} {info.get('phrase') or ''}".strip(), retry=not over)
            return
        if path == "call/redirection":
            # Never seen live, so not acted on: a redirected leg would leave us following nothing.
            redirection = body.get("redirection")
            target = (redirection if isinstance(redirection, dict) else body).get("target")
            # Peer-supplied, may carry a token anywhere (host included): its shape only, never its content.
            raw = str(target or "")
            log.warning(
                "call_redirection",
                call=call_id(self.payload),
                target=f"<{type(target).__name__}, {len(raw)} chars>",
            )
            return
        log.debug("call_callback", path=path)

    def _check_devices(self) -> None:
        """044-AC4: my last device gone from the roster (the follower aside), leave once
        follow_leave_grace_s passes without one coming back (a transfer, a rejoin, a lagging roster)."""
        own = self.roster.own
        # Judged only on a roster listing our own stream: a complete view of my entry. One without it
        # (our MSI unknown before the acceptance, my entry missing) proves nothing about my devices.
        if own is None or own not in self.roster.mine:
            return
        if self.roster.mine - {own}:
            if self._leaving is not None:
                if self.ended.is_set():
                    log.info("call_device_back_too_late", call=call_id(self.payload))
                else:
                    self._leaving.cancel()
                    log.info("call_device_back", call=call_id(self.payload))
                self._leaving = None
        elif self._leaving is None and not self.ended.is_set():
            grace = self.settings.follow_leave_grace_s
            log.info("call_last_device_gone", call=call_id(self.payload), grace_s=grace)
            loop = asyncio.get_running_loop()
            self._leaving = loop.call_later(grace, self._end, LAST_DEVICE_LEFT, False)

    def _trouter_done(self, task: asyncio.Task[None]) -> None:
        """run_forever returns on a dead login: no callback reaches us any more, nor will a new join's."""
        if not task.cancelled():
            self._end("trouter: stopped", retry=False)

    def _end(self, reason: str, retry: bool = True) -> None:
        if not self.ended.is_set():
            self.end_reason, self.retryable = reason, retry
            log.info("call_ended", call=call_id(self.payload), reason=reason)
            self.ended.set()
        if self._accepted is not None and not self._accepted.done():
            self._accepted.set_exception(JoinError(reason, retry=retry))

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("call_task_failed", error=str(exc), error_type=type(exc).__name__)

    async def _acknowledge(self, url: str) -> None:
        await self._post(url, {"callAcceptanceAcknowledgement": self._cc_links()}, "acknowledge")

    async def _renegotiate(self, nego: dict[str, Any]) -> None:
        """The meeting moves us to its mixer with an offer of its own: answer it, then key our receiving
        side with its crypto line. Without both no audio flows: a failure ends the call."""
        try:
            await self._answer(nego)
        except Exception as exc:
            self._end(f"renegotiation: {type(exc).__name__}")
            raise

    async def _answer(self, nego: dict[str, Any]) -> None:
        content = nego.get("mediaContent") or {}
        theirs, url = str(content.get("blob") or ""), (nego.get("links") or {}).get("mediaAnswer")
        if not theirs or not url:
            raise ValueError("renegotiation without an offer or an answer link")
        key = parse_media(theirs).key  # before answering: an offer we cannot key is left unanswered
        answer = renegotiation_answer(self._offer, theirs)
        if self.capture is not None:
            self.capture.file("reneg-offer.sdp", theirs)
            self.capture.file("reneg-answer.sdp", answer)
        media = {
            "blob": answer,
            "contentType": "application/sdp",
            "mediaLegId": content.get("mediaLegId") or "",
            "escalationOccurring": False,
            "newOffer": False,
        }
        await self._post(
            url, {"mediaAnswer": {"callModalities": ["Audio"], "mediaContent": media}}, "renegotiate"
        )
        self._key_rx(key)

    def _key_rx(self, key: bytes) -> None:
        if key != self._remote_key:
            self._remote_key = key
            self._rx = srtp_session(key, inbound=True)

    # --- join and leave ---

    async def join(self) -> None:
        """Raises JoinError with the failure's cause."""
        group, meeting = self.payload.get("groupContext"), self.payload.get("meetingInfo")
        if not (isinstance(meeting, dict) and isinstance(group, dict) and group.get("threadId")):
            # A 1:1 push names neither: phase 1 without them creates an ad hoc call of ours instead, so
            # phase 2 goes straight to the live call's controller, which attaches us to it (seen live).
            direct = self.payload.get("conversationController")
            if not isinstance(direct, str) or not direct:
                raise JoinError("push names no meeting nor conversation controller", retry=False)
            self.controller, self._direct, self._one_to_one = self.controller or direct, True, True
        loop = asyncio.get_running_loop()
        self._conn = conn = self._ice()
        gathering = self._spawn(conn.gather_candidates())  # STUN takes seconds: meanwhile, sign in
        try:
            tokens = await off_loop(token_source, self.settings)  # MSAL reads its authority here
            aad = await off_loop(tokens.refresh)
            skype = await off_loop(exchange_skype_token, self.settings, aad["access_token"])
        except Exception as exc:
            raise JoinError(f"sign-in: {type(exc).__name__}") from exc
        self.skype, conv_url = skype["skype_token"], skype.get("conversation_url") or ""
        if not conv_url:
            raise JoinError("no conversation service in the authz answer")
        self.roster.me = me = self_mri(aad["access_token"])
        name = await off_loop(tokens.display_name)
        self._surl = loop.create_future()
        self._accepted = loop.create_future()
        self._trouter = asyncio.create_task(
            self._trouter_fn(
                self.settings,
                on_event=_ignore,
                directory=self.directory,
                # Its own socket: the registrar maps one endpoint id to one socket.
                # ponytail: one registration per call left to expire (TTL); a stable pool if they pile up.
                epid=str(uuid.uuid4()),
                prompt=False,
                on_session=self._on_session,
                on_agent=self.on_agent,
            )
        )
        waits: set[asyncio.Future[Any]] = {self._surl, self._trouter}
        await asyncio.wait(waits, timeout=STEP_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
        if not self._surl.done():
            if self._trouter.done():  # it returns on a dead login, raises on anything else
                failure = self._trouter.exception()
                raise JoinError(f"Trouter: {type(failure).__name__ if failure else 'stopped'}")
            raise JoinError("no Trouter session")
        self._trouter.add_done_callback(self._trouter_done)
        await gathering
        default = conn.get_default_candidate(1)
        if default is None:
            raise JoinError("no local ICE candidate")
        lines = [candidate_line(c) for c in conn.local_candidates]
        self._offer = offer(
            default.host, default.port, conn.local_username, conn.local_password, lines, self._key, self._ssrc
        )
        if self.capture is not None:
            self.capture.file("offer.sdp", self._offer)
        if not self.controller:
            created = (await self._post(conv_url, self._conversation(me, name, None), "phase1", True)).json()
            controller = created.get("conversationController") or (
                created.get("conversationResponse") or {}
            ).get("conversationController")
            if not isinstance(controller, str) or not controller:
                raise JoinError("phase1 named no conversation controller")
            self.controller = controller
        joined = (
            await self._post(self.controller, self._conversation(me, name, self._offer), "phase2", True)
        ).json()
        # A leg named before the acceptance is hung up even if we are never admitted. ponytail: the live
        # phase 2 answers carried none (only the conversation's `leave`, which may drop every endpoint
        # of mine); the lobby is left to the server's timeout until a capture shows one.
        self.leg = str(
            ((joined.get("links") if isinstance(joined, dict) else None) or {}).get("callLeg") or ""
        )
        try:
            accepted = await asyncio.wait_for(asyncio.shield(self._accepted), ACCEPT_TIMEOUT_S)
        except TimeoutError:
            raise JoinError("not admitted (lobby timeout)") from None
        links = accepted.get("links") or {}
        self.leg = str(links.get("callLeg") or self.leg)
        if url := links.get("acknowledgement"):
            await self._acknowledge(url)
        if self.leg:
            cc = self._cc_links()
            await self._post(
                self.leg, {"callAcceptanceAcknowledgement": cc, "callParticipantUpdate": cc}, "register"
            )
        answer = str((accepted.get("mediaContent") or {}).get("blob") or "")
        if self.capture is not None:
            self.capture.file("answer.sdp", answer)
        try:
            media = parse_media(answer)
        except ValueError as exc:
            raise JoinError(f"answer SDP: {exc}") from None
        self.roster.own = media.msi
        if media.msi is None:
            log.warning("call_no_own_msi", call=call_id(self.payload))  # AC4's leave rule is off then
        self._check_devices()  # rosters that came before the acceptance
        if media.codec != PT_MULTICHANNEL:
            log.warning("call_codec_not_multichannel", codec=media.codec)
        conn.remote_username, conn.remote_password = media.ufrag, media.pwd
        for line in media.candidates:
            try:
                candidate = aioice.Candidate.from_sdp(line)
            except ValueError:
                continue
            if candidate.transport.lower() == "udp":
                await conn.add_remote_candidate(candidate)
        await conn.add_remote_candidate(None)
        try:
            await asyncio.wait_for(conn.connect(), STEP_TIMEOUT_S)
        except (TimeoutError, ConnectionError) as exc:
            raise JoinError(f"ICE: {type(exc).__name__}") from None
        if not self._remote_key:  # else a renegotiation answered meanwhile already keyed it
            self._key_rx(media.key)
        self._spawn(self._receive(conn))
        self._spawn(self._send_silence(conn, srtp_session(self._key, inbound=False)))
        if self.ended.is_set():  # ended once accepted (my last device gone, a renegotiation failed...)
            raise JoinError(self.end_reason, retry=self.retryable)
        log.info("call_joined", call=call_id(self.payload), own_msi=media.msi, codec=media.codec)

    async def _receive(self, conn: aioice.Connection) -> None:
        """Every exit but leave()'s cancel ends the call: a follower must never sit joined and deaf."""
        timeline, decoder = Timeline(), Decoder()
        reason = "media: receiver failed"
        try:
            while True:
                try:
                    data = await asyncio.wait_for(conn.recv(), MEDIA_IDLE_S)
                except TimeoutError:
                    reason = "media: nothing received"
                    return
                except ConnectionError:
                    reason = "media: ICE connection lost"
                    return
                try:
                    self._packet(data, timeline, decoder)
                except Exception as exc:  # one bad packet, or a speaker's line failing, costs only itself
                    n = self.stats["undecodable"] = self.stats["undecodable"] + 1
                    if n & (n - 1) == 0:  # 1, 2, 4, 8...: a lasting failure stays visible, not flooding
                        log.warning(
                            "call_packet_failed", error=str(exc), error_type=type(exc).__name__, count=n
                        )
        except asyncio.CancelledError:
            reason = ""
            raise
        finally:
            if reason:
                self._end(reason)

    def _packet(self, data: bytes, timeline: Timeline, decoder: Decoder) -> None:
        if is_rtcp(data):
            self.stats["rtcp"] += 1
            if self.capture is not None:
                self.capture.rtcp(data)
            return
        assert self._rx is not None  # keyed before the receiver starts
        try:
            plain = self._rx.unprotect(data)
        except pylibsrtp.Error:
            self.stats["undecryptable"] += 1
            return
        pkt = parse_rtp(plain)
        if pkt is None:
            self.stats["malformed"] += 1
            return
        self.stats["packets"] += 1
        if self.capture is not None:
            self.capture.rtp(pkt)
        if pkt.pt != PT_MULTICHANNEL:
            return
        t = timeline.place(pkt.ssrc, pkt.ts, self._clock())
        for msi, block in blocks(pkt.payload):
            self.stats["blocks"] += 1
            self.on_audio(msi, decoder.decode(msi, block), t)

    async def _send_silence(self, conn: aioice.Connection, tx: pylibsrtp.Session) -> None:
        seq, ts = secrets.randbits(16), secrets.randbits(32)
        due = time.monotonic()
        while True:
            await conn.send(tx.protect(rtp_packet(PT_PCMU, seq, ts, self._ssrc, PCMU_SILENCE)))
            seq, ts = seq + 1, ts + 160
            due += FRAME_S
            await asyncio.sleep(max(0.0, due - time.monotonic()))

    async def leave(self) -> int | None:
        """Hangs up (`DELETE` on the call leg; a meeting leg has no end link, and `POST .../end` answers
        404 and leaves a ghost participant) and closes everything; the DELETE status, None without a leg."""
        if self._leaving is not None:
            self._leaving.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        status = None
        if self.leg:
            try:
                headers = {
                    "Authorization": f"Bearer {await self._ic3()}",
                    "X-Skypetoken": self.directory.skype_token or self.skype,
                }
                status = (await self.http.delete(_https(self.leg), headers=headers)).status_code
            except Exception as exc:  # noqa: BLE001 — a failed hang-up must not skip the closes below
                log.warning("call_leave_failed", error=str(exc), error_type=type(exc).__name__)
        log.info("call_left", call=call_id(self.payload), status=status, reason=self.end_reason, **self.stats)
        for step, close in (
            ("ice", self._close_ice),
            ("trouter", self._stop_trouter),
            ("http", self.http.aclose),
        ):
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 — each close runs whatever the previous one did
                log.warning("call_close_failed", step=step, error=str(exc), error_type=type(exc).__name__)
        if self.capture is not None:
            self.capture.close()
        return status

    async def _close_ice(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def _stop_trouter(self) -> None:
        if self._trouter is not None:
            self._trouter.cancel()
            await asyncio.gather(self._trouter, return_exceptions=True)


async def _ignore(_obj: dict[str, Any]) -> None:
    return None  # chat events: this socket only carries the call's callbacks


# --- the follower ---


async def _until(stop: threading.Event, *aws: Awaitable[Any]) -> bool:
    """Waits until one of `aws` is done or `stop` is set (by the process's signal thread); True if stopped."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    while not stop.is_set():
        done, _ = await asyncio.wait(tasks, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
        if done:
            return False
    return True


async def follow(settings: Settings, data_dir: Path, payload: dict[str, Any], stop: threading.Event) -> str:
    """Joins the call, transcribes it until `stop` is set or the call ends, leaves; returns the outcome."""
    call, thread = str(call_id(payload)), thread_of(payload)
    session: Session | None = None
    # Audio flows as soon as join() returns, the engines load after: a second or two held here.
    pending: list[tuple[int, npt.NDArray[np.int16], float]] = []
    dropped = 0

    def on_audio(msi: int, pcm: npt.NDArray[np.int16], t: float) -> None:
        nonlocal dropped
        if session is not None:
            session.feed(msi, pcm, t)
        elif len(pending) < PENDING_MAX:
            pending.append((msi, pcm, t))
        else:
            dropped += 1

    capture = settings.follow_capture / call if settings.follow_capture else None
    c = Call(settings, payload, on_audio, capture=capture)  # rebound by fresh(): the wind-down reads it
    built: Session | None = None  # closed by the wind-down even if never started

    def resolve(msi: int) -> tuple[str, str] | None:
        name, mri = c.roster.names.get(msi, ("", ""))
        if not mri:
            return None
        return name or c.directory.known_name(mri), mri

    async def fresh() -> None:
        """Hangs up `c` (its leg and sockets, before the next one opens its own), rebinds a new one on the
        same call (phase 2 on its controller, phase 1 first only if no join reached it), pauses."""
        nonlocal c
        names = c.roster.names  # the same call: a line still in the engines keeps its speaker
        await c.leave()
        # Straight on the controller the first join reached, when it got that far: no phase 1 (an ended
        # 1:1 answers 404; an ended meeting comes back with us alone, and the leave rule ends it).
        c = Call(settings, payload, on_audio, directory=c.directory, capture=capture, controller=c.controller)
        c.roster.names.update(names)
        # A call that ended meanwhile gets its evt 116 (the trigger's stop) in this pause.
        await asyncio.sleep(JOIN_RETRY_PAUSE_S)

    async def reattach() -> str | None:
        """A fresh join on the moved call, one more when the failure allows it. None when joined, else the
        follow's outcome: the call's own end (over, my last device gone) if it had one, else the move."""
        attempt = 1
        while True:
            await fresh()
            try:
                await c.join()
                return None
            except Exception as exc:  # noqa: BLE001 — fail safe: no follower sitting deaf as me
                again = attempt == 1 and _retryable(c, exc)
                log.warning(
                    "follow_reattach_failed", call=call, cause=_cause(exc), attempt=attempt, retry=again
                )
                if not again:
                    return TROUTER_MOVED if c.retryable else c.end_reason
                attempt = 2

    async def attend() -> str:
        nonlocal session, built
        for attempt in (1, 2):  # 044-AC14: one retry; a stop cancels this task, never retried
            try:
                await c.join()
                break
            except Exception as exc:  # noqa: BLE001 — any join failure is logged and written, not raised
                cause = _cause(exc)
                retry = attempt == 1 and _retryable(c, exc)
                log.warning("follow_join_failed", call=call, cause=cause, attempt=attempt, retry=retry)
                if not retry:
                    started = time.time()
                    header = transcript_header(
                        call, thread, started, settings.stt_engine, join="failed", error=cause
                    )
                    Transcript(data_dir, call, started, header).close()
                    return "join_failed"
            await fresh()
        built = Session(settings, data_dir, call, thread, resolve, header={"join": "ok"})
        await built.start()
        if dropped:
            log.warning("follow_pending_full", call=call, dropped=dropped, held=len(pending))
        for item in pending:
            built.feed(*item)
        pending.clear()
        session = built
        outcome = "call_ended"
        await c.ended.wait()
        while c.end_reason == TROUTER_MOVED:  # 044-AC13: re-attached, the transcript goes on
            log.warning("follow_reattach", call=call)
            if (failed := await reattach()) is not None:
                outcome = failed
                break
            await c.ended.wait()
        # Before the hang-up, whose evt 116 may be followed by my rejoin's 115 at once: that 115 then
        # finds this join ended and starts a follower of its own instead of being taken for ours.
        try:
            await off_loop(mark_ended, data_dir, call, join_time(payload))
        except OSError as exc:
            log.warning("follow_ended_marker_failed", call=call, error=str(exc))
        return outcome

    task = asyncio.ensure_future(attend())
    try:
        # Whatever phase it is in (join, engines loading, transcribing), a stop is not held: the
        # follower's _LEAVE_S must leave room for the DELETE.
        if await _until(stop, task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            outcome = "stopped"
        else:
            outcome = task.result()
    except BaseException:
        try:
            # follow() itself cancelled: the body must not outlive the hang-up.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:  # a second cancel during that wait must not cost the hang-up
            await _wind_down(c, built)  # the failure that got here is the one to report
        raise
    if (error := await _wind_down(c, built)) is not None:
        raise error
    return outcome


def _retryable(c: Call, exc: BaseException) -> bool:
    """No new join when the failure, or the call's own first end (over, my device gone), rules it out."""
    return c.retryable and not (isinstance(exc, JoinError) and not exc.retry)


def _cause(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, JoinError) else f"{type(exc).__name__}: {exc}"


async def _wind_down(c: Call, session: Session | None) -> BaseException | None:
    """Hangs up and closes the transcript at once, each whatever the other does (both fit in _LEAVE_S);
    the first failure. close() also stops engines a cancelled start() left loading."""
    closing = [session.close()] if session is not None else []
    outcomes = await asyncio.gather(c.leave(), *closing, return_exceptions=True)
    return next((o for o in outcomes if isinstance(o, BaseException)), None)
