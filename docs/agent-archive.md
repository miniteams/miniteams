# Querying the miniteams archive (agent guide)

TL;DR for Claude/agents mining `data/` produced by `miniteams archive`. Read-only. Everything is
local SQLite + files — no network, no auth, no Teams API.

## Layout

```
data/
  index.db                          # one row per conversation (the channel directory)
  <thread-id>/                      # thread id with `/` → `_`; usually verbatim
    messages.db                     # every message of that conversation, raw API JSON
    media/                          # downloaded images/files (optional, --no-media skips)
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

```python
entries = json.load(open(p))["entries"]        # Stream transcript schema
for e in entries:
    e["speakerDisplayName"], e["text"], e["startOffset"]   # "00:01:23.4567890"
```

`speakerId` is `<oid>@<tid>`; the `<oid>` half matches the `8:orgid:<oid>` MRI in `from`, so a
transcript turn joins onto the chat's participants and `avatars/`. ~5% of entries have an empty
`speakerDisplayName` (unrecognised guests/externals) — fall back to `speakerId`, don't drop the turn.
`startOffset` is relative to the recording start, **not** a wall clock. The parent message's
`composetime` is when the recording was *posted* (after the meeting), so it is not a usable anchor —
keep offsets relative unless you resolve the real start from the calendar.

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
