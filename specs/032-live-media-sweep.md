# 032 — Media sweep inside `archive --live`

**Status**: approved
**Requested by**: babs
**Date**: 2026-10-23

## Problem

`archive --live --verify-media` is accepted, but the flag never reaches the live passes: it is
ignored without a word. Running `--verify-media` as a second process beside `--live` means two
writers on the NFS sqlite files, which lock each other. Handing the flag to the live catch-up pass
would hold live writes for the whole sweep (~18 h): events are buffered until a pass ends.

## Solution

`archive --live --verify-media` runs one media sweep in the background of the live process: chat by
chat, from the messages already archived (as `--assets-only`), downloading only what is missing on
disk, while live events keep being written. On demand only: a flag at launch.

## Scope

- **Trigger.** `--verify-media` with `--live` starts one sweep after the first catch-up pass. Without
  the flag, nothing changes.
- **Never blocks live.** The sweep runs beside the event loop and the catch-up/reconcile passes, one
  chat at a time; live events are applied as they come, not buffered for it. It yields to a running
  catch-up pass (no two writers on one chat at the same time).
- **Per chat.** Same work as `--assets-only` on that chat: re-scan stored messages, download missing
  assets (skip-exists), honouring `--media`, `--videos` and the denied-assets backoff; `--no-media`
  leaves nothing to sweep and is refused.
  No history fetch, no metadata, no avatars.
- **Paced.** A pause between chats (setting, default a few seconds) so Teams, SharePoint and the NAS
  are not hammered; downloads share the live media concurrency limit.
- **Resumable.** Progress (last chat done, never past a failed one) is kept in the archive index; a restart with
  `--verify-media` resumes the sweep, a restart without it drops it. A finished sweep is logged with
  its totals and not run again until asked.
- **Visible.** Log events at start, per chat (as `chat_assets`) and at the end
  (`media_sweep_done`: chats, files, failures, duration). The archiver heartbeat says a sweep runs.
- **Flags that cannot apply are refused.** Any archive flag `--live` does not honour fails at parse
  time instead of being ignored.

## Out of scope

- A periodic sweep.
- `--retry-assets` in live mode (still one-shot only).
- Changing the one-shot `--verify-media` (without `--live`).

## Acceptance criteria

- [x] **AC1** `archive --live --verify-media` downloads missing assets of every archived chat once,
  chat by chat, and logs `media_sweep_done`.
- [ ] **AC2** During the sweep, a live message is written to the archive as fast as without it (no
  buffering behind the sweep).
- [ ] **AC3** The sweep never works on a chat a catch-up/reconcile pass is writing; no sqlite lock
  error appears in a run with both.
- [x] **AC4** `--media`, `--videos` and the denied-assets backoff apply to the sweep; `--no-media` is
  refused with `--live --verify-media`.
- [x] **AC5** Stopped mid-way and restarted with `--verify-media`, the sweep resumes after the last
  chat done; restarted without it, no sweep runs.
- [x] **AC6** `--live` with a flag it does not honour fails at parse time with a clear message.

## Phases

1. **Sweep task** in `archive_live` reusing the `--assets-only` per-chat path, pacing, coordination
   with passes: AC1–AC4.
2. **Resume, logs, heartbeat, flag validation**: AC5, AC6.
