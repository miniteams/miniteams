# Querying the miniteams archive (agent guide)

TL;DR for Claude/agents mining `data/` produced by `miniteams archive`. Read-only. Everything is
local SQLite + files — no network, no auth, no Teams API.

## Layout

```
data/
  index.db                          # one row per conversation (the channel directory)
  <thread-id>/                      # thread id with `/` → `_`; usually verbatim
    messages.db                     # every message of that conversation, raw API JSON
    media/                          # downloaded images/files (optional, --no-media skips);
                                    # with --videos also meeting recordings/shared videos, named
                                    # <driveItem-name>.mp4 (SharePoint) or <id>.video.mp4 (AMS)
      recordings.json               # recording ↔ video/transcript pairing (see below)
    avatars/                        # sender avatars, <sanitized-MRI>.jpg
```

`index.db` is the entry point: never guess a thread id, look it up by `label`/`topic`.

## Schemas

```sql
-- index.db
chats(id TEXT PK, dir TEXT, label TEXT, topic TEXT, participants TEXT /*JSON*/,
      raw TEXT /*JSON conversation object*/, backfill_done INT, last_fetch_at TEXT,
      history_denied_at TEXT)

-- <thread>/messages.db
messages(id TEXT PK, composetime TEXT /*ISO-8601 UTC*/, raw TEXT /*JSON message*/)
denied_assets(url TEXT PK, status INT, at TEXT,   -- give-up cache, ignore when reading
              attempts INT, retry_after TEXT)     -- '' = permanent (403); else retry past it (404)
```

Index on `composetime` — always order/filter on it, never on `id`.

## Message record (`messages.raw`)

Fields that matter:

| Field | Notes |
|---|---|
| `composetime`, `originalarrivaltime` | ISO-8601 **UTC**, e.g. `2026-07-22T19:24:11.7020000Z` |
| `imdisplayname` | sender display name — use this; `fromDisplayNameInToken` is absent on ~1% |
| `from` | sender MRI URL, ends with `8:orgid:<uuid>` (stable id; avatars are named after it) |
| `content` | **HTML** (`RichText/Html`), not text. Empty string = deleted or activity event |
| `messagetype` | `RichText/Html` (~99%), `Text`, `Event/Call`, `ThreadActivity/*` (join/leave/topic) |
| `properties.emotions` | reactions: `[{key: "like", users:[{mri, time}]}]` |
| `properties.edittime` | present if edited; `properties.deletetime` / `hardDeleteTime` if deleted |
| `properties.files` / `mentions` / `links` / `cards` | **JSON-encoded strings** → second `json.loads` |
| `amsreferences` | AMS object ids → filenames under `media/` |

Filter noise first: `messagetype = 'RichText/Html'` and non-empty `content`.

## Recipes

### Find a channel

```bash
sqlite3 data/index.db \
  "select id, label from chats where label like '%COPS%' order by label;"
```

### Dump one channel as text (the workhorse)

```python
import sqlite3, json, re, html

def messages(thread_id, data_dir="data", since=None, until=None):
    """Yield (composetime, author, plain_text) oldest-first."""
    con = sqlite3.connect(f"file:{data_dir}/{thread_id}/messages.db?mode=ro", uri=True)
    sql, args = "select composetime, raw from messages where 1=1", []
    if since:
        sql += " and composetime >= ?"
        args.append(since)
    if until:
        sql += " and composetime < ?"
        args.append(until)
    for ct, raw in con.execute(sql + " order by composetime", args):
        d = json.loads(raw)
        if d.get("messagetype") != "RichText/Html":
            continue
        text = html.unescape(re.sub(r"<[^>]+>", " ", d.get("content") or ""))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            yield ct, d.get("imdisplayname", "?"), text
```

`<[^>]+>` stripping is enough — Teams HTML is flat (`<p>`, `<span>`, `<a>`, emoji `<img>`).

### Keyword timeline across several channels

Search in Python over the stripped text, not in SQL over `raw`: the HTML markup and base64-ish
URLs in `raw` produce false positives. Two-part regexes beat one broad one — require a topic word
**and** a qualifier (`\blogs?\b` AND `json|log4j|volum|quota`), otherwise every "check the logs"
support message matches.

### Reactions / edits / deletions

```python
props = d.get("properties") or {}          # already a dict
for e in props.get("emotions", []):        # real list, no re-parse
    print(e["key"], len(e["users"]))
edited  = "edittime" in props
deleted = "deletetime" in props or "hardDeleteTime" in props
```

### Attachments

`d["amsreferences"]` → files in `media/` named `<object-id>[.full].<ext>` (`.full` = original
resolution, the bare stem is the optimized variant). SharePoint documents keep their real filename;
meeting transcripts land as `sp-<sha1>.transcript.*` or `<object-id>.transcript.*`. Resolve by glob
on the stem; extensions come from the server's content-type, except the SharePoint transcripts whose
`.json`/`.vtt` is pinned by the rendition requested.

### Meeting transcripts — use the `.json`

Three shapes exist; start from the `.json`, but it is not always sufficient:

| File | Speakers | Timing | Durability |
|---|---|---|---|
| `sp-<sha1>.transcript.json` | ✅ `speakerDisplayName` + `speakerId` | one span per turn | durable (SharePoint) |
| `sp-<sha1>.transcript.vtt` | ❌ stripped | sub-cues, ~2.5× finer | durable (SharePoint) |
| `<object-id>.transcript.vtt` | ✅ `<v Name>` cue tags | sub-cues | AMS, **expires in ~3-4 weeks** |

The two SharePoint renditions are the same transcript at different granularity, and **neither is
derivable from the other** — start from the `.json`, and only join the `.vtt` when you need
subtitle-level timing. The join key is the entry id: JSON `…/9` ↔ VTT cues `…/9-0`, `…/9-1`, ….

#### Pairing a video with its transcript

**Start with `media/recordings.json`** — one entry per recording message, written by every media
pass: `{title, duration, videos: [...], transcripts: [...], message_id, composetime}`, listing only
files present on disk (empty lists = the recording existed but nothing was recoverable).

Fallback for chats not yet re-swept: filenames alone don't link them (`sp-<sha1>` is a URL hash,
videos carry a driveItem name or an AMS object id). The join lives in the **`Media_CallRecording`
message** that references both:

| In the message raw | On-disk file |
|---|---|
| `<item type="onedriveForBusinessTranscript" uri="U">` | `sp-{sha1(U.split('?')[0])[:16]}.transcript.{json,vtt}` |
| `<item type="onedriveForBusinessVideo">` → driveItem name | `<OriginalName>.mp4` (SharePoint copy) |
| `<item type="amsVideo" uri=".../objects/<id>/views/video">` | `<id>.video.mp4` (AMS fallback copy) |

```python
import hashlib, re
raw = row_raw  # one Media_CallRecording message
sp_t = re.search(r'onedriveForBusinessTranscript\\?" uri=\\?"([^"\\]+)', raw)
stem = "sp-" + hashlib.sha1(sp_t.group(1).split("?")[0].encode()).hexdigest()[:16]
ams  = re.search(r'amsVideo\\?" uri=\\?"[^"\\]*/objects/([^/\\]+)/', raw)
# transcript: media/{stem}.transcript.json — video: media/{OriginalName v=…}.mp4 or media/{ams}.video.mp4
```

Shortcut: the AMS video and AMS transcript of one recording share the object id — `<id>.video.mp4`
pairs with `<id>.transcript.vtt` by stem alone, no message lookup needed.

#### Entry schema

`json.load(p)["entries"]` — a list of turns, chronological. Fields that matter:

| Field | Notes |
|---|---|
| `text` | the turn, already plain text — no HTML, no escaping |
| `speakerDisplayName` | `"Damien DEGOIS"`, or **empty** for an unrecognised speaker |
| `speakerId` | `<oid>@<tid>`; the `<oid>` half matches the `8:orgid:<oid>` MRI in a message's `from` |
| `startOffset` / `endOffset` | `"00:01:23.4567890"`, relative to the recording start |
| `id` | `<guid>/<n>`; the VTT's sub-cues are `<guid>/<n>-0`, `-1`, … — the join key |
| `spokenLanguageTag` | `fr-fr` / `en-us` / … — mixed across an archive, filter on it before NLP |
| `confidence` | ASR confidence; median 0.56 under 25 chars vs 0.82 above — short turns score low by nature, not by error |
| `hasBeenEdited` | manual Stream edit; false on every entry of this archive |

`speakerId` makes a turn joinable onto the chat's `participants` and `avatars/`. When
`speakerDisplayName` is empty, fall back to `speakerId` — never drop the turn.

`startOffset` is **not** a wall clock. The parent message's `composetime` is when the recording was
*posted* (after the meeting), so it is not a usable anchor — keep offsets relative unless the real
start is resolved from the calendar.

#### Transcripts are frozen — never re-fetch hoping for more

Microsoft does **not** revise a transcript after publication. Five partially-anonymous transcripts
from Sept–Dec 2025 (159/222, 721/1003, 409/908, 365/514, 458/644 anonymous turns) re-fetched in
July 2026 came back **byte-identical**, with zero turns gaining a name.

Consequences: skip-if-exists is safe (there is no newer version to miss); an empty
`speakerDisplayName` is permanent, not "not yet resolved"; and revision detection (`cTag`
polling, re-resolving the transcript id) solves a problem that does not exist — do not build it.

Anonymity is concentrated, not spread — measure before assuming a gap is everywhere:

```python
import json, pathlib
for p in sorted(pathlib.Path("data").glob("*/media/sp-*.transcript.json")):
    e = json.loads(p.read_text())["entries"]
    anon = sum(1 for x in e if not x.get("speakerDisplayName"))
    if anon:
        print(f"{anon:5}/{len(e):<5} {p.parent.parent.name[:50]}")
```

#### Recipe — what one person said

```python
import json, pathlib

def turns_by(name, data_dir="data"):
    """Yield (meeting_dir, startOffset, text) for every turn attributed to `name`."""
    for p in pathlib.Path(data_dir).glob("*/media/sp-*.transcript.json"):
        for e in json.loads(p.read_text())["entries"]:
            if e.get("speakerDisplayName") == name:
                yield p.parent.parent.name, e["startOffset"], e["text"]
```

Match on `speakerDisplayName` for readability, on `speakerId` when a display name is ambiguous
(homonyms, renamed accounts) — the `oid` is the stable identity.

#### Two file-level gotchas

A stem may hold only the `.vtt`: the recording was deleted before the `.json` could be fetched, and
that file is now the only copy — speakers unrecoverable for that meeting.

Transcripts are written `0600` (they attribute every turn by name), so read them as the owner —
files fetched before that became the rule may still sit at the umask default.

## Gotchas

- **Quoted replies duplicate text.** Teams inlines the quoted message in the reply's HTML, so a
  keyword can match twice, dated at the reply. Dedupe on the leading quoted block before counting
  first occurrences.
- **`composetime` is UTC**; the humans are Europe/Paris. A 19:24Z message is a 21:24 local one —
  matters when correlating with "hier soir".
- **Threading is flat.** No parent id: reply context comes from the inlined quote and adjacency.
- **Meeting chats** are `19:meeting_<base64>@thread.v2` — the chat of a recurring meeting, mostly
  agenda + follow-ups, distinct from the standing channel on the same topic. Check both.
- **Coverage is not uniform.** `backfill_done = 0` means history is partial; `history_denied_at != ''`
  means the API refused it. Check before concluding "nothing was said before date X".
- **WAL files** (`-wal`, `-shm`) are normal; open read-only (`mode=ro`) so a concurrent
  `archive --loop` is never blocked.
- **Confidential by construction.** Internal names, URLs, incidents. Keep the analysis local — never
  publish an extract to an external host (artifacts, pastebins) without an explicit go-ahead.
