# Teams scheduled send on the wire

Read by `miniteams mcp` for its scheduling tools (spec 008). State on 2026-09-30. Names, ids and
message text are left out.

## Status

| Call | State |
|---|---|
| List, regional host, skype token | verified |
| List, proxy host, ic3 bearer | verified |
| Silent ic3 token from the miniteams cache | verified |
| Create | verified on Notes |
| Get one | verified |
| Edit text | verified |
| Move time | verified |
| Cancel | verified |
| Group chat or channel as target | not run |
| A draft made here in a 1:1 chat shows in the Teams client | verified |
| A draft made here in a group chat shows in the Teams client | not checked |
| A draft made here in Notes shows in the Teams client | no, nothing shown |
| A draft made here can be deleted from the Teams client | verified |
| Drawn as scheduled with its send time | not confirmed |

## Hosts and auth

| Host | Auth | Reads | Writes |
|---|---|---|---|
| `https://<region>.ng.msg.teams.microsoft.com` (`settings.contacts_host`) | `X-Skypetoken` | 200 | fail (404 or 500 seen here) |
| `https://teams.cloud.microsoft/api/chatsvc/<region>` | `Authorization: Bearer <ic3>` | 200 | 201 / 200 |

- The ic3 token has audience `https://ic3.teams.office.com`, scope `Teams.AccessAsUser.All`.
  MSAL gives it silently for `https://ic3.teams.office.com/.default` on the Teams client (FOCI),
  the same way `TokenSource.graph_token()` gets its token.
- `<region>` is the `region` field of the authz exchange (`skype.exchange_skype_token`). `emea`
  answered the list call as well.
- Headers on the proxy: `Authorization`, `BehaviorOverride: redirectAs404`,
  `x-ms-migration: True`, `Accept: application/json`.

## List (verified)

`GET /v1/users/ME/drafts?pageSize=200` answers `{"_metadata": {...}, "drafts": [...]}`.
`_metadata.syncState` points at `/v1/users/ME/conversations/48:drafts/messages`, so drafts are the
messages of a pseudo conversation `48:drafts`.

One draft, as listed:

```json
{
  "id": "1700000000500",
  "version": "1700000000500",
  "clientmessageid": "1700000000000",
  "sequenceId": 1,
  "type": "Message",
  "conversationid": "48:drafts",
  "draftType": "ScheduledDraft",
  "innerThreadId": "19:<uuid>_<uuid>@unq.gbl.spaces",
  "draftDetails": {"sendAt": "1700035200000", "shouldCloneAmsReferences": true},
  "messagetype": "RichText/Html",
  "contenttype": "Text",
  "content": "<html body>",
  "composetime": "2023-11-14T22:13:20.5000000Z",
  "properties": {"mentions": "[]", "skipfanouttobots": false, "languageStamp": "..."}
}
```

| Field | Meaning |
|---|---|
| `innerThreadId` | target chat. `<thread>;messageid=<id>` for a reply inside a channel thread |
| `draftDetails.sendAt` | send time, epoch milliseconds, as a string |
| `draftType` | `ScheduledDraft`. `RegularDraft` is a draft with no send time |
| `id` | draft id, its creation time in ms |

State of a draft:

| State | Test |
|---|---|
| cancelled | `content` empty. The row stays for good, with `properties.deletetime` and `skypeeditedid` |
| pending | `sendAt` in the future |
| sent | `sendAt` in the past. Teams keeps the row, for years |

Creating a draft bumps the target chat: its `properties.draftVersion` takes the draft id.

## Write calls (run on 2026-09-30)

| Call | Request | Answer |
|---|---|---|
| Create | `POST /v1/users/ME/drafts` | 201 `{"OriginalArrivalTime": <draft id>}` |
| Get one | `GET /v1/users/ME/drafts/<id>` | 200 with the draft. An id that was never a draft: 403 `MessageIdNotInAllowedRange` |
| Edit or move | `PUT /v1/users/ME/drafts/<id>` with the whole payload, built from the stored draft | 200, empty body. The draft gains `properties.edittime` |
| Cancel | `DELETE /v1/users/ME/drafts/<id>` | 200 `null` |
| Cancel a cancelled draft | same | 200 `null` again, so the answer does not tell the two apart |
| PUT on a cancelled draft | same as edit | 400 `Cannot set deletetime in editing message` |

Payload of create and of PUT. The message sits under `message`, and the draft fields are repeated
at the top level. `sendAt` is epoch ms outside and ISO inside.

```json
{
  "draftDetails": {"sendAt": "1700035200000"},
  "draftType": "ScheduledDraft",
  "innerThreadId": "<thread>",
  "message": {
    "id": "-1",
    "type": "Message",
    "conversationid": "<thread>",
    "conversationLink": "blah/<thread>",
    "draftDetails": {"sendAt": "2023-11-15T08:00:00.000Z"},
    "threadtype": "streamofdrafts",
    "innerThreadId": "<thread>",
    "from": "8:orgid:<uuid>",
    "fromUserId": "8:orgid:<uuid>",
    "composetime": "<now, ISO>",
    "originalarrivaltime": "<now, ISO>",
    "content": "<body>",
    "messagetype": "Text",
    "contenttype": "Text",
    "imdisplayname": "<name>",
    "clientmessageid": "<client id>",
    "callId": "",
    "state": 0,
    "version": "0",
    "amsreferences": [],
    "properties": {
      "importance": "",
      "subject": "",
      "title": "",
      "cards": "[]",
      "links": "[]",
      "mentions": "[]",
      "onbehalfof": null,
      "files": "[]",
      "policyViolation": null,
      "formatVariant": "TEAMS",
      "draftId": "<client id>"
    },
    "crossPostChannels": []
  }
}
```

On a PUT, `message.id` is the draft id instead of `-1`.

## Traps

- **Partial property bag (not checked here).** The service rejects a `properties` object that lacks keys. Send the
  whole set.
- **`properties.draftId` must equal `clientmessageid` (not checked here).** When they differ, the clients draw the
  draft as a message already sent, dated now, with no send time.
- **`clientmessageid` is fixed at creation (not checked here).** An edit keeps it and sets `properties.draftId` back
  to it. Editing a draft in the Teams client knocks the two apart.
- **A client id used by a cancelled draft is burnt (not checked here).** A later draft that reuses it is stored and
  will send, but no client shows it, so it cannot be cancelled by hand. Check the ids in the
  listing (`clientmessageid`, `skypeeditedid`, `properties.draftId`) before picking one.
- **Bounds on `sendAt` (verified 2026-09-30).** The service refuses with 400 and names the
  bound: `SendAt is too early, should be at least 5 seconds in the future` and
  `SendAt is too late, should be at most 125 days in the future`. A draft just under 125 days
  was accepted, then cancelled through the API.
- **Reach of the Teams client (verified 2026-09-30, babs).** Its date picker stops 120 days
  ahead. A draft scheduled through the API 122 days ahead shows in the chat and opens for
  editing, but the picker offers nothing later than day 120, so saving the edit pulls it back. A
  draft 118 days ahead can be moved up to day 120. Both were deleted from the client, the 122-day
  one included, and came back from the API as tombstones.
- **Cancel leaves a tombstone.** There is no call that removes the row.

## Notes to self as a target

Run through the proxy host with the payload above, `sendAt` two days ahead.

| `innerThreadId` | Answer |
|---|---|
| `48:notes` | 404 `ThreadNotFound` |
| `19:teamsstream_notes_<uuid>@thread.v2` | 201, draft created |

The alias is refused and the thread behind it is accepted. The thread id is
`threadProperties.originalThreadId` of `GET /v1/users/ME/conversations/48:notes`.

The test draft was created, read back, edited, moved by 3 hours and cancelled. It left one
tombstone. The Teams client offers no scheduling in Notes, and whether it shows a draft made this
way was not checked.

The client showed nothing for that draft while it was pending (checked by babs, 2026-09-30).

## A 1:1 chat as a target

A draft created on 2026-09-30 in a 1:1 chat, `sendAt` seven days ahead: 201. The
chat's `properties.draftVersion` took the draft id and no message was delivered. The Teams client
shows it in the chat, and deleting it there turned the row into a tombstone (babs, 2026-09-30).

Earlier attempts on the regional host with the skype token and a flat body gave 404 and 500. Those
came from the wrong host and body, not from the target.

## Other clients

Searched 2026-09-30 for `draft`, `schedul` and `sendAt`:

| Project | Result |
|---|---|
| `EionRobb/purple-teams` | nothing. One unrelated comment about rescheduling a timer |
| `Terrance/SkPy` | nothing |
| `fossteams/teams-api`, `fossteams/fossteams-frontend` | hits only in a login-flow note and tenant/user models, none about messages. Files not opened |
| GitHub code search, all repositories, `ScheduledDraft`, `draftDetails sendAt`, `48:drafts`, `users/ME/drafts` | no Teams client among the hits |

## Endpoints that do not exist

404 on the regional host: `/conversations/<thread>/drafts`,
`/conversations/<thread>/draftmessages`, `/users/ME/draftmessages`,
`/conversations/48:drafts/messages` answers 400 `Invalid threadId, cannot be 48:drafts.`

404 also on `/conversations/<thread>/scheduledmessages`, `/users/ME/scheduledmessages`,
`/threads/<thread>/drafts`.
