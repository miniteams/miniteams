# `miniteams watch` from an agent

`watch` prints one JSON line per live Teams event that matches its criteria, and nothing else on
stdout. Claude Code's Monitor tool runs a command in the background and turns each stdout line
into an event in the session, so an agent arms a watch by starting the command under Monitor.

## Arm a watch

```
Monitor({
  command: "uv run --directory /path/to/miniteams miniteams watch --thread <chat id> --once",
  description: "an answer in <chat>",
  timeout_ms: 1800000,
})
```

| You want | Command |
|---|---|
| my messages | `miniteams watch --from me` |
| an answer after a given message | `miniteams watch --thread ID --after MSG --from others --once` |
| one special chat | `miniteams watch --thread ID` |
| a reaction from me | `miniteams watch --event reaction --from me [--reaction like]` |

Chat ids come from the MCP tool `find_chats` or `miniteams chats`. A watch needs `--thread` or
`--from me`; anything wider is refused at startup with exit code 2.

## Read the lines

```json
{"event": "message", "thread": "19:…", "chat": "Weekly", "sender": "…", "message_id": "1790…", "time": "2026-…Z", "text": "…", "reaction": "", "note": ""}
```

`text` is plain text cut at 1000 characters. For a reaction, `reaction` holds the emotion key and
`text` the message reacted to. `note` repeats `--note`, to remember why the watch was armed.

Three service lines share the stream, so silence never stands for success:

| `event` | Meaning | Then |
|---|---|---|
| `gap` | the stream reconnected or reported a loss: events may have been missed | keeps running |
| `suppressed` | more than 10 matches in a minute; `held_back` says how many | keeps running |
| `stopped` | the credential died (`reason`); sign in with `miniteams login` or the MCP `login` tool | exits 1 |

## Monitor's deadline

A Monitor watch ends after 30 minutes at most. Re-arm it with `--after <last message_id received>`
on a `--thread` watch: at startup the command reads the chat's history after that message, prints
what matches, then follows the stream, so nothing between the two arms is lost. A watch without
`--thread` has no catch-up.

## What never fires

- Edits, deletes, typing, calls and roster changes.
- A reaction that was already on the message when the watch started.
- A message the MCP server of this project sent (`send_message`): a `--from me` watch would
  otherwise wake the agent that wrote it. Messages typed in Teams or sent with `miniteams send`
  do fire.

## Injection

A watch on a chat brings other people's text into the session. The criteria choose who can do
it, not what they write; the guard is the permission prompt on the tools that write.
