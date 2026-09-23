# 004 — Seed the widget's rows from the archive

**Status**: implemented (2026-09-23)
**Requested by**: babs
**Date**: 2026-09-23

## Problem

Starting the widget costs a bootstrap per row: one thread fetch for the label, one 20-message
history page for the mention scan and stub resolution, at 8-way fan-out. Measured on a real start
(`web.log`, 2026-09-18): **30.9 s for 50 rows** between the token and `web_bootstrap`. At the new
default of 250 that is roughly two minutes of blank page, and it grows linearly with `--limit`.

The reachability side of it is much smaller than it looks. Ranked by last activity: rank 250 was
active 33 days ago, rank 400 ninety-one days, rank 1 000 nearly a year, rank 2 457 (the last)
987 days. Only 206 of the 2 457 archived chats are 1:1, 153 of them sit beyond rank 250, and just
**33** of those have moved in the last 90 days. The 1:1 with Jean Martin ranks 112 and the one with
Jean Dupont 113 — both already inside the 250 the widget now loads.

So the pain is the *cost per row*, not the number of rows. Make rows cheap and the limit stops
hurting.

`data/index.db` already holds what the bootstrap pays for: one row per chat with the full
conversation object, `lastMessage` and `properties` included. Rebuilding every row from it offline
takes **150 ms cold, ~75 ms warm, zero API calls**; 2 401 of 2 457 get a usable label and
`consumptionhorizon` survives in 2 082, so the unread state comes back too.

*(Names in this spec are placeholders; the measurements are real.)*

## Solution

At startup the server reads the archive index and uses it for what the per-row fetches were buying —
label, snippet, read state. The live listing still defines *which* rows exist and wins on every
field it carries. Nothing else changes: same table size (`--limit`), same broadcast, same client.

## Scope

- `web --data-dir` (default `data`), like `archive`.
- **Seed**: the index is read in a thread, keyed by thread id. For a listed conversation, a missing
  label comes from `participants` minus my MRI (apps `28:` excluded unless they are all there is),
  `topic · Np` when a topic exists; `text` from `lastMessage` through the existing `snippet()`;
  `read_id`/`read_at` from `properties.consumptionhorizon`.
- **The live listing wins** on every field it provides. The index only fills gaps.
- **The per-row fan-out is skipped for seeded rows**: no thread fetch when the index gave a label,
  no history page when it gave a non-stub snippet.
- **The mention scan stays** on the newest 50 rows, seeded or not — dropping it would silently undo
  spec 003 for anything received while the widget was down. ~50 history fetches instead of 250.
- **Refresh**: when a live label lookup fails (429, or a chat we were removed from), the archive is
  asked instead; the index is re-read only if its inode/mtime/size moved and the last read is older
  than 30 s, so an unnamed message storm cannot turn into a read storm.
- **Progressive start**: the page is served as soon as the seed is ready and says so with a small
  floating spinner, bottom right, while the live listing and the mention scan are still in flight.
  The spinner clears on the broadcast that follows them. No per-row marker — whether a row came from
  the index or the API is invisible.
- `--limit` keeps its single meaning: how many rows the table holds, and its default moves from 250
  to **400** (91 days of history) **in the same change as the seed**, never before: at today's cost
  per row, 400 would mean ~3 minutes of blank page.

## Fallback — no index, or an unusable one

The archive is an **enrichment, never a dependency**. One code path, one row shape; only the number
of API calls differs.

| Situation | Behaviour |
|---|---|
| `--data-dir` missing, or no `index.db` | No seed: today's bootstrap, every row fetched. Logged once as `archive_index_absent`, never surfaced on the page. |
| `index.db` unreadable (permissions, corrupt, wrong schema) | Same, logged as `archive_index_unusable` with the error. |
| Index partial (first archive pass still running) | Seeds what it holds; the rest is fetched as today. |
| Index written concurrently by `archive` | Read-only, WAL: no lock, no wait. |
| Archive lagging behind the listing | Rows the listing does not return are pruned, so the table never holds both sets; a chat the stream brought in during the walk is kept. |

## Out of scope

- **The full-table design** (hold all 2 457 chats in memory, window the transfer with
  `page`/`more`, broadcast single rows, infinite scroll). Deliberately dropped: it is justified only
  by chats dead for one to three years, and it replaces a dumb-but-working broadcast with ordering,
  rank moves, dedup and reconnect resync.
- **Reaching chats beyond `--limit` by name.** If it ever matters, the smallest form is a `find`
  verb querying the index on disk per keystroke (74 ms for a light scan) — no in-memory table, no
  protocol change. Not built until the need shows up.
- Searching message **content**, and any write to `index.db` — the archive stays its only writer.

## Acceptance criteria

1. With a populated index, the page serves in a few seconds at `--limit 400`, against ~3 minutes
   today, and the log reports how many rows were seeded.
2. No **roster-built** label includes me (a topic that names me is the chat's real name and stays).
3. A seeded row's label, snippet and unread state match what the API bootstrap produces for the same
   chat (compared on a sample in a test).
4. A field present in the live listing is never overwritten by the index.
5. A mention received while the widget was down still shows on the newest 50 rows.
6. With no `--data-dir`, no `index.db`, or an unreadable one, the widget behaves exactly as today
   and logs the reason once; the page shows rows and no error.
7. `web` starts while a concurrent `archive` run writes the index.
8. The spinner is visible from the first paint until the listing and the mention scan have landed,
   and never sticks: it clears on the broadcast, and on failure of either background step.
9. The merged table never exceeds `--limit`, and a chat a live message created during the fill is
   not dropped by the prune.

## Phases

1. **Seed** — `--data-dir`, the offline row builder, the merge rule, the skip of the per-row fetches.
   Done when a test seeds a table from a temporary index and criteria 2-4 hold.
2. **Progressive start** — serve on seed, listing and mention scan in the background, spinner and
   its clearing, `--limit` default to 400. Done when criteria 1 and 8 hold.
3. **Edges** — archived name as the fallback for a failed lookup with its guarded re-read, the
   prune, the fallback table above. Done when criteria 5-7 and 9 hold, each with a test.

## Open questions

- ~~`--limit` at 250 or 400?~~ **400, decided 2026-09-23**, shipped with the seed and not before.
- ~~Does a seeded row say it is seeded?~~ **No, decided 2026-09-23**: a floating spinner during init
  replaces any per-row marker.
- ~~First paint ordered by slightly stale dates: reshuffle, or hold the sort?~~ **Reshuffle
  accepted, decided 2026-09-23** — rows appear at once and settle when the listing lands.

None left: the spec is ready to implement.
