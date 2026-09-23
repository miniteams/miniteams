# 005 — Archive follows live events

**Status**: shipped
**Requested by**: babs
**Date**: 2026-09-23

## Problem

The archive only learns about new activity by polling. `archive --loop 300` enumerates every chat,
then tops up the ones whose last activity moved. A message therefore reaches `data/` up to five
minutes late, plus the length of one pass. Edits, reactions and deletions never reach it: `INSERT OR
IGNORE` keeps the first version it saw. The read horizon in `index.db` is only as fresh as the
last enumeration. The widget (spec 004) seeds its rows from that index, so it inherits the lag.

## Solution

`miniteams archive --live` subscribes to the Trouter stream first, then catches the archive up, then
follows the stream:

1. Connect and register. From here on, events are **buffered**, not written.
2. Run a normal archive pass (enumerate, top-up, backfill, media) in a worker thread.
3. When the pass ends, **drain** the buffer: dedupe it, apply it, then write each new event as it
   arrives.
4. On any gap signal (reconnect, `trouter.message_loss`) go back to step 1's buffering and run a
   new catch-up pass. The fast-skip on unchanged chats keeps that pass cheap: it only touches chats
   whose last activity moved.

Media referenced by a live message downloads right away. The process runs until the refresh token
dies, then stops cleanly with exit code 1, like `--loop`.

## Why buffer instead of writing live during the catch-up

Top-up stops at the first page that overlaps `newest()`. If a live message lands in a chat before
the pass reaches it, `newest()` jumps past the gap and top-up stops early, so the gap is never
fetched. Buffering keeps `newest()` below every unfetched message. It also means a single writer
touches the archive at any time: the pass, or the live applier, never both.

## Scope

- `archive --live`. It excludes `--loop`, `--thread`, `--assets-only` and `--retry-assets`.
  `--all`, `--no-media`, `--videos`, `--no-avatars` and `--data-dir` keep their meaning.
- Own Trouter endpoint (`epid_name="endpoint_id-archive"`), so it runs next to `web` and `stream`
  without either stealing the other's registration.
- **One token source for the whole process.** `token_source(settings)` is already one locked
  instance per process, shared by the stream, its directory refresher and the archive's
  `RefreshingToken`. `RefreshingToken` is called from the pass thread and the event loop, so its
  re-mint goes under a lock. A dead refresh token stops
  the stream, the pass and the process.
- **Events persisted** (same scope filter as the pass: private and meeting chats, channels with
  `--all`):

  | Event | Write |
  |---|---|
  | `NewMessage` (not `Control/*`) | insert into `<chat>/messages.db`; bump `index.db` `raw.lastMessage` if newer |
  | `MessageUpdate` (edit, reaction, delete) | replace `messages.raw` when the event's `version` is higher; the replaced raw goes to `message_versions`, and so does an older version arriving late |
  | `ConversationUpdate` | merge into `index.db` `raw`, one level deep for dicts; `lastMessage` replaced only by a newer one |

- Unknown chat on a live event: create its `index.db` row and folder; the next pass fills label and
  roster.
- **Buffer dedupe**: messages collapse only on exact redelivery (same id and version), so every
  distinct version reaches `message_versions`. One merged entry per thread for
  `ConversationUpdate`. Applied in arrival order.
- **Buffer bound**: 10 000 entries (an Adaptive Card weighs several KB). On overflow, drop the events and keep only their thread ids.
  Memory stays bounded however long the pass runs.
- **Overflowed chats are topped up before the drain**: an overflow schedules one more pass that
  forces them, still buffering. Draining first would write a chat's pre-overflow entries, lift
  `newest()` over the events dropped after, and the top-up would stop short of them.
- **Gap during a pass**: a reconnect or `message_loss` while a pass runs schedules one more pass
  after it, even with nothing buffered. Several signals collapse into one.
- **Connect flood**: every (re)connect opens with a burst of `trouter.message_loss` repeating one
  `messaging` etag (the registration instant). The connect gap covers it. A gap is a `messaging`
  etag not seen before, except the first one of a session within 30 s of registration.
- **Loss the stream wrote past**: `message_loss` arrives after the lost events, and events written
  in between push `newest()` over the hole. The catch-up that follows forces top-up (no fast-skip)
  on chats written live in the 5 minutes before the signal. That set is bounded by pruning on age.
- Live media: `attachments.process` on each stored message, in a background task bounded by the
  pass's concurrency cap, through the same denied-asset cache and failure recording as the pass.
  A failure logs and never holds the stream; a dead refresh token stops the process.
- Structured logs: `live_gap` (reason), `live_catchup_start` (forced, buffered),
  `live_catchup_failed`, `live_drained` (per outcome, dropped), `live_event_stored`.
- A failed pass (network, 5xx) retries with backoff capped at 5 min, keeping its forced chats.
- A failed write (`database is locked` from another writer, disk) never drops the event. In the
  drain, the entry stays at the head of the buffer and is retried after another pass, with a
  backoff a good pass does not reset. While following, the entry is buffered and a catch-up starts
  (`live_gap reason=write_failed`).

## Out of scope

- Removing `--loop`. It stays as is; `--live` replaces it for whoever wants it.
- Avatars on live events (the next pass fetches them).
- `media/recordings.json` on live events: the next pass that touches the chat rewrites it.
- Presence, calls, typing: not archive data.
- Retrying 404-backed-off transcripts without a pass. A chat with no new activity is fast-skipped,
  same as under `--loop` today.
- `run-archive-loop.sh` defaults. It forwards args, so `run-archive-loop.sh --live` works.
- Writes from `web`. The archive stays the only writer of `data/`.

## Acceptance criteria

1. A message received while `--live` is following lands in `messages.db` within seconds, and its
   media in `media/`.
2. Events that arrive during the catch-up pass are written only after it ends, and a chat touched
   both live and by the pass shows no gap and no duplicate.
3. Several versions of one message in the buffer, in any order, produce one row holding the highest
   version, and every lower one sits in `message_versions`.
4. An edit, a reaction and a delete each update `messages.raw`; the delete keeps the original
   content in `message_versions`.
5. An update with a version at or below the stored one changes nothing.
6. A `ConversationUpdate` moves `consumptionhorizon` in `index.db` without dropping other `raw` keys.
7. A reconnect, or a `message_loss`, triggers a catch-up; a message sent during the disconnect ends
   up archived.
8. A buffer past its bound keeps memory flat. The overflowed chats are topped up after the pass and
   before the drain; a chat with entries buffered before the overflow and events dropped after it
   ends up with no gap.
9. The process runs past two skype-token lifetimes without a 401. A dead refresh token stops it with
   `archive_stopped reason=auth_expired` and exit code 1.
10. `--live` combined with `--loop`, `--thread`, `--assets-only` or `--retry-assets` is refused by
    the parser.

## Phases

### Phase 1 — Store writes for live events
- Work: `ChatStore.apply_message` (version-guarded replace, old raw to `message_versions`),
  `Index.merge_raw` (merge, `lastMessage` only when newer), `Index.ensure_chat`. Check a real
  `stream --raw` capture against a history message; the answer goes in Decisions.
- **Data model impact**: new table `message_versions(id TEXT, version TEXT, raw TEXT, replaced_at
  TEXT, PRIMARY KEY(id, version))` in each `messages.db`, created on open like `denied_assets`.
- **DoD**: `uv run pytest tests/test_archive_store.py` green; covers criteria 3 (store side), 4, 5, 6.

### Phase 2 — Live follower
- Work: `archive_live.py`: buffer with dedupe and bound, state machine (buffering, catching up,
  following), catch-up pass in a daemon thread, drain, gap handling, recent-writes window,
  forced top-up (`run_archive(force=)`). `run_forever` gets an `on_gap` hook fired after
  (re)registration and on a real `message_loss`. Lock in `RefreshingToken`. `--live` flag and its parser
  exclusions.
- **Data model impact**: none.
- **DoD**: `uv run pytest tests/test_archive_live.py` green, fake stream and fake pass; covers
  criteria 2, 3, 7, 8, 10, plus a token-refresh test for 9.

### Phase 3 — Live media and real run
- Work: media download per live message, error isolation, `docs/agent-archive.md` (`message_versions`,
  live freshness), README usage.
- **Data model impact**: none.
- **DoD**: `uv run pytest -k live_media` green (download called with the chat's `media_dir`, a
  failure keeps the stream acking). **Pause for a real run by the user**: criteria 1 and 9, over at
  least two hours.

## Data model impact (summary)

- `<chat>/messages.db`: new `message_versions` table. Existing rows untouched.
- `index.db`: no schema change. `raw` now also changes between passes (merged live).

## Open questions

- (none)

## Decisions

- `messages.raw` holds the latest version and replaced versions go to `message_versions`, deletes
  included: the original content of a deleted message is kept. Decided 2026-09-23.
- Buffer bound 10 000 and loss window 5 min. Constants, not flags. The bound counts entries, not
  bytes: a lower bound costs one extra forced pass, never data.
- Live resources match history ones (capture 2026-09-23): a `NewMessage` carries `id`,
  `composetime`, `version`, `content`, `properties`, `imdisplayname`, plus live-only keys (`isactive`,
  `threadtopic`, `to`). A `MessageUpdate` is a whole message, so it replaces `raw`; merging would
  keep a removed reaction. A resource without `content` would be partial and is merged instead.
  A `ConversationUpdate` is the whole conversation object.
- Id-less `NewMessage`s (`ThreadActivity/MemberConsumptionHorizonUpdate`) are ignored: they are
  read ticks, not messages. `ThreadUpdate` (roster, topic) is left to the next pass.
- The pass runs in a daemon thread, not `asyncio.to_thread`: the default executor would make
  Ctrl-C wait for a whole pass to end.
- No explicit `busy_timeout`: `sqlite3.connect` already waits 5 s on a lock, and with the buffer
  only one writer of this process touches the archive at a time.
- One process, stream in the event loop, pass in a worker thread. The pass makes blocking HTTP calls
  and `time.sleep`; on the loop it would stall the acks and Trouter would drop the socket.
- Spec number 005: 004 is taken by the seed work on `feat/seed-rows-from-archive`.
- Conflict risk with 004 is low: it reads `index.db`, this one writes new keys into `raw` and adds
  store methods. Merge 004 first if both are ready.
