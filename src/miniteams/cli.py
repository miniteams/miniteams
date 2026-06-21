"""miniteams CLI entrypoint."""

import argparse
import contextlib
import sys
from typing import Any

import structlog

from ._io import force_blocking_stdout
from .auth import acquire_aad_token
from .config import Settings
from .logging import setup_logging
from .send import NOTES_THREAD
from .skype import exchange_skype_token, persist_skype_token
from .trouter import get_or_create_epid, handshake, trouter_info

log = structlog.get_logger()


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


def cmd_update(settings: Settings, args: argparse.Namespace) -> int:
    """Edit a previously-sent message, identified by id or a Teams deep link."""
    from pathlib import Path

    from .send import edit_message, parse_message_link

    link = parse_message_link(args.target)
    if link:
        thread_id, message_id = link
    else:
        thread_id, message_id = args.thread, args.target  # bare message id + --thread

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


def cmd_stream(settings: Settings, args: argparse.Namespace) -> int:
    """Full chain → websocket → authenticate → register → stream, with auto-reconnect (M2-M4)."""
    import asyncio

    from .stream import run_forever

    try:
        asyncio.run(run_forever(settings, args.jsonl, args.raw))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="miniteams",
        description="Read-only CLI that streams live Microsoft Teams chat events.",
    )
    parser.add_argument(
        "--device-code",
        action="store_true",
        help="use device-code flow instead of browser (often CA-blocked)",
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

    p_stream = sub.add_parser("stream", help="stream live incoming chat events (M2+)")
    p_stream.add_argument(
        "--jsonl", action="store_true", help="emit full-detail JSON per event (no media download)"
    )
    p_stream.add_argument(
        "--raw",
        action="store_true",
        help="firehose NDJSON: every frame (all endpoints, presence/calls, named events), decoded",
    )
    p_stream.set_defaults(func=cmd_stream)

    args = parser.parse_args(argv)

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
