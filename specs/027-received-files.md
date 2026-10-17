# 027 — Show received files

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-17

## Problem

A file shared in a chat (Teams' file card, `properties.files`) shows in miniteams as its message text
alone: no name, no link. The archiver keeps the reference but never downloads the file: attachment
extraction reads the message HTML only.

## Solution

The chat page shows each shared file as a card (icon by type, name) that opens it. When the archiver
runs with media on, it also downloads these files like other attachments, and the card opens the local
copy when there is one, SharePoint otherwise.

## Scope

- **Card.** Each `properties.files` entry of a message renders as a card in the thread: a type icon and
  the file name, in the message, after its text. Live, history and edited messages alike.
- **Hotlink.** The card links to the file's SharePoint `objectUrl`, opened in a new tab (the browser's
  Microsoft session does the auth). A file without an `objectUrl` shows its name, not a link.
- **Archive.** `properties.files` entries become attachment items of the existing pipeline (SharePoint
  download without Graph, as `sp_file`), in the archive one-shot, the live archiver and the
  `--verify-media` sweep (which catches up on already archived messages).
- **Exclusions honoured.** `--no-media` downloads nothing; `--media` types apply (a video file is
  `videos`, off by default); denied assets back off as today (`--retry-assets` forces them).
- **Local copy first.** When the archive holds the file, the card opens it through the web's existing
  `/media/<thread>/<file>` route; else the hotlink.

## Out of scope

- File cards in the widget's list previews.
- Previews or thumbnails of documents.
- Files shared in the message HTML (already handled by the pipeline).

## Acceptance criteria

- [ ] **AC1** A message with one or more `properties.files` entries shows one card per file (type icon,
  name) in the chat page, live and from history; a message without any shows none.
- [ ] **AC2** A card links to the entry's `objectUrl` in a new tab (`noopener`); an entry with no
  `objectUrl` shows its name unlinked; the name is plain text everywhere.
- [ ] **AC3** With media on and `files` selected, the archive one-shot, the live archiver and
  `--verify-media` download each entry to the chat's media folder, once.
- [ ] **AC4** `--no-media` downloads none; a video entry follows the `videos` type; a denied entry backs
  off and `--retry-assets` forces it, as for other assets.
- [ ] **AC5** When the file is in the archive, the card opens the local copy; when it is not, the
  hotlink.

## Phases

### Phase 1 — Card and hotlink
- Work: render `properties.files` as cards in the chat page, linked to `objectUrl`.
- **Covers**: AC1, AC2
- **Data model impact**: none
- **DoD**: unit tests on the server side of the rendering; browser tests in Chrome and Firefox; full
  suite and pre-commit green; the trial message in the test chat shows its card.

### Phase 2 — Archive and local copy
- Work: extract `properties.files` into the attachment pipeline under the media rules; serve the local
  copy from the card.
- **Covers**: AC3, AC4, AC5
- **Data model impact**: none (files land in each chat's existing `media/` folder)
- **DoD**: unit tests with mocked SharePoint for extraction, exclusions and once-only download; a live
  `--verify-media` run on the test chat only downloads the trial file and the card opens it; full suite
  and pre-commit green.

## Data model impact (summary)

None: files go to the existing per-chat `media/` folder; message rows are unchanged.

## Open questions

None.

## Decisions

- Hotlink always, archive when the archiver runs with media, local copy preferred — user's call
  (2026-10-17).
- No size cap beyond the existing media types — not asked; `videos` stays the big-file opt-in.
- Cards in the chat page only, not the widget's previews — overturnable.
