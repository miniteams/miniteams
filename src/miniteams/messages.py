"""Chat messages: inbound delivery decode and printing, and the pure readers every view shares.

Trouter delivers each event as a pseudo-HTTP request whose `body` is a stringified (and often
gzip+base64-wrapped) JSON. Chat messages arrive on the `/messaging` endpoint as `EventMessage`
notifications; everything else (presence, calls) is ignored for the MVP dump. The readers (text,
snippet, deleted/edited, mentions, `fold`) serve the stream, the web views, the archive and MCP.
"""

import base64
import gzip
import html
import json
import re
import unicodedata
from typing import Any

import structlog

from . import attachments
from ._io import emit
from .directory import Directory
from .emoji_table import SHORTCODES

log = structlog.get_logger()

# What a person writes: quotable, editable, worth printing — not Control/*, calls or roster changes.
TEXT_TYPES = frozenset({"RichText/Html", "Text"})
_MEDIA_TYPES = {
    "RichText/Media_GenericFile",
    "RichText/UriObject",
    "RichText/Media_Card",
    "RichText/Media_CallRecording",
    "RichText/Media_LocalRecording",
}
_PRINTABLE = TEXT_TYPES | _MEDIA_TYPES
TAG_RE = re.compile(r"<[^>]+>")
_EMOJI_ALT_RE = re.compile(r'<emoji\b[^>]*\balt="([^"]*)"[^>]*>')
# A forward is a blockquote too, but its content IS the message — only a reply quotes another.
# Repeated: a reply quoting two messages carries two Reply blocks.
_REPLY_QUOTE_RE = re.compile(r"\A(?:\s*<blockquote\b(?![^>]*Forward)[^>]*>.*?</blockquote>)+", re.DOTALL)
SNIPPET_LEN = 1000  # the row tooltip shows it whole; CSS truncates the visible line
# Non-text last messages: a short marker instead of the raw HTML/card payload.
_TYPE_MARKERS = {
    "RichText/Media_GenericFile": "📎 file",
    "RichText/UriObject": "🖼 image",
    "RichText/Media_Card": "🃏 card",
    "RichText/Media_CallRecording": "🎥 recording",
    "RichText/Media_LocalRecording": "🎥 recording",
    "ThreadActivity/AddMember": "👥 member added",
    "ThreadActivity/DeleteMember": "👥 member removed",
    "ThreadActivity/TopicUpdate": "✎ renamed",
    "ThreadActivity/MemberConsumptionHorizonUpdate": "👁 read marker",
    "Event/Call": "📞 call started",
}
_THREAD_RE = re.compile(r"/conversations/([^/]+)")

# Teams' names for its default reactions, first name of an emoji first.
EMOJI = {
    "like": "👍",
    "heart": "❤️",
    "laugh": "😆",
    "surprised": "😮",
    "sad": "😢",
    "angry": "😡",
    "yes": "👍",
    "no": "👎",
    "praying": "🙏",
    "upsidedownface": "🙃",
}

# Teams names differ from the composer's GitHub/Slack shortcodes.
_REACTION_ALIASES = {
    "crossedfingers": "crossed_fingers",
    "launch": "rocket",
    "loudlycrying": "sob",
    "clappinghands": "clap",
    "handsinair": "raised_hands",
    "victory": "v",
    "ok": "ok_hand",
    "raisedfist": "fist_raised",
    "pointupindex": "point_up",
    "fistbump": "punch",
    "screamingfear": "scream",
    "think": "thinking",
    "sweatgrinning": "sweat_smile",
    "upsidedownface": "upside_down_face",
    "cwl": "joy",
    "hearteyes": "heart_eyes",
    "seenoevil": "see_no_evil",
    "speaknoevil": "speak_no_evil",
    "puke": "vomiting_face",
    "lipssealed": "zipper_mouth_face",
    "hi": "wave",
    "heartorange": "orange_heart",
    "heartgreen": "green_heart",
    "heartyellow": "yellow_heart",
    "angryface": "angry",
    "devil": "smiling_imp",
    "tongueout": "stuck_out_tongue",
    "grinningfacewithsmilingeyes": "smile",
}

# Only Unicode Emoji_Modifier_Base characters can take a skin tone.
# https://www.unicode.org/Public/17.0.0/ucd/emoji/emoji-data.txt
_TONE_BASES = frozenset(
    "☝⛹✊✋✌✍🎅🏂🏃🏄🏇🏊🏋🏌👂👃👆👇👈👉👊👋👌👍👎👏👐👦👧👨👩👪👫👬👭"
    "👮👯👰👱👲👳👴👵👶👷👸👼💁💂💃💅💆💇💏💑💪🕴🕵🕺🖐🖕🖖🙅🙆🙇🙋🙌🙍🙎🙏"
    "🚣🚴🚵🚶🛀🛌🤌🤏🤘🤙🤚🤛🤜🤝🤞🤟🤦🤰🤱🤲🤳🤴🤵🤶🤷🤸🤹🤼🤽🤾🥷🦵🦶🦸🦹"
    "🦻🧍🧎🧏🧑🧒🧓🧔🧕🧖🧗🧘🧙🧚🧛🧜🧝🫃🫄🫅🫰🫱🫲🫳🫴🫵🫶🫷🫸"
)


def reaction_emoji(key: str) -> str:
    """Resolve Teams names, Unicode keys and native -tone1 through -tone5 suffixes."""
    base, sep, tone = key.rpartition("-tone")
    if not sep:
        return _reaction_base(key)
    if tone not in ("1", "2", "3", "4", "5") or "-tone" in base:
        return f":{key}:"
    emoji = _reaction_base(base)
    if emoji == f":{base}:":
        return f":{key}:"
    positions = [i for i, char in enumerate(emoji) if char in _TONE_BASES]
    # Multi-person and already toned sequences need an explicit Unicode key.
    if len(positions) != 1 or any("🏻" <= char <= "🏿" for char in emoji):
        return f":{key}:"
    i = positions[0] + 1
    tail = emoji[i:].removeprefix("\ufe0f")
    return emoji[:i] + chr(0x1F3FA + int(tone)) + tail


def _reaction_base(key: str) -> str:
    """Resolve legacy names and Teams' hexadecimal Unicode reaction keys."""
    if key in EMOJI:
        return EMOJI[key]
    name = _REACTION_ALIASES.get(key, key)
    if name in SHORTCODES:
        return SHORTCODES[name]
    codes, sep, _ = key.partition("_")
    if sep and (chars := _code_points(codes)):
        return chars
    # Custom org reactions carry `;<id>` after their name.
    return f":{key.partition(';')[0]}:"


def _code_points(codes: str) -> str | None:
    """`1f9d1-200d-1f4bb` → its characters; None for anything a remote key must not carry."""
    if not re.fullmatch(r"[0-9a-fA-F]{4,6}(?:-[0-9a-fA-F]{4,6}){0,15}", codes):
        return None
    points = [int(code, 16) for code in codes.split("-")]
    # Reject controls, surrogates, unassigned, format (bidi) and line/paragraph separators from
    # remote keys; emoji sequences need the ZWJ and tag format characters.
    if all(
        point <= 0x10FFFF
        and (
            unicodedata.category(chr(point)) not in ("Cc", "Cs", "Cn", "Cf", "Zl", "Zp")
            or point == 0x200D
            or 0xE0020 <= point <= 0xE007F
        )
        for point in points
    ):
        return "".join(chr(point) for point in points)
    return None


# Character (no U+FE0F) → Teams' name for it; EMOJI first, so 👍 is `like`, not `yes`.
_TEAMS_NAMES = {
    char.replace("\ufe0f", ""): name
    for name, char in reversed([*EMOJI.items(), *((a, SHORTCODES[t]) for a, t in _REACTION_ALIASES.items())])
}

REACTION_KEY_MAX = 200
# Disjoint by their first character: Teams names start with a letter, Unicode keys with a digit.
_TEAMS_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}(?:-tone[1-5])?")
_UNICODE_KEY_RE = re.compile(
    r"(?P<codes>[0-9][0-9a-fA-F]{3,5}(?:-[0-9a-fA-F]{4,6}){0,15})(?:_\w+)?", re.ASCII
)
_CUSTOM_KEY_RE = re.compile(r"[A-Za-z0-9_+-]{1,64};[\w-]{1,128}", re.ASCII)  # compose.EMOJI_NAME_RE;<AMS id>


def reaction_key(key: str) -> str | None:
    """The key to send for `key` (spec 016): a Teams name, a Unicode key or a custom `<name>;<id>` as
    given; bare code points (`1f64f`) become Teams' name for them (`praying`) or `<hex>_<unicode name>`
    (`1f98a_foxface`, Teams' own form). None for anything else."""
    if len(key) > REACTION_KEY_MAX:
        return None
    if _TEAMS_NAME_RE.fullmatch(key) or _CUSTOM_KEY_RE.fullmatch(key):
        return key
    if not (m := _UNICODE_KEY_RE.fullmatch(key)) or not (chars := _code_points(m["codes"])):
        return None
    if m["codes"] != key:
        return key
    if name := _TEAMS_NAMES.get(chars.replace("\ufe0f", "")):
        return name
    # Teams' keys leave out a final presentation selector (`2665_blackheartsuit` for ♥️).
    chars = chars.removesuffix("\ufe0f")
    words = re.sub(
        r"[^a-z0-9]", "", "".join(unicodedata.name(c, "") for c in chars if c not in "\u200d\ufe0f").lower()
    )
    if not words:
        return None
    return "-".join(f"{ord(c):04x}" for c in chars) + "_" + words


def _gunzip_b64(data: str) -> str:
    return gzip.decompress(base64.b64decode(data)).decode("utf-8")


def _decode_body(req: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap the three documented encodings into the real payload object."""
    body = req.get("body")
    if not body:
        return None
    headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
    if headers.get("x-microsoft-skype-content-encoding") == "gzip":
        body = _gunzip_b64(body)
    obj = json.loads(body)
    if isinstance(obj, dict):
        if "cp" in obj:  # base64 → gunzip → real payload
            obj = json.loads(_gunzip_b64(obj["cp"]))
        elif "gp" in obj:  # base64 → parse directly
            obj = json.loads(base64.b64decode(obj["gp"]).decode("utf-8"))
    return obj if isinstance(obj, dict) else None


def strip_html(content: str) -> str:
    return html.unescape(TAG_RE.sub("", content)).strip()


def json_list(value: Any) -> list[Any]:
    """`properties.files` / `.cards` arrive as JSON *strings* (`'[]'` is truthy) or as lists."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def plain_text(content: str) -> str:
    """HTML body as one line of text.

    Emoji are tags whose `alt` holds the character; tags then become spaces, not nothing:
    `<at>Bob</at>dis` would otherwise read "Bobdis".
    """
    return " ".join(html.unescape(TAG_RE.sub(" ", _EMOJI_ALT_RE.sub(r"\1", content))).split())


def message_text(message: dict[str, Any]) -> str:
    """Plain text of a message as search and the MCP tools show it; `[Type]` for anything else."""
    msgtype = str(message.get("messagetype") or "")
    content = str(message.get("content") or "")
    text = plain_text(content) if msgtype in TEXT_TYPES else f"[{msgtype.rsplit('/', 1)[-1]}]"
    return text[:SNIPPET_LEN]


def snippet(msgtype: str, content: str, props: dict[str, Any] | None = None) -> str:
    """One-line preview of a message body, marker for non-text types, capped at SNIPPET_LEN."""
    props = props or {}
    if msgtype in TEXT_TYPES:
        text = content
        if msgtype == "RichText/Html":
            # A reply repeats the quoted message before its own body; preview what was written,
            # unless the reply is nothing but the quote.
            body = _REPLY_QUOTE_RE.sub("", content)
            text = plain_text(body) or plain_text(content)
        text = " ".join(text.split())
        if not text:
            # A file/card post has an empty body and its payload in properties; the list stub of
            # a deleted message has neither; a body that strips to nothing is an image tag.
            names = [
                str(f.get("fileName") or "file") for f in json_list(props.get("files")) if isinstance(f, dict)
            ]
            if names:
                text = "📎 " + ", ".join(names)
            elif json_list(props.get("cards")):
                text = "🃏 card"
            elif not content.strip():
                text = "🗑 deleted"
            else:
                text = "🖼 image" if "<img" in content else "📎 attachment"
    elif msgtype == "Event/Call" and "<ended/>" in content:
        text = "📞 call ended"
    else:
        text = (_TYPE_MARKERS.get(msgtype) or f"[{msgtype.rsplit('/', 1)[-1]}]") if msgtype else ""
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_LEN else text[: SNIPPET_LEN - 1] + "…"


def is_deleted(props: dict[str, Any]) -> bool:
    return bool(props.get("deletetime") or props.get("hardDeleteTime"))


def is_edited(resource: dict[str, Any]) -> bool:
    props = resource.get("properties")
    return bool(resource.get("skypeeditedid") or (isinstance(props, dict) and props.get("edittime")))


def fold(text: str) -> str:
    """Case and accents dropped, so "zoe" finds "ZOÉ" and "elodie" finds "Élodie"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def is_activity(msgtype: str) -> bool:
    """Thread activity (member changes, read markers…) is emitted by the thread, not a person."""
    return msgtype.startswith("ThreadActivity/")


def is_me(mri: str, me: str) -> bool:
    """`from` is a contact URL ending in the MRI; a call event names the bare MRI."""
    return bool(me) and mri.rsplit("/", 1)[-1] == me


def mention_kind(resource: dict[str, Any], me: str) -> str | None:
    """`"me"` when the message @mentions `me` by name, `"all"` for an @everyone, else None.

    `properties.mentions` is a JSON string (or list) of entries; `bot` and `share-contact`
    entries are not mentions of a person. A message from `me` never counts."""
    if not me or str(resource.get("from") or "").endswith(me):
        return None
    kind = None
    for entry in json_list((resource.get("properties") or {}).get("mentions")):
        if not isinstance(entry, dict):
            continue
        mtype = str(entry.get("mentionType") or "")
        if mtype == "person" and entry.get("mri") == me:
            return "me"
        if mtype == "everyone":
            kind = "all"  # the entry's mri is the thread itself; a `me` entry still wins
    return kind


def thread_of(resource: dict[str, Any]) -> str:
    link = resource.get("conversationLink") or ""
    match = _THREAD_RE.search(link)
    if match:
        return match.group(1)
    return str(resource.get("to", ""))


async def _print_message(resource: dict[str, Any], directory: Directory, tag: str = "") -> None:
    msgtype = resource.get("messagetype", "")
    if msgtype not in _PRINTABLE:
        log.debug("skipped_message", messagetype=msgtype)
        return
    sender_mri = resource.get("from", "")
    sender = resource.get("imdisplayname")
    directory.note_name(sender_mri, sender)  # feed the cache from free message metadata
    sender = sender or await directory.display(sender_mri)
    when = resource.get("composetime") or resource.get("originalarrivaltime") or ""
    label = await directory.label(thread_of(resource))
    content = resource.get("content", "")

    notes = await attachments.process(
        content,
        msgtype,
        directory.skype_token,
        directory.settings.media_dir,
        directory.settings.download_media,
        media=directory.media,
    )
    text = strip_html(content) if msgtype == "RichText/Html" else content if msgtype == "Text" else ""
    suffix = (" " + " ".join(notes)) if notes else ""
    await emit(f"[{when}] ({label}) {tag}{sender}: {text}{suffix}".rstrip() + "\n")


async def _print_deleted(resource: dict[str, Any], directory: Directory) -> None:
    when = (resource.get("properties") or {}).get("deletetime") or resource.get("composetime") or ""
    label = await directory.label(thread_of(resource))
    sender = await directory.display(resource.get("from", ""))
    await emit(f"[{when}] ({label}) 🗑 {sender} deleted a message\n")


async def print_resource(resource: dict[str, Any], directory: Directory) -> None:
    """Render one message resource — shared by the live stream and the history dump."""
    await _print_message(resource, directory)


async def emit_raw_delivery(req: dict[str, Any]) -> None:
    """Emit one inbound delivery verbatim as NDJSON — every endpoint, decoded, no filtering."""
    try:
        body: Any = _decode_body(req)
    except Exception as exc:  # noqa: BLE001 — keep the firehose flowing on a bad frame
        body = {"_decode_error": str(exc), "_raw": req.get("body")}
    record = {
        "kind": "delivery",
        "id": req.get("id"),
        "method": req.get("method"),
        "url": req.get("url"),
        "headers": req.get("headers"),
        "body": body,
    }
    await emit(json.dumps(record, ensure_ascii=False, default=str) + "\n")


async def emit_raw_named(data: str) -> None:
    """Emit a named Socket.IO event (trouter.connected, message_loss, …) as NDJSON."""
    try:
        payload: Any = json.loads(data)
    except json.JSONDecodeError:
        payload = data
    await emit(json.dumps({"kind": "named", "event": payload}, ensure_ascii=False, default=str) + "\n")


async def event_to_record(obj: dict[str, Any], req: dict[str, Any], directory: Directory) -> dict[str, Any]:
    """Full-detail JSONL record for one live stream event (max info, no media download)."""
    resource = obj.get("resource") or {}
    record: dict[str, Any] = {
        "type": obj.get("type"),
        "resourceType": obj.get("resourceType"),
        "time": obj.get("time"),
        # Trouter delivery envelope — the transport-level "tech stuff".
        "delivery": {
            "id": req.get("id"),
            "method": req.get("method"),
            "url": req.get("url"),
            "headers": req.get("headers"),
        },
    }
    if resource:
        record["message"] = await resource_to_record(resource, directory)
    else:
        record["raw"] = obj
    return record


async def resource_to_record(resource: dict[str, Any], directory: Directory) -> dict[str, Any]:
    """Full-detail structured record for JSONL output — keeps the raw resource verbatim."""
    msgtype = resource.get("messagetype", "")
    sender_mri = resource.get("from", "")
    directory.note_name(sender_mri, resource.get("imdisplayname"))
    content = resource.get("content", "")
    props = resource.get("properties") or {}
    thread_id = thread_of(resource)
    text = strip_html(content) if msgtype == "RichText/Html" else content if msgtype == "Text" else ""
    return {
        "id": resource.get("id"),
        "time": resource.get("composetime") or resource.get("originalarrivaltime"),
        "thread_id": thread_id,
        "thread_label": await directory.label(thread_id),
        "sender_mri": sender_mri,
        "sender": resource.get("imdisplayname") or await directory.display(sender_mri),
        "messagetype": msgtype,
        "text": text,
        "content_raw": content,
        "attachments": attachments.extract(content, msgtype, directory.media),
        "reactions": props.get("emotions"),
        # Surfaced technical identifiers (also present in `raw`, promoted for convenience).
        "clientmessageid": resource.get("clientmessageid"),
        "sequenceId": resource.get("sequenceId"),
        "version": resource.get("version"),
        "conversationid": resource.get("conversationid"),
        "conversation_link": resource.get("conversationLink"),
        "etag": resource.get("etag"),
        "skypeeditedid": resource.get("skypeeditedid"),
        "deletetime": props.get("deletetime"),
        "properties": props,
        "raw": resource,  # full server object, nothing dropped
    }


async def _print_reactions(resource: dict[str, Any], emotions: list[Any], directory: Directory) -> None:
    msg_id = str(resource.get("id") or resource.get("clientmessageid") or "")
    when = resource.get("composetime") or resource.get("originalarrivaltime") or ""
    label = await directory.label(thread_of(resource))
    raw = resource.get("content") or ""
    snippet = strip_html(raw)[:40] if raw else ""
    ctx = f' to "{snippet}"' if snippet else ""
    for emotion in emotions:
        key = emotion.get("key", "?")
        emoji = reaction_emoji(key)
        users = [str(u.get("mri", "")) for u in (emotion.get("users") or [])]
        added, removed = directory.reaction_diff(msg_id, key, users)
        for mri in added:
            await emit(f"[{when}] ({label}) ↳ {emoji} {await directory.display(mri)} reacted{ctx}\n")
        for mri in removed:
            await emit(f"[{when}] ({label}) ↳ {emoji}✖ {await directory.display(mri)} unreacted{ctx}\n")


async def _print_typing(resource: dict[str, Any], directory: Directory, started: bool) -> None:
    when = resource.get("composetime") or resource.get("originalarrivaltime") or ""
    label = await directory.label(thread_of(resource))
    who = await directory.display(resource.get("from", ""))
    verb = "is typing…" if started else "stopped typing"
    await emit(f"[{when}] ({label}) ✍ {who} {verb}\n")


def decode_event(req: dict[str, Any]) -> dict[str, Any] | None:
    """The `EventMessage` carried by a `/messaging` delivery, or None for anything else."""
    url = req.get("url", "")
    endpoint = url.rsplit("/", 1)[-1]
    if endpoint != "messaging":  # presence / call signaling — ignore for MVP
        log.debug("delivery_ignored", endpoint=endpoint)
        return None
    try:
        obj = _decode_body(req)
    except Exception as exc:  # noqa: BLE001 — never let one bad frame kill the stream
        log.warning("decode_failed", error=str(exc))
        return None
    if not obj or obj.get("type") != "EventMessage":
        log.debug("non_event", type=obj.get("type") if obj else None)
        return None
    return obj


async def handle_delivery(
    req: dict[str, Any], directory: Directory, jsonl: bool = False, typing: bool = False
) -> None:
    obj = decode_event(req)
    if obj is None:
        return

    if jsonl:
        record = await event_to_record(obj, req, directory)
        await emit(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return

    resource = obj.get("resource") or {}
    resource_type = obj.get("resourceType")
    if resource_type == "NewMessage":
        msgtype = resource.get("messagetype", "")
        if msgtype in ("Control/Typing", "Control/ClearTyping"):
            if typing:
                await _print_typing(resource, directory, started=msgtype == "Control/Typing")
            return
        await _print_message(resource, directory)
    elif resource_type == "MessageUpdate":
        # MessageUpdate carries reactions (emotions), deletions (deletetime), or edits.
        props = resource.get("properties") or {}
        if props.get("emotions") is not None:
            await _print_reactions(resource, props["emotions"], directory)
        elif is_deleted(props):
            await _print_deleted(resource, directory)
        elif is_edited(resource):
            await _print_message(resource, directory, tag="✏ ")
        else:
            log.debug("message_update", id=resource.get("id"))
    else:
        log.debug("ignored_resource", resource_type=resource_type)
