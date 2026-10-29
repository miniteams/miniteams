# 015 — Presence on avatars

**Status**: shipped
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
  spike, 10 min by default). Without a confirmation (a callback or a snapshot; a renewal's `20200`
  is not one) for two periods the dot disappears: unknown, never a stale status.
- **Cache.** Latest presence per `(tenant, mri)` in a SQLite file on local disk
  (`~/.cache/miniteams/presence.sqlite3`, not the NAS data dir), shown at start as unconfirmed until
  the first callback. Single writer, latest state only, no history. Schema: a subset of
  `misc/presence-spike/schema-proposal.sql` (no names stored), shared later with the read-receipts horizons (spec 011).
- **Own presence** is forced by the collector only for auto-Away (desktop idle or no page, see Decisions); the
  widget's existing `user.activity` signal is unchanged. Since 2026-10-29 `web`'s socket is published
  as one of my endpoints (see Decisions).

## Out of scope

- Presence on group rows, read-receipt avatars, the MCP or the CLI.
- Forcing the user's own status from a pick (spec 036 does); status messages; out-of-office notes.
- Presence history.

## Acceptance criteria

- [x] **AC1** The spike's long observation (~1 h) records at least one real availability/activity
      transition and the subscription lifetime, as sanitized fixtures in `misc/presence-spike/`;
      the renewal period is set from it.
- [x] **AC2** A subscription on the `web` process's own Trouter socket delivers presence callbacks
      without disturbing chat deliveries (or the dedicated-endpoint fallback is documented).
- [x] **AC3** 1:1 rows (widget and chat page list) and message author avatars show a dot coloured
      by availability, with an "Availability · Activity" hover; unknown or missing presence shows
      no dot and is never shown as Offline.
- [x] **AC4** A presence callback updates every visible dot for that person without a page reload.
- [x] **AC5** After two renewal periods without confirmation, or while Trouter is disconnected
      beyond that, the dots disappear; a reconnect re-subscribes and restores them.
- [x] **AC6** The followed list stays bounded (≤ ~50 peers + open chat authors) and follows row and
      chat changes (subscriptions added and removed).
- [x] **AC7** A `web` restart shows cached presence as unconfirmed at once, confirmed after the first
      callback; the cache lives on local disk.
- [x] **AC8** The presence collector forces no status of its own except auto-Away (2026-10-29: it
      publishes `web`'s endpoint, and forces Away after 5 min of desktop idle or 2 min without a page
      while no status is forced) and never calls Graph; the widget's existing `user.activity` signal is unchanged.

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

None left from phase 1 (answers in Decisions, evidence in `misc/presence-spike/README.md`).

## Decisions

- Dots on 1:1 rows (widget + chat page list) and on message author avatars; hover label;
  nothing when unknown (babs, 2026-10-13).
- The `web` process collects on its own Trouter socket; peers of ~50 recent 1:1 rows + open chat
  authors; snapshot then Trouter updates; local SQLite cache for restarts (babs, 2026-10-13).
- Finish the spike with a long observation before coding; renew on reconnect and periodically;
  stale after two periods → unknown; own presence never published (babs, 2026-10-13; reversed
  2026-10-29, below).
- Separate spec from the read-receipts amendment (spec 011), sharing the spike harness and the
  local cache (babs, 2026-10-13).
- **Renewal period: 10 min** (spike, 2026-10-13). 60 min observation of 50 peers: 19 availability
  transitions delivered, 0 missed against 2-min snapshots; deltas arrived unrenewed for 45 min with
  no expiry signal (none in the response, no readable status endpoint). A renewal re-POST returns
  an immediate full snapshot callback, which is the confirmation. Stale after two periods unchanged.
  Add status `40020` came back for exactly the 3 people whose snapshot was `PresenceUnknown`: treat as unknown.
- **Socket: the `web` socket, no dedicated endpoint** (spike, 2026-10-13). One endpoint registered
  like `web` carried 96 chat deliveries and 32 presence callbacks over 60 min, no close, no
  message loss after the connect flood, `user.activity` not needed. Verified on a fresh probe
  endpoint with the `web` registration, not inside the running `web` process.
- **`web` keeps sending `user.activity`** (babs, 2026-10-14): it keeps the user active in Teams;
  dropping it was a regression. The presence collector itself publishes nothing (until 2026-10-29).
- **`web` is one of my Teams endpoints** (babs, 2026-10-29), reversing "own presence never
  published": with the Teams client closed no endpoint of mine was left, so I showed Offline and a
  spec 036 status pick showed nowhere. `web` publishes its socket (`PUT /v1/me/endpoints/`,
  `activityReporting: Transport`, purple-teams' shape) on each socket, on renewal and every 4 min:
  unrenewed, the endpoint shows inactive after ~6 min and Offline after ~11 (spike, 2026-10-29); a
  re-put of the same id lifts inactive back to Available.
- **No activity reporting** (spike, 2026-10-29): `POST /v1/me/reportmyactivity/` answers 200 with no
  visible effect and does not extend the endpoint (404 once it expired); an endpoint cannot carry
  Away (400 for Transport) nor AvailableIdle. Activity from "a page is connected" is wrong anyway:
  the widget stays connected in the tray.
- **Auto-Away from desktop idle or no page** (babs, 2026-10-29): after 5 min without keyboard or
  mouse input (the Teams desktop client's delay), or 2 min without any page (widget, chat page)
  connected to `web` (a reload must not flap), with my endpoint published, no status forced and my
  availability neither Busy nor Do not disturb (meeting, call, presenting), `web` forces Away (spec
  036's call, `{"availability": "Away"}`, the only lasting Away the spike found). Back (a page
  connected and the desktop not idle; idle unknown counts as not idle), an Away still ours is lifted
  (Reset); a status changed in Teams meanwhile stays. A menu pick ends auto-Away and is never reset;
  stopping `web` lifts an auto-Away. A forced Away lasts 7 days by default, so auto-Away sends
  `desiredExpirationTime` = now + 15 min (honoured: the read-back `forcedAvailability.expiry`
  equals it; a field named `expiry` is ignored) and forces it again while still away once less than
  10 min are left: a `web` crash leaves at most ~15 min of Away. Menu picks keep Teams' default
  expiry. Desktop idle is read from X11 (screen saver extension) in a child process, cut after 2 s:
  Xlib exits the process on an X I/O error and a hung X server blocks the call. X11 idle is
  optional: the endpoint is published whenever `web` runs; without it (Wayland, no display, library
  missing) Away comes only from no page connected. Trade-off: while auto-Away is forced, activity on
  another device (phone, laptop) and a meeting or call that starts meanwhile show Away until I am
  back here. Before ending an auto-Away on a callback that no longer shows it, my presence is read
  again (`getpresence`): a late callback must not end it. An Away is ours only while its
  `forcedAvailability.expiry` matches the `desiredExpirationTime` sent (to the second): an Away
  picked in the Teams client meanwhile is never renewed nor lifted. One `web` per account: a second
  instance (another machine, a test instance on the real account) fights auto-Away, the one
  without pages forces Away and the other never lifts an Away it did not set.
