# 044 — Meeting follower: live personal transcription

**Status**: in progress
**Requested by**: babs
**Date**: 2026-11-14

## Problem

Teams transcribes a meeting only when someone in it starts the transcription, and the result lands
after the call, if at all. Calls without it, ad hoc group calls and 1:1 calls leave no text behind.
Nothing records what was said, by whom, for my own later reading or for an agent to query.

## Solution

When I join a call from any device (desktop, phone, web), miniteams joins it too, as a second endpoint
of my own account, listens, and writes a live transcript, one line per utterance, named after its
speaker. When I leave, it leaves. The transcript sits in the archive and the MCP server reads it, during
the call and after. There is no UI in this iteration.

How it works, from the probe of 2026-11-13 and grill-room spike S18:

- **Trigger.** Every endpoint of my account registered on the Trouter path `NGCallManagerWin` gets a push
  `{"evt": 115, "cp": <base64 gzip JSON>}` when I join a call: `correlationId`, `conversationController`,
  `userJoinTime`, `groupContext {threadId, messageId}`, `meetingInfo {organizerId, tenantId}`. That is all
  a join needs: no Graph, no join link. `evt 116`, same payload, is "active call ended": seen live
  0.5 s before the chat's `Event/Call` ended, when I left a call I was alone in.
- **Join and media** follow S18: phase 1 with `meetingInfo`, no `preheatProperties`, roster `Full`, fresh
  message id per request, the `mediaRenegotiation` answer, ICE + SDES SRTP, the `x-multichannel` audio
  codec (one G.722 16 kHz block per active speaker, tagged by MSI, up to 4 at once), leave by `DELETE` on
  the call leg.
- **Speech to text** reuses grill-room's `grill_stt` VAD and engines (Parakeet through transcribe.cpp,
  or faster-whisper), **copied** into miniteams: no dependency on grill-room.

## Scope

- Two hosts for the trigger, same code:
  - `archive --live --follow`: the archiver's existing Trouter socket also registers on
    `NGCallManagerWin`;
  - `miniteams follow`: a standalone process with its own Trouter socket and endpoint id, for a setup
    that does not archive, and for tests.
- On `evt 115` the trigger starts one follower per call, `miniteams follow --call <push>`, a detached
  process (own session) that does join, media and STT, and stops when I leave or the call ends. A
  restart of the trigger's host does not touch it.
- Every call kind: scheduled meetings, ad hoc group calls, 1:1 calls.
- Join, media, per-speaker demux, G.722 decode, speaker names from the roster (names store as fallback).
- Live STT, engine `parakeet` (default) or `whisper`, chosen by setting.
- Transcript files in the archive, appended live.
- MCP `read_transcript` lists and reads followed calls.
- A global on/off switch and a per-chat skip list.
- A `transcript` optional extra; `scripts/build-transcribe.sh` (the repo has no Makefile) builds libtranscribe
  and fetches the Parakeet and Whisper models.

## Out of scope

- Any UI: live panel, widget icon, window. A later spec.
- Summaries, action items, translation.
- Speaking, sending audio other than silence, video, screen share.
- More than 4 simultaneous speakers (server limit, S18 §3.5): a 5th voice talking over 4 others is lost.
- Keeping audio. Only text is stored; a `--capture` debug flag may dump RTP for diagnosis, off by default.
- A dedicated account: the follower joins as me.
- Calls already in progress when `follow` starts (no `evt 115` to replay). A later spec may use the
  active-call service (`GET {activeCallServiceBaseUrl}/api/v1/user/{mri}/conversations`).
- Go port, shared code with grill-room.

## Acceptance criteria

- [ ] **AC1** Given `follow` runs, when I join a scheduled meeting from the Teams desktop, then the
      follower is in the call within 10 s of `userJoinTime`, and the meeting shows no extra participant.
- [ ] **AC2** Same as AC1 for an ad hoc group call and for a 1:1 call, joined or answered on any device.
- [ ] **AC3** One follower per call: the `evt 115` its own join causes, a redelivered push, a second
      device of mine joining, and two triggers running at once (`archive --follow` and `follow`) start
      no second follower. The guard is a lock per `correlationId` under `<data-dir>/follow/`, released
      when the follower exits, even on a crash.
- [ ] **AC4** When my last device in the call leaves (the follower aside) and others stay, the follower
      leaves within 15 s, its `DELETE` on the call leg answers 2xx, and no ghost participant remains. While
      another of my devices is still in the call (PC and phone, a transfer, an added device), one of them
      leaving does not stop it.
- [ ] **AC5** When the call ends for everyone, the follower stops within 15 s and closes the transcript.
- [ ] **AC6** Each line names its speaker from the run's own roster (MSI → `displayName`); an MSI absent
      from the roster falls back to the names store, then to the MRI. Overlapping speakers (up to 4) each
      get their own lines.
- [ ] **AC7** Latency on this machine with `parakeet`, live: from end of speech to line written, median
      ≤ 2 s, p90 ≤ 4 s, over a call of 10 min or more with 3 or more speakers.
- [ ] **AC8** The transcript is NDJSON, one object per line: `t` (UTC ISO start of the utterance),
      `end`, `speaker`, `mri`, `text`, `engine`; a header line holds `call` (`correlationId`), `thread`,
      `kind` (meeting, group, 1:1), `started`. A line is readable by another process as soon as written.
- [ ] **AC9** MCP `read_transcript` without `message_id` lists the thread's followed calls next to its
      recordings; with `call` it returns the lines, paged by offset; both work while the call runs.
- [ ] **AC10** `engine` = `parakeet` or `whisper`. A missing library, model or extra makes `follow`
      refuse to start with the exact command to fix it, and never join a call it cannot transcribe.
- [ ] **AC11** With the switch off, or the chat in the skip list, `follow` joins nothing and logs the
      skip with the thread id.
- [ ] **AC12** Nothing but text is written: no audio file exists after a call unless `--capture` was set.
- [ ] **AC13** A Trouter drop or a token renewal neither stops nor duplicates a running follower; the
      socket re-registers on `NGCallManagerWin`.
- [ ] **AC14** A failed join (lobby timeout, refusal, ICE failure) is logged with its cause, retried
      once, then given up; the error lands in the transcript header.
- [ ] **AC15** Without the `transcript` extra, every other miniteams command works unchanged, and
      `miniteams follow` says which extra to install.
- [ ] **AC16** Copied files from `grill_stt` name their origin (grill-room path and commit) and keep the
      Apache-2.0 notice.
- [ ] **AC17** Restarting `archive --live --follow` during a call neither cuts the transcript nor starts
      a second follower; a failure in the trigger code is logged and never stops the archiving.

## Phases

### Phase 1 — Spike gates (scratchpad, never committed)
- Work: in `.scratchpad/`, answer the three unknowns that can change the design; write the conclusions
  into **Decisions** below.
  1. **Leave detection** while the follower keeps my account in the call: does `evt 116` still come, per
     device or only for the last one? Does the roster show each of my other endpoints (their
     `details.endpointId`), so the follower can count those that are not itself, with two devices of mine
     in the call and one leaving? Fallbacks in order: roster endpoints, `evt 116`, active-call service,
     presence. `evt 116` alone may not stop a follower while another device of mine stays (AC4): the
     Phase 2 trigger's stop on 116 becomes a hint the follower confirms.
  2. **Join from the push**, for each call kind: phase 1 with the push's `meetingInfo`/thread, or a
     direct join on its `conversationController`; which one attaches to the live call instead of
     creating a new one (S18 hit this trap with chat links).
  3. **Python media**: `aioice` with STUN, `pylibsrtp` with SDES keys, PyAV decoding G.722, the
     `x-multichannel` demux, on a live join; CPU and drift over 10 min.
- **Covers**: none
- **Data model impact**: none
- **DoD**: Decisions holds one verified answer per unknown, each citing the run that showed it.

### Phase 2 — Trigger
- Work: the trigger (`NGCallManagerWin` registration, `cp` decode keeping `evt`, switch and skip
  list), hosted by `archive --live --follow` and by `miniteams follow` (own endpoint id); the per-call
  lock; the detached `miniteams follow --call` launch; the extra and its startup checks. Followers are
  stubs that log start and stop.
- **Covers**: AC3, AC11, AC13, AC15, AC17
- **Data model impact**: `follow.json` in the config dir (switch, skip list); lock files under
  `<data-dir>/follow/`
- **DoD**: unit tests on recorded push shapes (synthetic ids) for 115, 116, redelivery, self-join and
  two triggers racing on one lock; live: joining and leaving a meeting logs one start and one stop,
  with the archiver restarted in between.

### Phase 3 — Join, media, leave
- Work: signaling per S18 and Phase 1 (create/join, Trouter callAgent callbacks with the expected
  response headers, acceptance, renegotiation answer, roster `Full`), ICE, SRTP, silence out, demux,
  G.722 to 16 kHz PCM per MSI, leave detection and `DELETE`, retry once.
- **Covers**: AC1, AC2, AC4, AC5, AC14
- **Data model impact**: none
- **DoD**: tests on synthetic captures (wraps, gaps, 2 to 4 speakers, a new MSI mid-call); live: each
  call kind joined and left cleanly, `DELETE` 2xx logged.

### Phase 4 — Speech to text and transcript files
- Work: copy from grill-room `stt/grill_stt` @ `56e5e89` (each file identical there to its last change:
  `89744d1` for `line.py`, `engines.py`, `parakeet.py`, `transcribe.py`; `ae133de` for `vad.py`,
  `pipeline.py`, `config.py`; each file's notice names its own):
  - as is: `line.py`, `parakeet.py`, `transcribe.py`;
  - adapted: `engines.py` (an env per engine, `python -P -m miniteams.stt.engines`, miniteams logging,
    HF offline instead of grill's secret filter, a final stop safe from another thread, a clean exit on
    stop), `vad.py` (`Cut.trail_s` and `Cut.end_ago_s`, to place a cut on the call's timeline);
  - trimmed: `pipeline.py` (queue, per-speaker order, `Plausibility` filters; drop `RoomAPI` and the
    Prometheus metrics, replaced by the NDJSON writer), `config.py` (engine and VAD settings only, merged
    into miniteams `Settings`);
  - tests: `test_vad.py`, `test_parakeet.py`, `test_pipeline.py`, `test_plausibility.py`,
    `test_pool.py`, `fake_engine.py` and the fixtures they need;
  - not copied: `worker.py`, `replay.py`, `__main__.py`, `logs.py`, the LiveKit tests.

  One pipeline per call feeding up to 4 tracks; NDJSON writer; `scripts/build-transcribe.sh` builds
  libtranscribe (pinned commit, AVX2, rewritten from grill-room's `stt/Dockerfile`) and fetches the GGUF
  model and faster-whisper small (pinned revision), all under the cache dir.
  `faster-whisper` is needed with either engine: the VAD loads its Silero model from that package.
- **Covers**: AC6, AC7, AC8, AC10, AC12, AC16
- **Data model impact**: transcript files under `<data-dir>/follow/<YYYY-MM>/<started>-<call>.ndjson`
- **DoD**: replay test of a synthetic multi-speaker WAV through both engines; live call meets AC7, the
  latency computed from the follower log's `line_done.latency_s` by `scripts/follow-latency.py`.

### Phase 5 — MCP
- Work: `read_transcript` lists followed calls of a thread and pages one by `call`.
- **Covers**: AC9
- **Data model impact**: none
- **DoD**: MCP tests on fixture files, including a file still being appended.

## Data model impact (summary)

- `follow.json` (config dir): `{"enabled": bool, "skip": [thread ids]}`.
- `<data-dir>/follow/<YYYY-MM>/<started>-<call>.ndjson`: header line + one line per utterance.
- `<data-dir>/follow/<call>.lock`: held by the running follower.
- No database change.

## Open questions

- (none)

## Decisions

- Python, inside miniteams, as a separate process: auth, Trouter, config and names store are already
  here; a STT crash cannot take the archiver or the web down.
- `grill_stt` is copied, not depended on (user call, 2026-11-14).
- Transcripts live in the archive (`--data-dir`), not the cache: they cannot be regenerated.
- Text only by default; `--capture` exists for protocol breakage diagnosis.
- On/off and skip list are a config file plus CLI, since this iteration has no UI.
- Own endpoint id for standalone `follow`, as the web and archiver have theirs: one socket per
  registration id.
- The archiver hosts only the trigger; each call runs in its own detached process, so archiver
  restarts (one per release) do not cut a transcript (user call, 2026-11-14).
- The lock is an advisory `flock` on a file in the data dir: the kernel drops it when the holder dies.
  ponytail: flock on NFS depends on the mount's lock support; a local run dir if it proves flaky.
- Live, 2026-11-14 (probe, 2 runs): `evt 115` arrives about 0.1 s after `userJoinTime`, `evt 116` on
  leaving, both on a second endpoint of mine. Still open for Phase 1: `evt 116` when others stay, and
  when the follower keeps my account in the call.
- Phase order 2 → 4 → 1 → 3 → 5 (user call, 2026-11-14): the offline phases go first; the spike
  waits for real calls. A prototype follower runs permanently meanwhile and follows every call I join.
- Phase 1 results (spike in `.scratchpad/044/spike/`, never committed):
  - Q3, offline part, 2026-11-14: PyAV decodes the `x-multichannel` G.722 blocks packet by packet with
    output identical to the ffmpeg CLI on the whole stream (S18 run of 1 min, 5 MSIs, 2969 packets,
    0.27 s CPU). The same capture paced on its RTP clock through `Session` (parakeet): 12 lines on 3
    speakers, latency median 1.19 s, p90 1.98 s, max 3.0 s. `aioice` and `pylibsrtp` against Teams: _live,
    to fill_.
  - Q1 leave detection, live in a test tenant, 2026-11-14 (two devices of mine plus the S18 client, two
    talkers):
    - `evt 115` fires once, on my first endpoint's join; neither a second device nor the follower pushes
      one. `evt 116` fires once, when the last endpoint of my account leaves, the follower included: never
      while the follower is in the call.
    - My roster entry is one participant whose `call.mediaStreams` holds one `main-audio` stream per
      endpoint; each device leaving drops its stream from the next `Full` rosterUpdate (~1 s).
      `details.endpointId` is zeros and `participantLink` is shared, so the follower recognises itself by
      its own audio MSI, `a=x-source-streamid` on the `main-audio` m-line of the server's answer SDP.
    - Leave rule: the follower leaves when my roster entry has no `main-audio` stream but its own. The
      trigger's stop on `evt 116` only covers the follower already gone or the call over.
  - Q2 join from the push:
    - Meeting and ad hoc group call: the push carries `groupContext` and `meetingInfo` (for a group call
      the caller is the organizer, `messageId` "0"); the meetup-join link built from it attaches to the
      live call.
    - 1:1 call: the push has `conversationController` and `additionalData.participantList` but no
      `groupContext` nor `meetingInfo`; an `evt 107` (incoming call, `links.attach`) precedes it. The join
      must go straight to `join_conversation_with_sdp` on the push's `conversationController`: not tried.
  - Q3 live (`aioice`, `pylibsrtp` against Teams) and the 1:1 join are settled by Phase 3's first slice,
    in the test tenant; if either fails, Phase 3 stops at that fork.
