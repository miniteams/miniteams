# 037 — Up-to-date app cards

**Status**: in progress
**Requested by**: the user
**Date**: 2026-10-29

## Problem

A link-unfurl card (Jira, …) is stored as it was when the message was posted. Teams shows it up to
date because its client asks the app's bot for a fresh card each time it displays it; the message
itself never changes. miniteams shows the posted snapshot: a Jira ticket stays "BACKLOG,
Unassigned" in the chat page while Teams shows "DONE, Assigned to …". The MCP shows no card content
at all.

## Solution

When a message with a refreshable card is shown in the chat page or returned by the MCP, miniteams
asks the card's bot for the current card, the way the Teams client does, and shows that instead. The
latest card is kept in the archive, so the next view, a restart or an offline read starts from it.

## Spike (2026-10-29, verified live, read-only)

- **Bot id**: `POST https://teams.microsoft.com/api/mt/beta/users/apps/batchedDefinitions`, body
  `[<appId>]`, `Authorization: Bearer` (the profile lookup's AAD token) + `X-Skypetoken` → a list of
  app definitions; the bot is `inputExtensions[0].botId` (Jira: also `bots[0].id`).
- **Invoke**: `POST https://{chat host}/v1/agents/28:<botId>/invoke`, `X-Skypetoken`,
  `X-MS-AutoRefreshRequestInThread: <threadId>`, body `{"name": "adaptiveCard/action", "appId",
  "value": {"action": {"type": "Action.Execute", "verb", "data"}, "trigger": "automatic"},
  "serverMessageId", "clientMessageId", "conversation": {"id": <threadId>}}` → `200 {"statusCode":
  200, "type": "application/vnd.microsoft.card.adaptive", "value": <AdaptiveCard>}`. The Teams web
  code also handles `401 …loginRequest`, `412 …preconditionFailed`, `429 …retryAfter` and
  `4xx/5xx …microsoft.error`.
- Teams refreshes a card whose `refresh` has no `userIds`, or whose `userIds` lists the user.
- The returned card renders correctly through the existing card renderer.

## Scope

- Cards in `properties.cards[]` (app cards: `appId` + `cardClientId`) whose content has
  `refresh.action.type == "Action.Execute"`, eligible per the `userIds` rule above.
- Refresh on display in the chat page (history pages and live rows) and in MCP `read_messages`.
- The latest card per (chat, message, card) kept in `data/cards.db`; the stored raw message is never
  modified.
- MCP records gain the card text.

## Out of scope

- Cards in a Swift payload (`RichText/Media_Card`, bot-sent cards): their bot is the sender, a path
  the spike did not verify.
- Any user action on a card (`Action.Execute`/`Action.Submit` buttons, sign-in cards).
- Refreshing from the archiver, the CLI, search results or the widget.
- Teams' member-count limit on automatic refresh (its default is unknown).

## Acceptance criteria

- [ ] **AC1** Given a chat page showing a message whose app card declares an eligible refresh, when
      the stored card is older than the refresh interval, then miniteams asks the bot and the row
      shows the returned card without a reload.
- [ ] **AC2** A card refreshed less than the refresh interval ago is shown from `data/cards.db`
      without asking the bot again.
- [ ] **AC3** After a restart, the page and the MCP show the last refreshed card before any new
      request.
- [ ] **AC4** A card whose `refresh.userIds` does not list the user, a card without `refresh`, or a
      Swift card is never refreshed.
- [ ] **AC5** Any failure (no app definition, no bot id, non-200, a `statusCode` other than 200, a
      type other than an adaptive card, a timeout) keeps the card shown so far, logs one structured
      line without tokens, and is not retried before the refresh interval; a `429` waits at least
      its `retryAfter`, capped at a day.
- [ ] **AC6** One message shown twice at once (two pages of one web process) triggers one request;
      requests per process are capped in concurrency.
- [ ] **AC7** Bot ids are looked up once per app per process lifetime, with a bounded cache; an app
      without one is negative-cached for the refresh interval.
- [ ] **AC8** MCP `read_messages` records carry `cards`: one plain-text line per card (its text
      blocks), the refreshed version when there is one.
- [ ] **AC9** MCP `read_messages` refreshes stale eligible cards within a total time budget;
      past it, it returns the stored cards.
- [ ] **AC10** The stored raw message and its versions are unchanged by a refresh.
- [ ] **AC11** A real Jira link card whose issue changed since it was posted shows the current
      status and assignee in the chat page and in MCP `read_messages`.

## Phases

### Phase 1 — Refresh client and store
- Work: eligibility check, app definition lookup, invoke, response validation, `data/cards.db`
  read/write, row rendering using the stored card when present.
- **Covers**: AC2, AC3 (store side), AC4, AC5, AC6 (client side), AC7, AC10
- **Data model impact**: new `data/cards.db`, table `cards(thread_id, message_id, card_id, card,
  refreshed_at, retry_at, PRIMARY KEY (thread_id, message_id, card_id))`.
- **DoD**: unit tests with a fake transport for every AC above, green under the bounded test run;
  pre-commit clean.

### Phase 2 — Chat page
- Work: rows with stale eligible cards schedule a refresh through the refresher (its deduplication
  and cap); a result re-renders the record and is pushed as the existing `edit` frame.
- **Covers**: AC1, AC3 (page), AC6
- **Data model impact**: none
- **DoD**: page test with a fake bot shows the row replaced; a test server on its own bind and
  config dir shows a real changed Jira card up to date.

### Phase 3 — MCP
- Work: `cards` text in records, refresh inside `read_messages` under a time budget.
- **Covers**: AC8, AC9, AC11
- **Data model impact**: none
- **DoD**: MCP tests green; `read_messages` on a real chat returns a changed Jira card up to date.

## Data model impact (summary)

- New `data/cards.db` (one table, see phase 1). `messages.db` and `index.db` untouched.

## Open questions

- [x] Where the refreshed card lives: the archive (user, 2026-10-29).
- [x] Surfaces: chat page and MCP `read_messages` (user, 2026-10-29).

## Decisions

- A separate `data/cards.db`, not a table in `messages.db`: the archiver stays the only writer of
  the archived messages, and the web and MCP processes, which open those read-only, write only this
  cache. WAL and a busy timeout let both write it.
- Refresh interval 10 minutes; MCP budget 5 s per call; 4 concurrent requests per process; bot id
  cache of 64 apps; a `429` wait capped at a day; a body `retryAfter` read as seconds (its unit is
  unknown, seconds never waits less than asked).
- A `429` from either endpoint holds every card of that app until its longest wait ends, with no
  request and no log line; 64 held apps at most.
- The refreshed card is keyed by `cardClientId`; an edit that keeps the id keeps the refreshed card.
- The member-count limit is ignored: one user's views, throttled by the interval.
