# 038 — Names that outlive their owners

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-30

## Problem

A person who left the organisation shows as a bare guid: in chat titles, in "… left" lines, as a
message sender. Their account is gone, so the profile lookup answers with no name, and miniteams
caches that empty answer as the guid. Big meeting chats end up with screens of guids.

Two things make it worse:

- `names-v2.json` started empty when it replaced `names.json` (#85). Names learned before someone
  left were dropped with the old file.
- Each process (web, archiver, MCP) loads the whole JSON file at start and rewrites it whole. The last
  writer wins and silently drops what the others learned since.

The archive still knows most of these names: call records list every participant's display name,
member events often carry a `friendlyname`, stored rosters keep the names they had, and old messages
carry the name their sender's client stamped.

## Solution

miniteams keeps every name it has ever learned for a person, in one SQLite file shared by all its
processes. A person who left keeps the last name miniteams saw, live or in the archive. A person
nobody ever named shows as "Unknown user", never as a guid. In the chat page, hovering "Unknown user"
shows the id, so two unknown people stay distinguishable.

## Scope

- One names store, SQLite, replacing `names-v2.json`: one row per MRI, its name, the source of that
  name, and when it was seen.
- Sources, from most to least trusted:
  1. **live**: profile lookup, a fetched roster's `friendlyName`;
  2. **archived**: `Event/Call` participant `displayName`, a member event's `friendlyname`, a stored
     roster in `index.db`;
  3. **declared**: the `imdisplayname` a message carries (free text chosen by the sender's client).
- A name never gets replaced by a lower-ranked source. Within a rank, the most recently seen name
  wins. A failed or empty lookup never erases a name.
- First start imports `names-v2.json`, the old `names.json` as **declared** (it mixes in declared
  names), and the rosters in `index.db`.
- The archiver records the names of the call records, member events and message senders it stores,
  as it stores them.
- A one-off sweep, `miniteams names --sweep`, reads the existing archive for the same three
  sources.
- Labels: the best-ranked name, else "Unknown user". Covers chat titles built from rosters, member
  lines, senders, reactions, receipts, mentions. The chat page puts the MRI in the label's tooltip.
- MCP records keep the MRI next to the label (`from`, `participants[].mri`, as today).

## Out of scope

- Marking a departed person ("Jean Dupont (left)"): knowing who left needs a lookup, and nothing asks
  for it yet.
- Names for apps and bots (`28:` MRIs): they keep today's handling.
- A UI to edit or merge names.
- Expiring names: the store is a few hundred rows; it only grows with new people.

## Acceptance criteria

- [x] **AC1** Given an MRI whose profile lookup returns no name, when the archive holds a call record
      naming it, then every label for it (title, member line, sender) shows that name.
- [x] **AC2** A **declared** name never replaces an **archived** or **live** one; an **archived**
      name never replaces a **live** one; an empty or failed lookup never replaces any name.
- [x] **AC3** Two processes learning different names at the same time both keep theirs: neither
      write drops the other.
- [x] **AC4** First start with an existing `names-v2.json`, `names.json` and `index.db` imports all
      three; a second start imports nothing more and changes no row.
- [x] **AC5** `miniteams names --sweep` on an archive names every MRI that a call record, a member
      event or a sent message names; it reads the archive only, never writes it, and can run while
      the archiver runs.
- [x] **AC6** Given an MRI no source names, every label for it reads "Unknown user", never its guid;
      in the chat page, hovering it shows the MRI.
- [x] **AC7** A message from a sender with a **live** or **archived** name is attributed to that name
      even if its `imdisplayname` differs (today's #85 rule kept).
- [x] **AC8** The live archiver stores the names of the call records, member events and senders it
      archives, without a second pass over the message.

## Phases

### Phase 1 — Names store
- Work: `names.db` with ranked upsert; `Directory` reads and writes through it; import of
  `names-v2.json`, `names.json`, `index.db` rosters on first start; `names-v2.json` no longer
  written.
- **Covers**: AC2, AC3, AC4, AC7
- **Data model impact**: new `names.db` (table `names`: `mri` primary key, `name`, `rank`, `source`,
  `seen_at`, `looked_up_at`; table `meta` for the import marker).
- **DoD**: tests for the ranking table, two-writer concurrency on one file, idempotent import; the
  full suite green.

### Phase 2 — Archive sources
- Work: parsers for `Event/Call` parts and member-event `friendlyname`; the archiver records them and
  sender `imdisplayname` on store; `miniteams names --sweep`.
- **Covers**: AC1, AC5, AC8
- **Data model impact**: none (rows in `names.db`).
- **DoD**: parser tests on synthetic call and member-event payloads; a sweep on a copy of a fixture
  archive names the expected MRIs and leaves its files byte-identical; a live run of the sweep on the
  real archive names most of today's unresolved MRIs (count reported in the PR, not in the repo).

### Phase 3 — "Unknown user"
- Work: the fallback label everywhere a guid shows today; the MRI as tooltip in the chat page.
- **Covers**: AC6
- **Data model impact**: none.
- **DoD**: no guid-shaped label in a test page holding an unnamed member; a test server on its own
  `--bind` shows "Unknown user" with the MRI on hover in a big meeting chat.

## Data model impact (summary)

- New `names.db` (`names`, `meta`), in the config dir: it holds names no live source can give back,
  so it is state, not cache.
- `names-v2.json` is read once for the import, then left on disk unread, like `names.json`.

## Open questions

- [ ] None.

## Decisions

- SQLite over JSON: per-row upserts end the whole-file overwrites between processes; WAL mode lets
  the web and the archiver read while one writes.
- The store lives in the config dir, not the cache dir: a departed person's name cannot be fetched
  again, so clearing the cache must not lose it.
- Call records rank with the archived sources, above declared: the call service writes them from
  the directory, the sender's client does not choose them.
- The old `names.json` imports as **declared**: it mixes directory names with names messages
  declared, and nothing tells them apart.
- "Unknown user" in English, like the rest of the UI.
