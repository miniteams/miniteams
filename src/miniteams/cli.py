"""miniteams CLI entrypoint."""

import argparse
import contextlib
import re
import sys
from datetime import UTC
from typing import Any

import structlog

from ._io import force_blocking_stdout
from .auth import AuthUnavailable, acquire_aad_token
from .config import Settings
from .logging import setup_logging
from .send import NOTES_THREAD
from .skype import exchange_skype_token, persist_skype_token
from .trouter import get_or_create_epid, handshake, trouter_info

log = structlog.get_logger()

_NET_RETRIES = 4  # consecutive transport failures an archive run may burn before giving up
_NET_RETRY_MAX_BACKOFF = 30.0


def _ensure_skype_token(settings: Settings) -> tuple[dict[str, Any], str]:
    """Silent AAD (cached) → fresh skype token. Returns (aad_result, skype_token)."""
    aad = acquire_aad_token(settings)
    skype = exchange_skype_token(settings, aad["access_token"])
    return aad, skype["skype_token"]


def _mask(token: str, keep: int = 12) -> str:
    if len(token) <= keep * 2:
        return f"<{len(token)} chars>"
    return f"{token[:keep]}…{token[-keep:]} ({len(token)} chars)"


def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    """M0: prove client_id/scope/authz by minting an AAD token and a skype token."""
    aad = acquire_aad_token(settings)
    access_token = aad["access_token"]
    log.info("aad_token_acquired", expires_in=aad.get("expires_in"))

    skype = exchange_skype_token(settings, access_token)
    path = persist_skype_token(settings, skype)
    log.info("skype_token_persisted", path=str(path))

    print(f"\n--- M0: tokens acquired ({'FULL' if args.raw else 'masked'}) ---", file=sys.stderr)
    if args.raw:
        print(f"AAD access_token:\n{access_token}\n")
        print(f"Skype token:\n{skype['skype_token']}\n")
    else:
        print(f"AAD access_token: {_mask(access_token)}")
        print(f"Skype token:      {_mask(skype['skype_token'])}")
    return 0


def cmd_handshake(settings: Settings, args: argparse.Namespace) -> int:
    """M1: info call → surl+connectparams; session handshake → sessionId."""
    _, skype_token = _ensure_skype_token(settings)
    epid = get_or_create_epid(settings)
    info = trouter_info(settings, skype_token, epid)
    session_id = handshake(settings, info, skype_token, epid)

    print("\n--- M1: trouter handshake ---", file=sys.stderr)
    print(f"epid:          {epid}")
    print(f"socketio:      {info['socketio']}")
    print(f"surl:          {info['surl']}")
    print(f"connectparams: {sorted(info['connectparams'])}")
    print(f"sessionId:     {session_id}")
    return 0


def cmd_send(settings: Settings, args: argparse.Namespace) -> int:
    """Send a message — defaults to your own Notes (write-safe target)."""
    from pathlib import Path

    from .send import send_message

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        log.error("send_no_input", hint="provide TEXT or --file")
        return 2

    aad, skype_token = _ensure_skype_token(settings)
    display_name = (aad.get("id_token_claims") or {}).get("name") or ""
    send_message(settings, skype_token, args.thread, text, display_name, is_html=args.html)
    print(f"sent → {args.thread}", file=sys.stderr)
    return 0


# Re-sent every 5 s, the indicator stayed up without a gap in the Teams client (2026-10-01).
_TYPING_RESEND = 5.0


def cmd_typing(settings: Settings, args: argparse.Namespace) -> int:
    """Show your typing indicator in a chat: once, or held for about --for seconds."""
    import time

    from .send import send_typing

    _, skype_token = _ensure_skype_token(settings)
    # ponytail: the skype token is not refreshed, a hold longer than its ~45 min life fails.
    deadline = time.monotonic() + args.duration
    try:
        while True:
            send_typing(settings, skype_token, args.thread)
            if deadline - time.monotonic() <= _TYPING_RESEND:
                break
            time.sleep(_TYPING_RESEND)
    except KeyboardInterrupt:
        log.info("interrupted")
    print(f"typing → {args.thread}", file=sys.stderr)
    return 0


def _message_target(args: argparse.Namespace) -> tuple[str, str]:
    """(thread, message id) from a Teams deep link, else a bare message id + --thread."""
    from .send import parse_message_link

    return parse_message_link(args.target) or (args.thread, args.target)


def cmd_update(settings: Settings, args: argparse.Namespace) -> int:
    """Edit a previously-sent message, identified by id or a Teams deep link."""
    from pathlib import Path

    from .send import edit_message

    thread_id, message_id = _message_target(args)
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        log.error("update_no_input", hint="provide TEXT or --file")
        return 2

    _, skype_token = _ensure_skype_token(settings)
    edit_message(settings, skype_token, thread_id, message_id, text, is_html=args.html)
    print(f"edited → {thread_id}/{message_id}", file=sys.stderr)
    return 0


def cmd_react(settings: Settings, args: argparse.Namespace) -> int:
    """Add or remove your reaction on a message, identified by id or a Teams deep link."""
    from .send import react

    thread_id, message_id = _message_target(args)
    _, skype_token = _ensure_skype_token(settings)
    react(settings, skype_token, thread_id, message_id, args.key, remove=args.remove)
    print(
        f"{'unreacted' if args.remove else 'reacted'} {args.key} → {thread_id}/{message_id}", file=sys.stderr
    )
    return 0


def cmd_emojis(settings: Settings, args: argparse.Namespace) -> int:
    """List the organisation's custom emojis with their creator and creation date."""
    import asyncio

    from .auth import token_source
    from .emojis import list_emojis

    aad, skype_token = _ensure_skype_token(settings)
    csa_token = token_source(settings).csa_token()
    if not csa_token:
        log.error("csa_token_unavailable", hint="run `miniteams login`")
        return 1
    bearer = str(aad.get("id_token") or aad["access_token"])
    asyncio.run(
        list_emojis(settings, skype_token, bearer, csa_token, jsonl=args.jsonl, download=args.download)
    )
    return 0


def cmd_dump(settings: Settings, args: argparse.Namespace) -> int:
    """Dump a conversation's full message history (default: your Notes)."""
    import asyncio

    from .dump import dump_conversation

    aad, skype_token = _ensure_skype_token(settings)
    bearer = str(aad.get("id_token") or aad["access_token"])
    asyncio.run(
        dump_conversation(
            settings, skype_token, args.thread, args.page_size, args.max_pages, args.jsonl, bearer
        )
    )
    return 0


def cmd_chats(settings: Settings, args: argparse.Namespace) -> int:
    """List recent chats (default: private 1:1/group only), newest activity first."""
    import asyncio

    from .chats import list_chats, parse_when

    since = parse_when(args.since) if args.since else None
    until = parse_when(args.until, end=True) if args.until else None
    aad, skype_token = _ensure_skype_token(settings)
    bearer = str(aad.get("id_token") or aad["access_token"])
    asyncio.run(list_chats(settings, skype_token, bearer, args.limit, since, until, args.all, args.jsonl))
    return 0


def cmd_web(settings: Settings, args: argparse.Namespace) -> int:
    """Serve the live conversation-list widget on a loopback address (spec 002)."""
    import asyncio
    from pathlib import Path

    from .web import run, self_mri

    aad, skype_token = _ensure_skype_token(settings)
    bearer = str(aad.get("id_token") or aad["access_token"])
    me = self_mri(str(aad["access_token"]))
    if not me:
        log.warning("self_mri_unknown")  # mentions cannot be detected; the list still works
    try:
        asyncio.run(
            run(
                settings,
                skype_token,
                bearer,
                args.limit,
                args.bind,
                args.reactions,
                args.opener,
                args.open_scheme,
                args.browser,
                me=me,
                data_dir=Path(args.data_dir),
            )
        )
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


def cmd_archive(settings: Settings, args: argparse.Namespace) -> int:
    """Build/refresh a resumable local archive of chats under data/ (see spec 001)."""
    import asyncio
    import time
    from pathlib import Path

    import httpx

    from .archive import RefreshingToken, run_archive
    from .auth import token_source

    # Acquire once (may prompt), then hand a self-refreshing provider to the run: a full-account
    # archive outlives the ~45-min skype token, so it must be re-minted mid-run.
    source = token_source(settings)
    source.acquire()
    provider = RefreshingToken(settings, source)
    if args.live:
        from .archive_live import run_live

        try:
            asyncio.run(
                run_live(
                    settings,
                    Path(args.data_dir),
                    token_provider=provider,
                    include_all=args.all,
                    download_media=not args.no_media,
                    download_avatars=not args.no_avatars,
                    download_videos=args.videos,
                    reconcile=args.live,
                )
            )
        except KeyboardInterrupt:
            log.info("interrupted")
            return 0
        log.error("archive_stopped", reason="auth_expired", hint="run `miniteams login`")
        return 1
    net_failures = 0
    try:
        while True:
            try:
                auth_expired = asyncio.run(
                    run_archive(
                        settings,
                        Path(args.data_dir),
                        token_provider=provider,
                        thread=args.thread,
                        include_all=args.all,
                        download_media=not args.no_media,
                        download_avatars=not args.no_avatars,
                        download_videos=args.videos,
                        verify_media=args.verify_media,
                        assets_only=args.assets_only,
                        retry_denied=args.retry_denied,
                        retry_assets=args.retry_assets,
                        recheck_since=args.recheck_since,
                    )
                )
            # OSError also covers msal's transport layer: requests.exceptions.RequestException
            # subclasses it, so a reset during a token refresh lands here too.
            except (OSError, AuthUnavailable, httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise  # 401/403/404 do not heal by waiting
                reason = "auth" if isinstance(exc, AuthUnavailable) else "network"
                net_failures += 1
                if net_failures > _NET_RETRIES:
                    log.error("archive_stopped", reason=reason, attempts=net_failures, error=str(exc))
                    return 1
                delay = min(2.0**net_failures, _NET_RETRY_MAX_BACKOFF)
                log.warning(
                    "archive_retry",
                    reason=reason,
                    attempt=net_failures,
                    of=_NET_RETRIES,
                    delay=delay,
                    error=str(exc),
                )
                time.sleep(delay)
                continue  # the archive is reentrant: a fresh run resumes where this one died
            net_failures = 0  # a run that reached its recap clears the streak
            if auth_expired:
                log.error("archive_stopped", reason="auth_expired", hint="run `miniteams login`")
                return 1
            if args.loop is None:
                return 0
            log.info("archive_loop_sleep", seconds=args.loop)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0


def cmd_watch(settings: Settings, args: argparse.Namespace) -> int:
    """Print one JSON line per live event matching the criteria (spec 009)."""
    import asyncio

    from .watch import run_watch

    try:
        return asyncio.run(run_watch(settings, args.watch))
    except KeyboardInterrupt:
        return 0


def cmd_mcp(settings: Settings, args: argparse.Namespace) -> int:
    """Serve the MCP tools over stdin/stdout (spec 008)."""
    from pathlib import Path

    from .mcp import Server

    return Server(settings, Path(args.data_dir), read_only=args.read_only).serve()


def cmd_stream(settings: Settings, args: argparse.Namespace) -> int:
    """Full chain → websocket → authenticate → register → stream, with auto-reconnect (M2-M4)."""
    import asyncio

    from .stream import run_forever

    # Stream is long-running and often headless (server/SSH) where a localhost browser redirect
    # can't complete; default the interactive fallback to device-code. A cached token still goes
    # silent; `--device-code` is implied here.
    settings.use_device_code = True
    try:
        asyncio.run(run_forever(settings, args.jsonl, args.raw, args.typing))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


def _positive_int(value: str) -> int:
    # --loop 0 would hot-loop the API with no pause; negatives crash time.sleep. Reject both.
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _utc_floor(value: str) -> str:
    """ISO date/datetime → UTC `YYYY-MM-DDTHH:MM:SS`, comparable as text with message timestamps."""
    from .chats import parse_when

    try:
        return parse_when(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date/datetime: {value!r}") from exc


def _reaction_key(value: str) -> str:
    # Keys are opaque (`yes-tone1`, `starMSER`, `name;0-frc-d4-<hash>`); only blanks are surely wrong.
    if not value or re.search(r"\s", value):
        raise argparse.ArgumentTypeError("a Teams reaction key, without spaces")
    return value


def _non_negative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="miniteams",
        description="Read-only CLI that streams live Microsoft Teams chat events.",
    )
    parser.add_argument(
        "--device-code",
        action="store_true",
        help="authenticate via device-code flow instead of the browser",
    )
    parser.add_argument("--log-level", default=None, help="override MINITEAMS_LOG_LEVEL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="acquire + cache AAD and skype tokens (M0)")
    p_login.add_argument("--raw", action="store_true", help="print full tokens (secrets!)")
    p_login.set_defaults(func=cmd_login)

    p_handshake = sub.add_parser("handshake", help="trouter info + session handshake (M1)")
    p_handshake.set_defaults(func=cmd_handshake)

    p_dump = sub.add_parser("dump", help="dump a conversation's full message history")
    p_dump.add_argument(
        "--thread", default=NOTES_THREAD, help="conversation/thread id (default: Notes to self)"
    )
    p_dump.add_argument("--page-size", type=int, default=30, help="messages per page")
    p_dump.add_argument("--max-pages", type=int, default=0, help="0 = fetch all pages")
    p_dump.add_argument(
        "--jsonl", action="store_true", help="emit full-detail JSON per line (no media download)"
    )
    p_dump.set_defaults(func=cmd_dump)

    p_send = sub.add_parser("send", help="send a message (default target: your Notes)")
    p_send.add_argument("text", nargs="?", help="message text (omit when using --file)")
    p_send.add_argument("--file", help="read message body from this file instead of TEXT")
    p_send.add_argument(
        "--html",
        action="store_true",
        help="send content as raw RichText/Html (no escaping) for formatting",
    )
    p_send.add_argument(
        "--thread",
        default=NOTES_THREAD,
        help="target conversation/thread id (default: Notes to self)",
    )
    p_send.set_defaults(func=cmd_send)

    p_update = sub.add_parser("update", help="edit a sent message (by id or Teams deep link)")
    p_update.add_argument("target", help="message id, or a /l/message/<thread>/<id> Teams link")
    p_update.add_argument("text", nargs="?", help="new message text (omit when using --file)")
    p_update.add_argument("--file", help="read new body from this file instead of TEXT")
    p_update.add_argument("--html", action="store_true", help="send content as raw RichText/Html")
    p_update.add_argument(
        "--thread",
        default=NOTES_THREAD,
        help="thread id when target is a bare message id (default: Notes to self)",
    )
    p_update.set_defaults(func=cmd_update)

    p_react = sub.add_parser("react", help="add or remove your reaction on a message (id or deep link)")
    p_react.add_argument("target", help="message id, or a /l/message/<thread>/<id> Teams link")
    p_react.add_argument(
        "key",
        type=_reaction_key,
        help="reaction key (case-sensitive): like, heart, laugh, 1f525_fire, yes-tone1, or a custom "
        "emoji's key from `emojis`",
    )
    p_react.add_argument("--remove", action="store_true", help="remove your reaction instead")
    p_react.add_argument(
        "--thread",
        default=NOTES_THREAD,
        help="thread id when target is a bare message id (default: Notes to self)",
    )
    p_react.set_defaults(func=cmd_react)

    p_typing = sub.add_parser("typing", help="show your typing indicator in a chat (default: your Notes)")
    p_typing.add_argument(
        "--thread",
        default=NOTES_THREAD,
        help="target conversation/thread id (default: Notes to self)",
    )
    p_typing.add_argument(
        "--for",
        dest="duration",
        type=_non_negative_int,
        default=0,
        metavar="SECONDS",
        help="keep the indicator up for about this long (default: a single send); Ctrl-C stops it",
    )
    p_typing.set_defaults(func=cmd_typing)

    p_emojis = sub.add_parser("emojis", help="list the organisation's custom emojis (creator, date)")
    p_emojis.add_argument("--jsonl", action="store_true", help="one JSON object per emoji")
    p_emojis.add_argument(
        "--download", action="store_true", help="save each image under the cache dir (emojis/)"
    )
    p_emojis.set_defaults(func=cmd_emojis)

    p_chats = sub.add_parser("chats", help="list recent private chats (newest activity first)")
    p_chats.add_argument("--limit", type=int, default=20, help="max chats to list (0 = no limit)")
    p_chats.add_argument("--since", help="ISO date/datetime — only chats with activity at/after")
    p_chats.add_argument(
        "--until", help="ISO date/datetime — only chats with activity before (date = whole day)"
    )
    p_chats.add_argument("--all", action="store_true", help="include channels and meeting chats too")
    p_chats.add_argument("--jsonl", action="store_true", help="emit one JSON object per chat")
    p_chats.set_defaults(func=cmd_chats)

    p_archive = sub.add_parser("archive", help="build/refresh a resumable local chat archive")
    p_archive.add_argument("--data-dir", default="data", help="archive root (default: ./data)")
    p_archive.add_argument("--thread", help="archive only this conversation (skip enumeration)")
    p_archive.add_argument("--all", action="store_true", help="include channels too")
    p_archive.add_argument(
        "--no-media", action="store_true", help="skip downloading attachments (messages only)"
    )
    p_archive.add_argument(
        "--no-avatars", action="store_true", help="skip downloading group icons / member avatars"
    )
    p_archive.add_argument(
        "--videos",
        action="store_true",
        help="also download meeting recordings and shared video files (large — expect "
        "hundreds of MB per hour of meeting; combine with --verify-media or --assets-only "
        "to sweep chats already archived without videos)",
    )
    p_archive.add_argument(
        "--verify-media",
        action="store_true",
        help="re-check every message's assets against disk (even backfilled chats), "
        "downloading any that are missing",
    )
    p_archive.add_argument(
        "--assets-only",
        action="store_true",
        help="fast recovery pass: only download missing assets for already-archived chats "
        "(no history, no metadata, no avatars, no network enumeration)",
    )
    p_archive.add_argument(
        "--retry-denied",
        action="store_true",
        help="re-attempt chats whose history previously came back 403 (normally skipped for good)",
    )
    p_archive.add_argument(
        "--retry-assets",
        action="store_true",
        help="re-attempt assets the archive gave up on, skipping the backoff: both the 403-denied "
        "(normally permanent) and the 404 ones still waiting out their retry delay",
    )
    p_archive.add_argument(
        "--loop",
        type=_positive_int,
        nargs="?",
        const=300,
        metavar="SECONDS",
        help="re-run the archive indefinitely, sleeping SECONDS between runs (default 300); "
        "stops when authentication breaks",
    )
    p_archive.add_argument(
        "--live",
        type=_positive_int,
        nargs="?",
        const=1800,
        metavar="SECONDS",
        help="follow live events: catch up once, then write each message as it arrives; "
        "re-catches up after a disconnect and every SECONDS (default 1800) (spec 005)",
    )
    p_archive.add_argument(
        "--recheck-since",
        type=_utc_floor,
        metavar="WHEN",
        help="one-shot: re-walk the history of every chat active since WHEN (ISO date/datetime) "
        "down to it, filling any hole under the newest stored message",
    )
    p_archive.set_defaults(func=cmd_archive)

    p_web = sub.add_parser("web", help="serve the live conversation-list widget (local page)")
    p_web.add_argument(
        "--limit",
        type=_non_negative_int,
        default=None,
        help="max conversations to load (0 = no limit; default 400 with an archive, 250 without)",
    )
    p_web.add_argument("--data-dir", default="data", help="archive root to seed rows from (default: ./data)")
    p_web.add_argument("--bind", help="ADDR:PORT to listen on (default: persisted random 127.0.0.X:PORT)")
    p_web.add_argument(
        "--reactions", action="store_true", help="a reaction becomes the row's last event and bumps it"
    )
    p_web.add_argument(
        "--opener",
        default="xdg-open",
        help="command run with the chat's deep link on row click (default: xdg-open; 'none' = the page "
        "just follows its https link)",
    )
    p_web.add_argument(
        "--open-scheme",
        choices=["msteams", "https"],
        default="msteams",
        help="deep-link scheme handed to --opener: msteams = desktop client, https = Teams web",
    )
    p_web.add_argument(
        "--browser",
        default="",
        help="command run with the https link on Ctrl/middle click (e.g. firefox); default: the page's "
        "own browser follows the link",
    )
    p_web.set_defaults(func=cmd_web)

    p_watch = sub.add_parser("watch", help="one JSON line per live event matching the criteria (spec 009)")
    p_watch.add_argument("--event", choices=["message", "reaction"], default="message")
    p_watch.add_argument("--thread", default="", help="only this chat (a chat id)")
    p_watch.add_argument("--from", dest="sender", choices=["me", "others", "anyone"], default="anyone")
    p_watch.add_argument(
        "--after", default="", help="only messages newer than this message id; also catches up"
    )
    p_watch.add_argument("--reaction", default="", help="only this emotion key (like, heart, ...)")
    p_watch.add_argument("--once", action="store_true", help="exit after the first match")
    p_watch.add_argument("--note", default="", help="free text repeated in every line")
    p_watch.set_defaults(func=cmd_watch)

    p_mcp = sub.add_parser("mcp", help="serve the MCP tools over stdio (for Claude Code and co)")
    p_mcp.add_argument("--data-dir", default="data", help="archive root to read (default: ./data)")
    p_mcp.add_argument("--read-only", action="store_true", help="expose no tool that writes to Teams")
    p_mcp.set_defaults(func=cmd_mcp)

    p_stream = sub.add_parser("stream", help="stream live incoming chat events (M2+)")
    p_stream.add_argument(
        "--jsonl", action="store_true", help="emit full-detail JSON per event (no media download)"
    )
    p_stream.add_argument(
        "--raw",
        action="store_true",
        help="firehose NDJSON: every frame (all endpoints, presence/calls, named events), decoded",
    )
    p_stream.add_argument(
        "--typing", action="store_true", help="show typing indicators (✍ is typing / stopped)"
    )
    p_stream.set_defaults(func=cmd_stream)

    args = parser.parse_args(argv)
    # A one-shot override on a timer stops being an override: it would re-request every dead asset
    # every cycle, which is exactly the hammering the backoff exists to stop.
    for flag in ("retry_assets", "recheck_since"):
        if getattr(args, flag, None) and getattr(args, "loop", None) is not None:
            name = flag.replace("_", "-")
            parser.error(f"--{name} is a one-shot recovery flag; it cannot be combined with --loop")
    # --assets-only never fetches history: the recheck would silently do nothing.
    if getattr(args, "recheck_since", None) and getattr(args, "assets_only", False):
        parser.error("--recheck-since walks history; it cannot be combined with --assets-only")
    # --live runs forever over every chat: a single thread, a timer or a one-shot recovery contradict it.
    if getattr(args, "live", False):
        flags = ("loop", "thread", "assets_only", "retry_assets", "recheck_since")
        clash = [f for f in flags if getattr(args, f) not in (None, False)]
        if clash:
            parser.error(f"--live cannot be combined with --{clash[0].replace('_', '-')}")
    if getattr(args, "func", None) is cmd_watch:
        from .watch import Watch

        args.watch = Watch(
            args.event, args.thread, args.sender, args.after, args.reaction, args.once, args.note
        )
        try:
            args.watch.validate()
        except ValueError as exc:
            parser.error(str(exc))
    # Videos ride the media pass; without it the flag would be a silent no-op.
    if getattr(args, "videos", False) and getattr(args, "no_media", False):
        parser.error("--videos requires media downloads; drop --no-media")

    settings = Settings()  # type: ignore[call-arg]  # tenant_id comes from env/.env
    if args.device_code:
        settings.use_device_code = True
    if args.log_level:
        settings.log_level = args.log_level
    setup_logging(settings.log_level)
    # Covers the sync commands (login/handshake/send); the async commands re-apply it inside
    # their event loop (see _io.force_blocking_stdout).
    force_blocking_stdout()

    try:
        return int(args.func(settings, args))
    except BrokenPipeError:
        # Downstream reader (head/jq/…) closed the pipe — exit quietly without a traceback.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard; report and exit non-zero
        log.error("command_failed", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
