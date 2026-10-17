# 022 — The chat's members on hover

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-16

## Problem

The chat page's header says "12 members" but not who: finding out means opening the chat in Teams.

## Solution

Hovering or focusing "N members" opens a small list of the chat's members, with their avatar, name
and presence when known.

## Scope

- **Data.** The server sends the page the chat's roster (names and MRIs, people only: bots and apps
  left out, as the roster already does for mentions, plus their `apps` count) when the page follows a
  chat whose name carries a member count. The follow answers from the cached roster (`_ROSTER_TTL`;
  a stale copy is refetched behind it); a roster not cached yet is fetched in the background and
  arrives in its own `members` frame.
- **Popover.** Under the header on hover and on keyboard focus of "N members" (made focusable);
  avatar initials or picture as elsewhere on the page, name, presence dot when the page has it;
  sorted by name, "You" first; at most 50 rows, then "… and N more"; "+ N apps" for the bots and
  apps the count includes; Escape or leaving closes it; a click toggles it.
- **Count.** "N members" keeps coming from the label; the list may be missing (roster unavailable),
  then the popover says "members unavailable". With no count, "N members" is hidden.

## Out of scope

- The widget.
- Acting on a member from the list (open a 1:1, mention).
- Live roster updates while the popover is open (the next follow brings the new roster).

## Acceptance criteria

- [x] **AC1** Following a chat answers its roster's people (name, MRI), bots and apps excluded (counted
  as `apps`), from the cache; a roster not cached arrives in its own `members` frame.
- [x] **AC2** Hovering "N members" shows the list sorted by name with "You" first, avatar and
  presence when known; leaving or Escape hides it.
- [x] **AC3** "N members" is reachable by Tab and its focus shows the same list.
- [x] **AC4** More than 50 members shows 50 rows then "… and N more"; the bots and apps the label
  counts show as "+ N apps", so the lines add up to it.
- [x] **AC5** With no roster, the popover says "members unavailable"; names are text, never markup.

## Phases

### Phase 1 — Members popover
- Work: roster in the follow answer; popover in chat.html.
- **Covers**: AC1, AC2, AC3, AC4, AC5
- **Data model impact**: none
- **DoD**: unit test for the follow answer; browser tests per AC in Chrome and Firefox; full suite and
  pre-commit green.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- A popover, not a native tooltip (user's call, 2026-10-16) — a big meeting's names do not fit a
  `title`.
- A roster to fetch comes in its own `members` frame, not in the follow answer: the follow must never
  block the socket loop.
