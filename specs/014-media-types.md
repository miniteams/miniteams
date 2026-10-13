# 014 — Choose which media types are downloaded

**Status**: approved
**Requested by**: babs
**Date**: 2026-10-13

## Problem

Media downloads are all or nothing: `--no-media` skips every attachment, the default takes
images, shared files and transcripts, and `--videos` adds recordings and video files on top. There
is no way to keep, say, transcripts only, or images without documents, while archiving or streaming.

## Solution

A `--media` option takes a comma-separated list of types: `images`, `files`, `transcripts`,
`videos`. Only those types are downloaded; every other part of the run (messages, index, avatars)
is unchanged. The default stays what it is today.

## Scope

- **Types.** Each attachment kind from `attachments.extract` maps to one type:

  | Type | Kinds |
  |---|---|
  | `images` | `image` (AMS images); `file` when the `URIObject` is a picture |
  | `files` | `file` (other `URIObject`s), `sp_file` for SharePoint documents |
  | `transcripts` | `transcript`, `sp_transcript`, every rendition and format Teams serves (AMS VTT/JSON/TXT by content type, SharePoint JSON and VTT) |
  | `videos` | `video`, `sp_file` for recordings and shared video files (`:v:` links, video extensions, `onedriveForBusinessVideo`) |

- **Option.** `--media images,files,transcripts,videos` on `archive` (batch and `--live`) and on
  `stream`. Default `images,files,transcripts`. Unknown names, duplicates and an empty list are
  refused by argparse with the list of valid names.
- **Existing flags.** `--no-media` = an empty selection; `--videos` = the default plus `videos`.
  Both stay; combining either with `--media` is refused (one way to say it per run).
- **`--verify-media` and `--assets-only`** re-check and fetch only the selected types.
- **Already archived files** of a type no longer selected stay on disk and stay listed in the
  manifests (`recordings.json`, message media); nothing is deleted.
- **Docs.** README archive section and `--help` for the option and the type table.

## Out of scope

- Purging media of an excluded type.
- Per-chat or per-size filters, file extension filters.
- A setting in `.env` (the option is per run; a `MINITEAMS_MEDIA` env var can come later).
- Avatars (already `--no-avatars`).

## Acceptance criteria

- [ ] **AC1** `extract` (or a pure helper next to it) classifies every kind into exactly one type,
      including a `URIObject` picture vs document and an `sp_file` document vs recording/video.
- [ ] **AC2** `archive --media transcripts` on a chat holding an image, a document, a recording
      with transcript and video downloads only the transcript, in every format it is served in (both
      SharePoint renditions, the AMS one by content type); messages and index are complete.
- [ ] **AC3** Without `--media` the downloaded set is byte-for-byte the current default;
      `--videos` and `--media images,files,transcripts,videos` download the same set.
- [ ] **AC4** `--no-media` downloads nothing; `--media` with `--no-media` or `--videos` exits with
      a usage error; `--media pdf`, `--media ""` and `--media images,images` are usage errors
      naming the valid types.
- [ ] **AC5** `archive --live` and `stream` honour the selection on new messages.
- [ ] **AC6** `--verify-media --media images` fetches missing images only and leaves missing
      documents, transcripts and videos missing (and unreported as failures).
- [ ] **AC7** Files of an excluded type already on disk are untouched by any run.

## Phases

### Phase 1 — type mapping and option
- Work: type mapping in `attachments.py`, `--media` parsing in `cli.py`, selection threaded through
  `archive`, `archive_live`, `stream` (replacing the `videos: bool` parameters), `--verify-media` /
  `--assets-only`, README and `--help`.
- **Covers**: AC1–AC7
- **Data model impact**: none
- **DoD**: `pytest` green with one test per AC (network mocked, fixtures for each kind);
  `pre-commit run --all-files` rc=0; one `archive --thread <test chat> --media transcripts` run on a
  scratch `--data-dir` downloads only the transcript.

## Data model impact (summary)

None. Selection is per run; manifests keep their shape.

## Open questions

- How does a `URIObject` say it is a picture: its `type` attribute (`Picture.1`) or the file
  extension? To check on archived messages before phase 1.

## Decisions

- One `--media` list rather than one flag per type (babs, 2026-10-13): one place to read the
  selection; `--no-media` and `--videos` kept as shorthands.
- Four types, `files` separate from `images` (babs, 2026-10-13); a recording hosted on OneDrive is a
  `videos` item, not a `files` one.
- Applies to `archive`, `archive --live`, `--verify-media` and `stream` (babs, 2026-10-13).
- Media of a deselected type are kept, never deleted (babs, 2026-10-13).
- `transcripts` means every transcript format, never a subset (babs, 2026-10-13).
