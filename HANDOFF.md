# Handoff: Teams real-time message-dump CLI (POC / MVP)

**Goal:** a personal, read-only CLI that authenticates as the signed-in user, opens an
**outbound** connection to Microsoft's Trouter real-time channel (the same one the official
Teams client uses), and prints incoming chat messages to stdout. No inbound endpoint, no
polling — push-grade real-time by construction.

**Status of this doc:** the flow below is reconstructed from the reference 3rd-party client
`EionRobb/purple-teams` (GPLv3) and Microsoft's own docs. Endpoints and message framing are
taken verbatim from that source. The few values that *drift* or are *tenant-specific*
(client_id, OAuth scope, version strings) are explicitly marked **VERIFY** — capture them from
a live browser session rather than trusting a hardcoded guess.

---

## 0. Scope

> **Update (post-MVP):** the items below marked "out of scope" were since implemented —
> `send` (chatsvc REST), `update`/edit (PUT `.../messages/<id>` with `skypeeditedid`), `dump`
> (history backfill via `/messages` time-windowed paging), plus thread metadata, reactions and
> attachment download. This section documents the original POC boundary; see `README.md` for the
> current feature set.

**In scope (MVP):**
- Interactive auth (browser auth-code+PKCE preferred; device-code as fallback).
- AAD token → Skype token exchange.
- Trouter handshake + websocket + registration + keepalive.
- Parse delivered `/messaging` events and print `{timestamp, conversation, sender, body}` to stdout.

**Out of scope (POC):**
- Sending messages (different stack — native chatsvc REST with the skype token).
- History backfill — if needed, that's `GET /users/{id}/chats/getAllMessages/delta`
  (documented, pull-based, last 8 months). Trouter only delivers *new* events from connect time.
- Presence/calls — they arrive on the same socket; filter them out for MVP (see §5).

**Legal/operational note:** this replicates the official client's auth and transport. It relies
on a Microsoft **first-party `client_id`** and undocumented endpoints. That is the same posture
as purple-teams/AADInternals — interop, not sanctioned API. Expect it to break when Microsoft
rotates flows; keep the code thin so re-fixing is cheap. Use only against your own account.

---

## 1. End-to-end data flow

```
[1] MSAL interactive sign-in (browser auth-code+PKCE)        -> AAD access token (aud = Skype/Teams resource)
[2] POST authsvc/v1.0/authz  (Bearer AAD token)              -> Skype token  (X-Skypetoken)
[3] POST go.trouter.../v4/a?epid=<endpoint>  (x-skypetoken)  -> {socketio, surl, connectparams}
[4] GET  {socketio}socket.io/1/?v=v4&<connectparams>...      -> "<sessionId>:<hb>:<to>:<transports>"
[5] WS   {socketio}socket.io/1/websocket/<sessionId>?...     -> persistent socket (header X-Skypetoken)
[6] on "1::"  -> send user.authenticate (Bearer AAD token) + user.activity + register surl
[7] POST teams.microsoft.com/registrar/prod/V2/registrations (X-Skypetoken + Bearer)  -> binds surl
[8] inbound "3:::" pseudo-HTTP requests arrive; url endswith /messaging => a chat message
```

Two tokens travel together for the whole session:
- **AAD access token** → used as `Authorization: Bearer ...` (websocket `user.authenticate`, registrar).
- **Skype token** → used as `X-Skypetoken: ...` (trouter info GET, websocket connect header, registrar).

---

## 2. Authentication (steps 1–2)

### 2.1 Acquire the AAD token (MSAL)
Use MSAL (`msal` for Python). Interactive auth-code+PKCE opens a browser, which is what
purple-teams does ("Authentication is done through the browser"). Device-code is a fallback but
**is frequently blocked by Conditional Access in corporate tenants** — don't make it the default.

- **client_id — VERIFY.** Must be a Microsoft first-party Teams client app ID. Your own app
  registration generally cannot obtain tokens for the Skype/Teams first-party resource. Capture
  the real one from a live `teams.microsoft.com` login: DevTools → Network → the request to
  `login.microsoftonline.com/.../oauth2/v2.0/authorize` — read `client_id` and `scope` from the
  query string. (Work tenant = TFW path; personal = TFL path, different values.)
- **scope / resource — VERIFY.** Target audience is the Skype/Teams chat resource
  (`https://api.spaces.skype.com/...` or `chatsvcagg.teams.microsoft.com`). Take the exact
  scope string from the same capture.
- **authority:** `https://login.microsoftonline.com/<your-tenant-id>` (work) — work tenant.

### 2.2 Exchange for the Skype token
`POST` the AAD bearer token to the authz endpoint; response carries the skype token (and region
metadata).

| Account type | authz endpoint |
|---|---|
| Work/school (TFW) | `https://teams.microsoft.com/api/authsvc/v1.0/authz` |
| Personal (TFL) | `https://teams.live.com/api/auth/v1.0/authz/consumer` |

Send `Authorization: Bearer <AAD token>`. Parse the skype token out of the JSON response
(`tokens.skypeToken` shape in current responses — **VERIFY** the exact JSON path against your
capture). Also generate and persist a stable **endpoint GUID** (`epid`) now; reuse it for both
the trouter connect and the registrar.

---

## 3. Trouter connection (steps 3–5)

### 3.1 Info call
```
POST https://go.trouter.teams.microsoft.com/v4/a?epid=<endpoint>
Header: x-skypetoken: <skype token>
Header: Content-Length: 0
```
Response JSON:
```json
{
  "socketio": "https://trouter2-<...>.trouter.teams.microsoft.com:443/",
  "surl":     "https://trouter2-<...>.trouter.teams.microsoft.com:3443/v4/f/<id>/",
  "connectparams": { "sr":"...", "issuer":"prod-2", "sp":"connect", "se":"...", "st":"...", "sig":"..." }
}
```
Keep `surl` (you register it in §4) and `connectparams` (they go into the URLs below).

### 3.2 Session handshake
```
GET {socketio}socket.io/1/?v=v4&<each connectparam k=v>&tc=<...>&con_num=<n>_1&epid=<endpoint>[&ccid=<ccid>]&auth=true&timeout=40&
Header: X-Skypetoken: <skype token>
```
`tc` is a URL-encoded JSON blob: `{"cv":"<TCCV>","ua":"TeamsCDL","hr":"","v":"<clientinfo version>"}`.
The reference pins `TCCV = 2024.23.01.2` — **VERIFY/refresh** the version strings against a
current capture; stale versions can be rejected.

Response is **plaintext**: `"<sessionId>:<heartbeat>:<timeout>:websocket,xhr-polling"`.
Take the substring before the first `:` as `sessionId`.

### 3.3 Open the websocket
```
WS {socketio}socket.io/1/websocket/<sessionId>?v=v4&<connectparams>&tc=<...>&con_num=<n>_1&epid=<endpoint>[&ccid=<ccid>]&auth=true&timeout=40&
Extra header: X-Skypetoken: <skype token>
Extra header: User-Agent: <Teams UA>
```
Re-open on close/error. Re-register on TTL (reference uses `86400 - 10s`).

---

## 4. Post-connect choreography & registration (steps 6–7)

**Framing is Socket.IO 0.9-era** (the `N:::` message types). On the open socket:

Inbound frames:
- `1::` → connected. Trigger: send `user.authenticate`, send `user.activity`, then register.
- `3:::<json>` → a delivered event as a pseudo-HTTP request (this is where messages arrive).
  You **must** reply `3:::{"id":<same id>,"status":200,"body":""}`.
- `5:<n>::<json>` / `5:<n>+::<json>` → named server events (e.g. `trouter.message_loss` →
  re-register the messaging worker).
- `6:<n>+::` → ack to something you sent (noop).

Outbound frames you send:
- ephemeral: `5:::<json>` (used for `user.authenticate`).
- regular: `5:<count>+::<json>` (used for ping; increment `count`).

**`user.authenticate`** (sent as `5:::`):
```json
{"name":"user.authenticate","args":[{
  "headers":{"X-Ms-Test-User":"False","Authorization":"Bearer <AAD token>","X-MS-Migration":"True"},
  "connectparams": <connectparams from §3.1>
}]}
```

**`user.activity`**: `{"name":"user.activity","args":[{"state":"active","cv":"<cv>.0.1"}]}`

**Keepalive ping** every ~30s (as `5:<n>+::`): `{"name":"ping"}`

**Registration** — bind `surl` so the backend routes events to your socket:
```
POST https://teams.microsoft.com/registrar/prod/V2/registrations
Headers: X-Skypetoken: <skype token>
         Authorization: Bearer <AAD token>
         Content-Type: application/json
Body:
{
  "clientDescription": {
    "appId": "TeamsCDLWebWorker",
    "aesKey": "",
    "languageId": "en-US",
    "platform": "edge",
    "templateKey": "TeamsCDLWebWorker_2.1",
    "platformUIVersion": "<clientinfo version>"
  },
  "registrationId": "<endpoint GUID>",
  "nodeId": "",
  "transports": { "TROUTER": [ { "context": "", "path": "<surl>", "ttl": 86400 } ] }
}
```
- **`TeamsCDLWebWorker` is the messaging worker** — for an MVP that's the only registration you
  need. Path = `surl` with no suffix.
- (Optional, for calls/presence later, register `NextGenCalling` at `surl+"NGCallManagerWin"`
  and `SkypeSpacesWeb` at `surl+"SkypeSpacesWeb"`; register `TeamsCDLWebWorker` **last**.)
- `templateKey`/`platformUIVersion` are version-pinned — **VERIFY** against a current capture.

---

## 5. Extracting messages (step 8)

Each inbound `3:::` frame, after the 3rd `:`, is JSON:
```json
{"id":<int>,"method":"POST","url":".../messaging","headers":{...},"body":"<stringified json>"}
```
Routing by `url` suffix:
- endswith `/messaging` → **chat message**. Parse `body` → object; if `type == "EventMessage"`,
  it's the one to print.
- endswith `/TeamsUnifiedPresenceService` or `/unifiedPresenceService` → presence (ignore for MVP).
- endswith `/NGCallManagerWin` or contains `/SkypeSpacesWeb` → call signaling (ignore for MVP).

**Body decoding gotchas (handle all three):**
- If header `X-Microsoft-Skype-Content-Encoding: gzip` → `body` is base64 → gunzip before parsing.
- If the parsed body has a `cp` member → it's base64 → gunzip → that's the real payload.
- If it has a `gp` member → it's base64 → parse directly.

For the dump, after decode pull the message resource (sender MRI like `8:orgid:<guid>`,
conversation/thread id, content, composetime). Strip the `8:orgid:` prefix for display. Always
send the `3:::{id,status:200,body:""}` ack regardless of whether you printed anything.

---

## 6. Recommended stack

Python (your primary), async:
- `msal` — interactive auth-code+PKCE (`acquire_token_interactive`) / device-code fallback.
- `httpx` — info call, authz exchange, registrar POST.
- `websockets` or `aiohttp` — the socket (must support custom request headers on connect;
  `aiohttp.ws_connect(..., headers=...)` is convenient for the `X-Skypetoken` header).
- stdlib `gzip`, `base64`, `json`, `uuid`.

Go is viable too (you'd reuse patterns from `microsoft/azure-sdk`/`AzureAD/microsoft-authentication-library-for-go`),
but Python gets the POC standing fastest given the JSON-wrangling and quick iteration on the
VERIFY values.

---

## 7. Endpoint reference (work/TFW)

| Purpose | Method | URL |
|---|---|---|
| AAD token | — | MSAL against `login.microsoftonline.com/<tenant>` |
| Skype token | POST | `https://teams.microsoft.com/api/authsvc/v1.0/authz` |
| Trouter info | POST | `https://go.trouter.teams.microsoft.com/v4/a?epid=<endpoint>` |
| Session handshake | GET | `{socketio}socket.io/1/?v=v4&...` |
| Websocket | WS | `{socketio}socket.io/1/websocket/<sessionId>?v=v4&...` |
| Registrar | POST | `https://teams.microsoft.com/registrar/prod/V2/registrations` |

Headers cheat-sheet: trouter info/handshake/ws → `X-Skypetoken`; websocket `user.authenticate`
+ registrar → also `Authorization: Bearer <AAD token>`.

---

## 8. Milestones

- **M0 — Auth:** print a valid AAD token and a valid skype token. (Proves client_id/scope/authz path.)
- **M1 — Handshake:** info call returns `surl`+`connectparams`; session handshake returns a `sessionId`.
- **M2 — Socket up:** websocket opens, you see `1::`, auth+activity+register succeed (registrar 200),
  ping keeps it alive >2 min without drop.
- **M3 — Dump:** send yourself a Teams message from the official client; the CLI prints it within ~1s.
- **M4 — Robustness:** auto-reconnect on close/error; re-register on `trouter.message_loss` and on
  TTL; re-mint skype token (and AAD token) when 401/expired; survive a laptop sleep/wake.

---

## 9. Risk register / known sharp edges

- **client_id / scope (highest risk).** Wrong audience = no skype token. Get them from a live
  capture, per-tenant. Device-code may be CA-blocked → use browser auth-code.
- **Version-string drift.** `TCCV`, `templateKey`, `platformUIVersion`, User-Agent are pinned in
  the reference and go stale; refresh from capture if handshakes/registrations start 4xx-ing.
- **Token lifetimes.** AAD token ~1h; skype token longer (reference re-registers on a 24h TTL).
  Treat both as refreshable; on authz/registrar 401, re-mint and re-register.
- **Socket.IO 0.9 framing.** Not modern socket.io — don't reach for a socket.io client lib;
  speak the raw `N:::` framing yourself.
- **Body encoding.** gzip+base64 and nested `cp`/`gp` — handle all paths or you'll silently drop messages.
- **Must ack `3:::`.** Failing to reply `status:200` can cause redelivery/disconnects.
- **Single-user only.** Don't fan this out; it's your account, your blast radius.

---

## 10. Source references (read these before coding)

- `EionRobb/purple-teams` — `teams_trouter.c` (the entire §3–§5 flow, verbatim), `teams_login.c`
  (authz endpoints), `teams_connection.c` (per-host token usage), `teams_messages.c` (message parsing).
- `Gerenios/AADInternals` — `Teams_utils.ps1` (skype token via `Authentication: skypetoken=...`, PowerShell-readable).
- Microsoft Learn — delta query for `chats/getAllMessages` (history backfill, if M-future).

The reference code is GPLv3 — if you redistribute, mind license compatibility; for a private
personal tool it's a non-issue.
