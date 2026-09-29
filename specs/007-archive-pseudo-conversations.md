# 007 — Archive the `48:` conversations, drafts included

**Status**: in progress
**Requested by**: babs
**Date**: 2026-09-30
**Order**: built before 008

## Problem

`archive` keeps private chats and meeting chats. Teams also holds nine conversations of your own
whose id starts with `48:`. None is archived, so your Notes, your scheduled messages and the text
of a draft you cancelled exist nowhere but at Microsoft.

## Solution

Every pass of `archive`, and `archive --live`, also stores the nine `48:` conversations. No flag,
no selection.

| Id | What it holds |
|---|---|
| `48:notes` | Notes to self, with media |
| `48:drafts` | scheduled and parked drafts |
| `48:annotations` | reactions and similar marks |
| `48:calllogs` | call log, with recording and transcript entries |
| `48:mentions` | messages that mention you |
| `48:notifications` | activity feed |
| `48:saved` | saved messages |
| `48:starred` | bookmarks |
| `48:threads` | followed threads |

## Scope

- One scope rule, shared by `archive._enumerate` and `archive_live.in_scope`: private chats,
  meeting chats and every id that starts with `48:`. `--all` still adds channels.
- The eight conversations other than drafts go through the existing path: history call, top-up,
  backfill, media, `index.db` row, `messages.db`.
- `48:drafts` has its own fetch. The history call refuses it (400 `Invalid threadId`), so the pass
  reads `GET /v1/users/ME/drafts` and follows `_metadata.syncState` until a page is empty.
- Drafts are stored like messages, in `data/48:drafts/messages.db`, keyed by draft id.
- The whole draft list is read on every pass. A draft whose `version` changed replaces the stored
  row, and the previous `raw` goes to `message_versions`, as `archive --live` does for edits.
- Labels for the nine ids in `directory._SPECIAL_THREADS`.
- `docs/agent-archive.md`: the nine conversations, which are feeds of copies, how to read a draft.

## Out of scope

- Writing drafts. That is spec 008.
- Drafts as live events. Whether Trouter announces a draft was not observed. Under `--live`,
  drafts are refreshed by the reconcile pass.
- Showing any `48:` conversation in the web widget.
- Removing from the archive what Teams removed from a feed.
- De-duplicating a feed entry against the message it copies.

## Acceptance criteria

- [ ] After a pass on an empty archive, `index.db` holds nine rows whose id starts with `48:`.
- [ ] Each of the eight history-readable ones has in `messages.db` the messages the API returned,
      and a second pass adds nothing and makes no history call beyond the top-up probe.
- [ ] A `48:` conversation with no message gets its index row and an empty store, and the pass
      does not count it as a failure.
- [ ] `archive --no-media` stores the messages of `48:notes` and downloads nothing.
- [ ] Media of `48:notes` lands under `data/48:notes/media/` with the names the other chats use.
- [ ] Drafts: every row of the listing is stored, across two pages when the first is full.
- [ ] Drafts: a row edited between two passes has its new `raw` in `messages` and the old one in
      `message_versions`.
- [ ] Drafts: a row cancelled between two passes is stored as the tombstone, and its text is in
      `message_versions`.
- [ ] Drafts: a failing draft call is logged and counted in `archive_recap`, and the other chats
      of the pass are still archived.
- [ ] `archive --thread 48:drafts` runs the draft fetch. `archive --thread 48:notes` works as any
      other thread.
- [ ] `archive --live` writes a message you post in Notes as it arrives.
- [ ] The web widget lists the same rows before and after the archive holds `48:` conversations.
- [ ] The scope rule is one function. `grep` finds no second copy of the test in `archive.py` or
      `archive_live.py`.

## Phases

### Phase 1 — the eight history-readable conversations
- Work: the shared scope rule, labels, tests.
- **Data model impact**: none. New rows in `chats`, new chat folders.
- **DoD**: tests green. One `--thread` pass per conversation into a scratch data dir: eight
  `48:` rows in `index.db`, each store holding what the API returned, a second pass adding
  nothing. Bytes added under `48:*/media` measured and reported. Widget test green.
  `pre-commit run --all-files` rc=0.

### Phase 2 — drafts
- Work: the draft fetch with paging, storage with versions, `--thread 48:drafts`, recap counts.
- **Data model impact**: none. `messages` and `message_versions` of `48:drafts` use the existing
  schema.
- **DoD**: tests green with HTTP mocked, two pages, an edit and a cancel. On a scratch data dir:
  the row count equals what `GET /v1/users/ME/drafts` returns. A draft scheduled, archived, then
  cancelled and archived again has its text in `message_versions`.

### Phase 3 — docs
- Work: `docs/agent-archive.md`, README.
- **Data model impact**: none
- **DoD**: the doc names the nine ids and marks the feeds. A recipe reads the pending drafts from
  `data/48:drafts/messages.db` and returns the same ids as the API.

## Data model impact (summary)

No schema change. Nine more rows in `index.db` and nine more chat folders.

## Open questions

None.

## Decisions

- All nine, no flag, no filter (babs, 2026-09-30).
- Drafts included (babs, 2026-09-30).
- Built before 008 (babs, 2026-09-30).
- Channels stay behind `--all`. "No filter" is read as no selection among the `48:`
  conversations. Overturnable.
- Media follows the flags of the pass for all nine. Measured in phase 1 without `--videos`:
  `48:calllogs` downloads nothing, and nearly all the bytes come from `48:annotations`, which
  carries the files attached to posts you reacted to. No skip rule: "no filter" stands.
  With `--videos`, `48:calllogs` was not measured.
- Live checks run on a scratch data dir, not on the real archive (babs, 2026-09-30).
- `48:mentions`, `48:notifications`, `48:saved`, `48:starred` and `48:threads` hold copies of
  messages that live in other chats. The archive stores them as they are. Readers that search
  across chats have to leave them out, which spec 008 does.
- Drafts are re-read in full on every pass. The list is short, one request in the common case.
- The draft fetch uses the regional host and the skype token, which read the list. The proxy
  host and the ic3 token are for writes.
