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

- **Card.** Each `properties.files` entry of a message (other than `reference`) renders as a card in the thread: a type icon and
  the file name, in the message, after its text. Live, history and edited messages alike.
- **Hotlink.** The card links to the file's sharing link (`fileInfo.shareUrl`), else its `objectUrl`,
  opened in a new tab (the browser's Microsoft session does the auth). A file with neither shows its
  name, not a link.
- **Pasted links (`state: "reference"`).** No card: a plain link whose text is the file name. When the
  message body already links that file, that link's text becomes the file name instead of a second
  link. No extra download: body SharePoint links are already archived.
- **Archive.** `properties.files` entries become attachment items of the existing pipeline (SharePoint
  download without Graph, as `sp_file`), in the archive one-shot, the live archiver and the
  `--verify-media` sweep (which catches up on already archived messages).
- **Exclusions honoured.** `--no-media` downloads nothing; `--media` types apply (a video file is
  `videos`, off by default); denied assets back off as today (`--retry-assets` forces them).
- **Local copy first.** When the archive holds the file, the card opens it through the web's existing
  `/media/<thread>/<file>` route; else the hotlink. The archive records which local file each SharePoint
  URL was saved as, so a card finds its own copy by URL, never by name, and a held file costs no
  SharePoint call on later passes.
- **Name collisions.** Two different files with the same name in one chat are both kept: the second is
  saved as `name (2).ext`, and so on.

## Out of scope

- File cards in the widget's list previews.
- Previews or thumbnails of documents.
- Files shared in the message HTML (already handled by the pipeline).

## Acceptance criteria

- [ ] **AC1** A message with one or more `properties.files` entries shows one card per non-`reference` file (type icon,
  name) in the chat page, live and from history; a message without any shows none.
- [ ] **AC2** A card links to the entry's `shareUrl`, else its `objectUrl`, in a new tab (`noopener`);
  an entry with neither shows its name unlinked; the name is plain text everywhere.
- [ ] **AC7** A card opens the local copy saved for its own URL; two different files sharing a name never
  open each other's copy; a file already held is not looked up on SharePoint again.
- [ ] **AC8** Two different files with the same name in one chat are both downloaded, the second as
  `name (2).ext`; the same file met twice is downloaded once.
- [ ] **AC6** A `reference` entry shows as a link named after the file, not a card; when the body already
  links it, that link reads as the file name and no second link appears; it is not downloaded again.
- [ ] **AC3** With media on and `files` selected, the archive one-shot, the live archiver and
  `--verify-media` download each entry to the chat's media folder, once.
- [ ] **AC4** `--no-media` downloads none; a video entry follows the `videos` type; a denied entry backs
  off and `--retry-assets` forces it, as for other assets.
- [ ] **AC5** When the file is in the archive, the card opens the local copy; when it is not, the
  hotlink.

## Phases

### Phase 1 — Card and hotlink
- Work: render `properties.files` as cards in the chat page, linked to `shareUrl`, else `objectUrl`;
  `reference` entries as links named after the file.
- **Covers**: AC1, AC2, AC6
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

### Phase 3 — Local copies by URL, name collisions
- Work: record URL → local name on download; cards and skip-if-held look up by URL; a different file
  under a taken name is saved as `name (n).ext`.
- **Covers**: AC7, AC8
- **Data model impact**: new URL → local file table in each chat's `messages.db`
- **DoD**: unit tests for the table, lookup, collision naming and no-network skip; full suite and
  pre-commit green; live `--verify-media` on the test chat.

## Data model impact (summary)

A new table in each chat's `messages.db`: SharePoint URL → local file name (additive, created on open, no
migration of existing rows). Files go to the existing per-chat `media/` folder; message rows are unchanged.

## Open questions

None.

## Decisions

- Hotlink always, archive when the archiver runs with media, local copy preferred — user's call
  (2026-10-17).
- No size cap beyond the existing media types — not asked; `videos` stays the big-file opt-in.
- Cards in the chat page only, not the widget's previews — overturnable.
- URL → local file table and `name (n).ext` collisions, as phase 3 — user's call (2026-10-17, after
  the phase 2 review).
- `shareUrl` before `objectUrl`: most received files live in the sender's OneDrive; `reference` entries
  are links named after the file — user's calls (2026-10-17, after review).
