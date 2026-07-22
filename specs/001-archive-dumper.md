# 001 — Archive dumper

**Status**: draft
**Requested by**: babs
**Date**: 2026-07-22

## Problem

Chat history lives only on Microsoft's servers, fetchable one conversation at a time via
`dump`, with no persistence, no resume, and media only as side effects. There is no way to
build (and keep up to date) a complete local archive of all private chats — messages *and*
original-quality media — that survives interruptions and re-runs.

## Solution

`miniteams archive` builds a local archive under `data/`: one folder per conversation holding
a `messages.db` (every message as raw API JSON) and a `media/` folder (original-quality
attachments), plus a global `index.db` with chat metadata. Interrupt it anytime; re-running
resumes exactly where it stopped and picks up new messages and new chats.

## Scope

- New `archive` CLI command.
- Phase 1 enumerate: upsert all chats (private by default, `--all` for channels/meetings) into
  `data/index.db` — id, label, topic, participants, last_fetch_at, backfill_done.
- Per-chat backfill newest→oldest via `endTime` windowing (existing `fetch_history` machinery),
  one sqlite transaction per HTTP page, `INSERT OR IGNORE` on message id.
- Forward top-up on re-run: fetch newest pages until overlapping `MAX(composetime)` already stored.
- Resume cursor derived from data itself (`MIN(composetime)` of stored messages), no separate
  state that can desync.
- Media download into `data/<chat>/media/` using the existing `attachments.process`
  (original/full-res views), skip-if-already-downloaded.
- 429-aware politeness: inter-page delay + exponential backoff honoring `Retry-After`.
- `--thread ID` to archive a single conversation.

## Out of scope

- jsonl export (`sqlite3 … | jq` covers reading; an export command can come later if needed).
- Scheduled/daemon incremental sync — re-run manually or via cron.
- Deleted/edited message reconciliation (raw stream already carries edits as new versions;
  archive stores what the history API returns at fetch time).
- Any upload/remote storage of the archive.

## Acceptance criteria

- [ ] `uv run miniteams archive` creates `data/index.db` and one folder per private chat with
      `messages.db`; each stored row keeps the complete raw API JSON of the message.
- [ ] Killing the run mid-backfill and re-running continues from the oldest stored message —
      no gap, no duplicate rows (message id is primary key).
- [ ] Re-running after new activity stores the new messages (top-up) without re-walking the
      full history of an already-backfilled chat.
- [ ] New conversations appearing since the last run are discovered and archived.
- [ ] Media referenced by archived messages lands in `data/<chat>/media/` at original quality;
      already-present files are not re-downloaded; a failed download never aborts the run.
- [ ] `index.db` reflects per chat: id, label, topic, participants, backfill_done, last_fetch_at.
- [ ] A 429 from the chat service slows the run down instead of crashing it.

## Phases

### Phase 1 — Storage layer (`archive_store.py`)
- Work: `index.db` schema + upsert; per-chat dir naming (sanitized thread id, exact id kept in
  `index.db`); per-chat `messages.db` schema; `insert_messages(page)` with INSERT OR IGNORE in
  one transaction; `oldest()/newest()` cursor queries; `mark_backfill_done`.
- **Data model impact**: new sqlite files — `index.db` `chats(id PK, dir, label, topic,
  participants_json, backfill_done, last_fetch_at)`; per-chat `messages(id PK, composetime, raw)`.
- **DoD**: `uv run pytest tests/test_archive_store.py` green — covers dedup on double insert,
  cursor queries, reopen-and-resume, dir sanitization.

### Phase 2 — `archive` command (no media)
- Work: enumerate → upsert chats; per-chat loop: top-up then backfill with per-page commit;
  resume from `MIN(composetime)`; `backfill_done` on exhausted history; 429 backoff +
  inter-page delay; `--data-dir`, `--thread`, `--all` flags.
- **Data model impact**: none beyond phase 1.
- **DoD**: `uv run pytest tests/test_archive.py` green — fake paged API: full run, interrupted
  run resumes without dup/gap, top-up stops at overlap, new chat discovered on second run.
- **Fin de phase**: pause — validation user sur un run réel avant la phase 3.

### Phase 3 — Media
- Work: per-message `attachments.process` into `data/<chat>/media/` when archiving; skip when
  destination file exists; failures logged, non-fatal; `--no-media` flag.
- **Data model impact**: none.
- **DoD**: `uv run pytest tests/test_archive.py -k media` green — download called with per-chat
  dir, existing file skipped, exception in download doesn't stop the message loop.

## Data model impact (summary)

New sqlite databases only (no change to existing storage): `data/index.db` (chats table),
`data/<chat>/messages.db` (messages table). Config/cache dirs untouched.

## Open questions

- (none)

## Decisions

- Per-chat `messages.db` rather than one global DB — self-contained, copyable/deletable per
  conversation (validated in conversation).
- Raw JSON blob per message, no normalized columns beyond `id` + `composetime` — "max d'info",
  schema-drift-proof; queries needing more can json_extract.
- Folder name = thread id with `/` replaced (`:`/`@` are legal on Linux); authoritative id
  stays in `index.db`.
- Archive contains personal conversation data — stays local under `data/`; recommend adding
  `data/` to `.gitignore` (done in phase 2).
- Top-up before backfill on each chat, so a long backfill never delays capturing fresh messages.
