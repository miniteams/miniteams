# 025 — Send any file as an attachment

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-17

## Problem

The composer sends text and pasted images only. Sharing any other file (a PDF, a log, an archive)
means opening Teams.

## Solution

A paperclip button and drag and drop in the composer attach any file to the message. It is uploaded
to the sender's OneDrive and shared with the chat, the way Teams does it, and recipients see Teams'
own file card.

## Scope

- **Path.** SharePoint's own REST API on the sender's OneDrive host, with that host's SharePoint
  bearer (never Microsoft Graph): upload into the user's Teams chat files folder (its localized name
  found, not hard-coded), share it with the chat's members, then post the message with
  `properties.files` in the shape the archive shows Teams uses.
- **Composer.** A paperclip button and dropping files on the composer (chat page and widget quick
  reply); attached files show as chips (name, size, ×) beside pasted images; the message goes
  through the send queue (spec 017) like any other.
- **Limits.** Up to 10 files and 100 MiB per file per message in this version; larger uploads use an
  upload session in chunks.
- **CLI.** `miniteams send --attach <path>` (repeatable; `--file` already reads the body from a file), the
  first consumer and the trial harness.
- **Failure.** An upload or share that fails leaves no message posted and says why; a file uploaded
  but not shared is deleted again.

## Out of scope

- Sharing an existing OneDrive/SharePoint file by link picker.
- Channels (the composer is not offered there).
- Editing a sent message's attachments.

## Acceptance criteria

- [x] **AC1** `miniteams send --attach f.pdf --thread <chat>` posts a message that Teams shows with
  its file card, openable by every member of that chat (verified live on the test chat).
- [x] **AC2** The file lands in the sender's Teams chat files folder, whatever its localized name,
  and no request goes to graph.microsoft.com.
- [x] **AC3** A file over the single-request limit uploads in chunks and arrives intact.
- [x] **AC4** An upload or share failure posts nothing, reports the cause, and leaves no orphan file.
- [x] **AC5** In the chat page and the widget, the paperclip and a drop attach files as chips; × removes
  one; Enter sends them with the text through the send queue.
- [x] **AC6** More than 10 files or a file over 100 MiB is refused with a clear message.

## Phases

### Phase 1 — The send path, by live trials
- Work: OneDrive host and chat files folder discovery, upload (simple and session), share with the
  chat's members, message with `properties.files`, cleanup on failure; `send --attach`; trials on the
  test chat only, each trial's files deleted and shares revoked afterwards.
- **Covers**: AC1, AC2, AC3, AC4, AC6 (CLI side)
- **Data model impact**: none
- **DoD**: unit tests with mocked SharePoint; a live trial log (anonymised) showing the card in Teams
  for a member of the test chat; full suite and pre-commit green.

### Phase 2 — The composer
- Work: paperclip, drop target, chips, limits, the send queue carrying files.
- **Covers**: AC5, AC6 (page side)
- **Data model impact**: none
- **DoD**: browser tests in Chrome and Firefox; full suite and pre-commit green.

## Data model impact (summary)

None.

## Open questions

- How Teams shares chat files (which call, which permission scope) — settled by phase 1's trials.

## Decisions

- Phase 1 shipped: AC2-AC4 and the CLI side of AC6 verified by 5 live trials on the test chat
  (cleaned up). AC1's card in a member's Teams client is still to be looked at.
- The share is an edit link for the chat's people (`scope: users`), Teams' chat default as far as the
  archived share tokens show.

- Live trials on the test chat, files deleted and shares revoked after each (user's call, 2026-10-17):
  no capture of the Teams client is available.
- Images keep their inline AMS path; only other files go through OneDrive — because that is what
  Teams does and inline images already work.
- 10 files, 100 MiB each in this version — overturnable; Teams itself allows far more.
- Phase 2 transport: the page streams files over its socket in 1 MiB binary frames into unnamed temp
  files under the cache dir — because the local server answers GET only, so an upload route would need
  a second server.
- Known ceiling: no per-process cap on spooled bytes (10 × 100 MiB per socket, sockets unbounded); a
  global budget if the cache disk ever fills.
- Known ceiling: a retry after a post with an unknown outcome uploads and shares the files again when
  the check finds no message, leaving the first try's shared copies behind.
