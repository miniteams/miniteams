# 030 — Widget mute is Teams' mute

**Status**: shipped (AC9 awaits a Teams-client check)
**Requested by**: babs
**Date**: 2026-10-21

## Problem

The widget's 🔕 Mute (`muted.json`, `Board.set_muted`) is local. A chat muted in Teams shows up in the
widget's Inbox like any other, and a chat muted in the widget still notifies on the phone and in the
Teams client. Two mute lists drift apart, and the user keeps both by hand.

## Solution

The widget reads and writes Teams' own mute. A chat muted in Teams is muted in the widget (out of
Inbox, Unread and Seen, listed under the existing `muted` tab). 🔕 Mute and 🔔 Unmute in the widget
change the mute in Teams, so every device sees it. The local `muted.json` goes away after a one-time
push of its entries to Teams.

## Scope

- **Source of truth.** The conversation property `alerts`: `"false"` means muted. Absent or `"true"`
  means not muted.
- **Read.** Rows from the live listing (`row_from_conversation`) and from the archive seed (the index
  keeps the raw conversation) take `muted` from `alerts`.
- **Write.** The page's `mute` / `unmute` verbs call Teams (`PUT …/conversations/<id>/properties?name=alerts`,
  next to `send.mark_read`). The row flips when Teams answers 2xx, never before.
- **Live.** A `ConversationUpdate` carrying `alerts` updates the row (mute from another device).
- **Migration.** On start, each entry of `muted.json` is pushed to Teams (the PUT is idempotent),
  then the file is renamed `muted.json.migrated`.
- **Filter.** The existing `muted` tab and the Inbox/Unread/Seen rules (`ui.js` `filters`) stay as they
  are. Their input changes, not their logic.

## Out of scope

- Channels. They have no `alerts` property, and the widget does not list them (`in_scope`).
- Teams' channel notification settings (All activity / Off / Custom).
- Mute in the MCP server or the CLI.
- A distinct icon for "muted in Teams" versus "muted here": there is only one mute now.

## Acceptance criteria

- [x] **AC1** Given a chat whose listing entry has `alerts: "false"`, when the widget fills, then its row
  has `muted: true` and it shows under the `muted` tab only.
- [x] **AC2** Given a chat with `alerts` absent or `"true"`, then its row has `muted: false`.
- [x] **AC3** Given an archive seed whose raw conversation has `alerts: "false"`, then the seeded row is
  muted before the live listing arrives.
- [x] **AC4** When the user clicks 🔕 Mute, then miniteams sends the Teams request, and the row turns
  muted only after a 2xx.
- [x] **AC5** When the Teams request fails, then the row stays as it was and the failure is logged
  (`mute_failed`, thread, muted, error). The page is not left in a half state.
- [x] **AC6** 🔔 Unmute does the same in reverse (AC4, AC5).
- [x] **AC7** Given a `ConversationUpdate` with `alerts` for a listed chat, then the row's `muted` follows
  it, including an update that carries no `consumptionhorizon`.
- [x] **AC8** Given a `muted.json` with entries, when the widget starts, then each entry is muted in
  Teams, and the file becomes `muted.json.migrated`. Any other refusal about one chat (e.g. 400, 403, 404, 410, a
  2xx carrying an `errorCode`) skips that chat; a failure that would hit every chat (401, 408, 429, 5xx, network) keeps the file, so the
  next start retries.
- [ ] **AC9** After a widget mute, the Teams client shows the chat as muted (checked by hand).
- [x] **AC10** README describes the mute as Teams' mute; `muted.json` appears only in the migration note.

## Phases

### Phase 1 — Capture the Teams request
- Work: find the exact request that mutes and unmutes a chat (method, URL, body), by probing the
  `properties?name=alerts` endpoint on one chat and reading the listing back, then restoring the
  chat's original state. Also check whether a `ConversationUpdate` with `alerts` reaches the stream
  when the mute changes. Write the findings (request shape, event or not) into this spec under
  Decisions. No product code.
- **Covers**: none
- **Data model impact**: none
- **DoD**: the chat's listing entry reads `alerts: "false"` after the mute and its original value after
  the restore; this spec records the request and the event finding.

### Phase 2 — Read Teams' mute
- Work: `muted` from `alerts` in listing rows, seeded rows and `ConversationUpdate`; `Board` stops
  reading `muted.json` for row state.
- **Covers**: AC1, AC2, AC3, AC7
- **Data model impact**: none
- **DoD**: `timeout 120 uv run pytest tests/test_web.py` green, with tests for each AC.

### Phase 3 — Write Teams' mute and migrate
- Work: `send.set_alerts` (or the name phase 1 suggests), the `mute` / `unmute` verbs call it off the
  event loop, row update on 2xx only; one-time `muted.json` migration; README.
- **Covers**: AC4, AC5, AC6, AC8, AC9, AC10
- **Data model impact**: none (`muted.json` is renamed, not reshaped)
- **DoD**: `timeout 120 uv run pytest` green; a mute and an unmute from a test widget on its own
  `--bind` change the chat in Teams (listing read back); `pre-commit run --all-files` clean.

## Data model impact (summary)

None. `~/.config/miniteams/muted.json` stops being read after its migration.

## Open questions

- [x] Two-way sync (A) or read-only (B): A (user, 2026-10-21).

## Decisions

- No optimistic flip on mute: a mute that Teams refused would otherwise show as done.
- `muted.json` is renamed, not deleted, so the migration can be undone by hand.
- The mute in the widget keeps letting a mention through, as today (`ui.js` `filters.all`). Teams'
  own mute does the same for @mentions.
- Phase 1 finding (2026-10-21): `PUT https://<chat host>/v1/users/ME/conversations/<id>/properties?name=alerts`
  with JSON `{"alerts": "false"}` mutes, `{"alerts": "true"}` unmutes. Teams answers 200 with an empty
  body; the conversation read back carries the new value at once.
- Phase 1 finding: every change sends a `ConversationUpdate` whose `properties` hold only a few keys
  (`alerts` among them, no `consumptionhorizon`) and whose `propertiesUpdated` is `["alerts"]`. An
  update without `consumptionhorizon` must still reach the mute handling.
- Migration pushes every `muted.json` entry without reading Teams first: the `alerts` PUT is idempotent, so
  an entry already muted costs one request and no special case.
- Migration outcomes per entry: 2xx done. A failure that would hit every chat alike (401, 408, 429,
  5xx, a network error) keeps the file and stops (`mute_migration_failed`); the next start retries. Any
  other failure is about that chat only (400, 403, 404, 410, a 2xx carrying an `errorCode`): logged
  `mute_migration_skipped`, counted in `skipped=`, and does not block the rename. No retries within a
  start. A 401 is not a skip: a token problem would otherwise drop every local mute.
