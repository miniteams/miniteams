# 015 — Presence on avatars

**Status**: approved
**Requested by**: babs
**Date**: 2026-10-13

## Problem

The widget and the chat page show who wrote, never whether that person is available, busy, away or
offline. Checking takes a trip to the Teams client.

## Solution

A coloured dot on the avatar of 1:1 rows and of message authors shows the person's current Teams
presence, kept live through the native presence service and Trouter, without Microsoft Graph. A
hover gives the label ("Busy · In a meeting"). Unknown presence shows no dot.

## Scope

- **Where.** 1:1 rows in the widget list and in the chat page list; message author avatars in the
  chat page. Not group rows (no single avatar), not read-receipt avatars (spec 011 amendment).
- **Collector.** The `web` process itself. It subscribes on its existing Trouter socket
  (`<surl>/TeamsUnifiedPresenceService`, as purple-teams does on its single socket); a dedicated
  endpoint is the fallback if the spike shows that cannot work.
- **People followed.** The peers of the ~50 most recent 1:1 rows plus the authors of the open chat
  page; a bounded list, updated when it changes.
- **Protocol** (verified by `misc/presence-spike/`): snapshot `POST
  https://presence.teams.microsoft.com/v1/presence/getpresence/` and subscription `POST
  …/v1/pubsub/subscriptions/<endpoint>` with the presence resource token; deliveries over Trouter.
- **Freshness.** Re-subscribe on every Trouter reconnect and every renewal period (measured by the
  spike, 10 min by default). Without a confirmation (callback or successful renewal) for two periods
  the dot disappears: unknown, never a stale status.
- **Cache.** Latest presence per `(tenant, mri)` in a SQLite file on local disk
  (`~/.cache/miniteams/presence.sqlite3`, not the NAS data dir), shown at start as unconfirmed until
  the first callback. Single writer, latest state only, no history. Schema from
  `misc/presence-spike/schema-proposal.sql`, shared later with the read-receipts horizons (spec 011).
- **Own presence** is never published or forced (no `user.activity`).

## Out of scope

- Presence on group rows, read-receipt avatars, the MCP or the CLI.
- Setting or overriding the user's own presence; status messages; out-of-office notes.
- Presence history.

## Acceptance criteria

- [ ] **AC1** The spike's long observation (~1 h) records at least one real availability/activity
      transition and the subscription lifetime, as sanitized fixtures in `misc/presence-spike/`;
      the renewal period is set from it.
- [ ] **AC2** A subscription on the `web` process's own Trouter socket delivers presence callbacks
      without disturbing chat deliveries (or the dedicated-endpoint fallback is documented).
- [ ] **AC3** 1:1 rows (widget and chat page list) and message author avatars show a dot coloured
      by availability, with an "Availability · Activity" hover; unknown or missing presence shows
      no dot and is never shown as Offline.
- [ ] **AC4** A presence callback updates every visible dot for that person without a page reload.
- [ ] **AC5** After two renewal periods without confirmation, or while Trouter is disconnected
      beyond that, the dots disappear; a reconnect re-subscribes and restores them.
- [ ] **AC6** The followed list stays bounded (≤ ~50 peers + open chat authors) and follows row and
      chat changes (subscriptions added and removed).
- [ ] **AC7** A `web` restart shows cached presence as unconfirmed at once, confirmed after the first
      callback; the cache lives on local disk.
- [ ] **AC8** No request publishes or forces the user's own presence; no Graph call.

## Phases

### Phase 1 — finish the spike
- Work: long observation, own-socket subscription test, renewal period.
- **Covers**: AC1, AC2
- **Data model impact**: none
- **DoD**: fixtures and README updated in `misc/presence-spike/`; renewal period and socket choice
  recorded in this spec's Decisions.

### Phase 2 — collector, cache and dots
- Work: subscription manager in `web`, SQLite cache, board/page frames carrying presence, dots in
  both lists and on author avatars.
- **Covers**: AC3–AC8
- **Data model impact**: new local cache file `~/.cache/miniteams/presence.sqlite3`
- **DoD**: unit tests (snapshot, callback, staleness, bounded list, restart), browser tests for the
  dots and hover; `pre-commit run --all-files` rc=0; a live check against native Teams.

## Data model impact (summary)

A regenerable local SQLite cache; no change to the archive.

## Open questions

- Subscription lifetime and renewal period (phase 1).
- Whether the existing Trouter socket accepts the presence subscription (phase 1).

## Decisions

- Dots on 1:1 rows (widget + chat page list) and on message author avatars; hover label;
  nothing when unknown (babs, 2026-10-13).
- The `web` process collects on its own Trouter socket; peers of ~50 recent 1:1 rows + open chat
  authors; snapshot then Trouter updates; local SQLite cache for restarts (babs, 2026-10-13).
- Finish the spike with a long observation before coding; renew on reconnect and periodically;
  stale after two periods → unknown; own presence never published (babs, 2026-10-13).
- Separate spec from the read-receipts amendment (spec 011), sharing the spike harness and the
  local cache (babs, 2026-10-13).
