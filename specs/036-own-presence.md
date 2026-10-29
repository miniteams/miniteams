# 036 — Connection banner and own presence

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-28

## Problem

The widget and the chat page carry a green dot whose only job is the page's socket to `miniteams web`.
It says nothing when `web` loses Teams (Trouter reconnecting, sign-in expired), and a red dot in a
corner is easy to miss. Changing one's own Teams status (Busy, Do not disturb, Appear away…) still
takes a trip to the Teams client.

## Solution

The dot goes. A banner under the header appears only when something is wrong: the page lost
`miniteams web`, `web` lost Teams, or the Teams sign-in expired. In the dot's place, my own Teams
presence: a dot coloured like everyone else's, and a click opens the Teams status menu (current
status on top, then Available, Busy, Do not disturb, Be right back, Appear away, Appear offline,
Reset status).

## Scope

- **Pages.** The widget (`widget.html`, also inside the native Qt window) and the chat page
  (`chat.html`, both list and split modes). The `#status` dot is removed from both.
- **Banner states**, most severe first, one at a time:
  - page ↔ `web` socket closed: "Disconnected from miniteams, reconnecting…";
  - `web` ↔ Teams (Trouter) down, reconnecting: "Teams connection lost, reconnecting…";
  - sign-in expired (`web` waits for a login): "Teams sign-in expired: run `miniteams login`".
  Any of them shows once it has lasted 2 s (a reload, a quick server restart or `web`'s own Teams
  reconnect after a restart does not flash it); recovery hides the banner at once.
- **Teams link state.** `web` tracks its Trouter session (connected / reconnecting / login needed)
  and sends it to every page on connect and on change.
- **Own presence read.** My MRI joins the followed people of the presence collector (spec 015):
  same snapshot, subscription, freshness and cache. The `presence` frame carries my availability,
  my activity, and whether the status is forced (`forcedAvailability`), so the menu marks the
  chosen entry.
- **Own presence dot.** Same colours and hover label as spec 015 dots ("Busy · In a call"); unknown
  shows a neutral, empty ring (never Offline). Keyboard reachable (button, Enter/Space, Escape closes).
- **Menu.** Header line: current label (activity when it differs, e.g. "In a call"). Entries:
  Available, Busy, Do not disturb, Be right back, Appear away, Appear offline, Reset status. The
  forced entry, if any, is marked. The menu reuses the chat page's existing popup look.
- **Set presence.** `web` calls the native presence service, never Graph:
  `PUT https://presence.teams.microsoft.com/v1/me/forceavailability/` (purple-teams uses the same
  call). Body `{"availability": <A>}` with A in `Available`, `Busy`, `DoNotDisturb`, `BeRightBack`,
  `Away`; Appear offline is `{"availability": "Offline", "activity": "OffWork"}`; Reset status is
  `{"availability": "Offline"}` (spike, phase 1). No expiry is sent: the service applies Teams'
  defaults.
- **Feedback.** The dot follows the next presence callback; a refused or failed call shows an error
  next to the menu ("status not changed: <reason>"). Offline pages disable the menu entries.
- **`user.activity`** keeps being sent as today (spec 015 decision).

## Out of scope

- Durations / expiry ("reset after 1 h, today…").
- Status message, out-of-office note, work location.
- Presence in the tray icon, the MCP or the CLI.
- Any page ↔ `web` reconnection change: only how it is shown.

## Acceptance criteria

- [x] **AC1** A spike against the live account records, as sanitized fixtures in
      `misc/presence-spike/`: the answer of `forceavailability` for each of the 6 values, the reset
      call and its answer, and the presence callback for my own MRI (availability, activity,
      `forcedAvailability`) after each; the original status is restored afterwards.
- [x] **AC2** Neither page has the `#status` dot any more.
- [x] **AC3** With `web` stopped, a page shows "Disconnected from miniteams, reconnecting…" within
      3 s; restarting `web` clears it without a manual reload. A page reload while `web` runs does
      not show it.
- [x] **AC4** When `web`'s Trouter session drops, every connected page shows "Teams connection lost,
      reconnecting…" until the next session is connected; a page opened meanwhile shows it after the
      same 2 s grace.
- [x] **AC5** When `web` waits for a login (refresh token dead), pages show the sign-in banner;
      a `miniteams login` clears it once the session reconnects.
- [x] **AC6** My own dot shows my current availability with the spec 015 colours and label; it
      follows a status changed from the Teams client without a reload; unknown shows the empty ring.
- [x] **AC7** Picking each of the 6 entries makes `web` send the matching `forceavailability`
      call, and the dot shows the new status from the following callback; the menu marks the forced
      entry.
- [x] **AC8** Reset status lifts the forced status (`{"availability": "Offline"}`); the dot returns to
      the automatic one.
- [x] **AC9** A failed set (HTTP error, token unavailable) leaves the dot unchanged and shows the
      error; nothing is retried in a loop.
- [x] **AC10** The menu opens and closes by mouse and keyboard, stays inside the viewport of the
      narrow widget window, and closes on Escape or a click outside.

## Phases

### Phase 1 — Spike: set and read own presence
- Work: extend `misc/presence-spike/probe.py` with the 6 `forceavailability` values, the reset
  candidates (`DELETE …/forceavailability/`, then other shapes if refused) and a subscription to my
  own MRI; record fixtures; restore the starting status.
- **Covers**: AC1
- **Data model impact**: none
- **DoD**: fixtures in `misc/presence-spike/` (local and untracked, as spec 015's), no real names
  or ids; the reset call and the own-callback shape written into this spec's Decisions.

### Phase 2 — Connection banner
- Work: Teams link state in `web` (hook in the session loop: connected, reconnecting, login
  needed) broadcast as a frame; banner in both pages; `#status` removed.
- **Covers**: AC2, AC3, AC4, AC5
- **Data model impact**: none
- **DoD**: tests for the state frame on connect and on change; live check on a test `--bind`:
  stop/start `web` (AC3), forced Trouter drop (AC4).

### Phase 3 — Own presence: dot, menu, set
- Work: own MRI followed and framed with `forced`; `setpresence` page request handled by `web`
  (validated value, native call, error answer); dot and menu in both pages.
- **Covers**: AC6, AC7, AC8, AC9, AC10
- **Data model impact**: none (the presence cache already stores any followed MRI)
- **DoD**: unit tests for the request validation, the call shapes and the error path; live check on
  a test `--bind`: each entry and reset, a change made in Teams, the narrow widget layout.

## Data model impact (summary)

None. My own state lands in the existing presence cache like any followed person.

## Open questions

None.

## Decisions

- Banner covers page ↔ `web` and `web` ↔ Teams, sign-in expiry included (babs, 2026-10-28).
- Menu: the 6 Teams entries plus Reset status (babs, 2026-10-28).
- No duration in the UI (babs, 2026-10-28). The service sets one anyway when none is sent
  (spike): Busy and Do not disturb lift after 1 day, Available, Be right back and Away after 7
  days, Appear offline never. These are Teams' own defaults, kept as is.
- **Protocol** (spike, 2026-10-28, `misc/presence-spike/own*.json`): every set answers 200 with an
  empty body, and the own-MRI callback follows in ~0.3 s. `forcedAvailability {availability,
  activity, expiry, publishTime}` is present in the presence entry only while a status is forced.
  `DELETE …/forceavailability/` answers 401 (40102); `{"availability": ""}` and `null` answer 400;
  `{"availability": "Offline"}` alone lifts the force, so it is the reset. Subscribing my own MRI
  works like any peer (`20200`, snapshot callback, then one per change).
- Live spike and tests may flip the real status briefly, restored after each run (babs, 2026-10-28).
- Both pages get the banner and the selector, the dot's place in each header — because the dot
  lives in both today.
- 2 s grace before any banner — so reloads, quick restarts and `web`'s first Teams connect do not
  flash it.
- No MCP/CLI command — not asked; a later spec if wanted.
- Auto-Away (spec 015, 2026-10-29) never overrides a pick: it acts only while no status is forced,
  and a pick ends it.
