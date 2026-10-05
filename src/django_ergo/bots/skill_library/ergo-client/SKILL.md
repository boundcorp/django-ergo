---
name: ergo-client
description: Use an Ergonaut server's bots from outside it (Claude Code, Codex, a script or another server's bot). List bots, start threads, send messages and wait for replies, follow workers and PRs, and answer approvals. Use it whenever work should be delegated to an Ergo bot or you need to check what a bot is doing.
install: [claude, codex]
---
# Ergo client

Ergonaut hosts Ergo bots. Each person has a **main chat** with each bot, and
can also have **named chats** and **threads**, which are child chats for one
piece of work. A turn can call tools, start **workers** (background jobs such
as an Orca coding agent), message other bots' threads, and stop to wait for
someone to **approve** a risky tool call. Everything you can do in the web
app, you can also do through its API with an API key.

## Connecting

You need the server's URL and an API key (`ergo_...`). The person makes the
key in the web app (**API keys**, at the bottom of the sidebar), or an admin
runs `ergonaut manage api_key create <user> --name <where>` on the server. A
key acts as that person, with their access. Never print a key, commit it or
put it in a prompt.

```bash
ergo login https://ergo.example.com --name prod     # asks for the key; saved in ~/.config/ergo/client.json (0600)
ergo whoami                                         # who you are and the server's version
```

`ergo` is `scripts/ergo.py` in this skill's folder. If the installer put it
on your PATH, run `ergo`. Otherwise run `python3 <this skill's folder>/scripts/ergo.py`.
It needs only Python 3.10+. `ERGO_URL` with `ERGO_API_KEY`, or
`ERGO_SERVER=<name>`, override the saved default. `--server NAME` picks a
server for one command.

When you are an Ergo bot using this skill, the `ergo_client_*` tools run the
same commands against `ERGO_URL` with `ERGO_API_KEY`, which are secrets set
on your server.

## Finding your way

```bash
ergo bots                            # bots you can use, their chats, whether they take threads
ergo bot devbox                      # its skills and tools, schedules, tables, model
ergo threads --bucket waiting        # chats waiting on you (approval, question, failure)
ergo threads --bot devbox --limit 10 # newest first, each with its status line, workers and PRs
ergo show <session>                  # transcript (tool calls clipped), workers, requests, PRs, approvals
ergo show <session> --full --limit 100
```

Wherever a command takes a session, you can give a full id, a unique id
prefix of 6 or more characters, `BOT` for your main chat with it, or
`BOT:CHAT` for a named chat.

Thread buckets mean the same as in the sidebar:

- `waiting`: needs the person.
- `working`: a turn or worker is running.
- `review`: idle with an open PR.
- `idle`: nothing running and nothing open.
- `resolved`: archived.

## Delegating work

Start a **thread** for each self-contained task. Use the **main chat** for
quick questions and for asking an orchestrating bot to route work.

```bash
ergo new devbox "Fix the flaky upload test in django-ergo; open a PR" --title "Flaky upload test"
ergo new devbox "..." --wait                  # block until the reply (default limit 900s)
ergo send <session> "Also add a regression test" --wait
ergo wait <session>                           # wait for whatever runs now, then print the reply
```

- A message sent while a turn runs steers that turn. `--interrupt` stops
  the running turn first, so your message starts a new one.
- Write the task the way you would brief a colleague. Say what done means
  (a PR, a report, an answer), which repo and branch to use, and any
  constraint. The bot can't see your conversation.
- Long work (an Orca worker writing code) keeps going after the reply. The
  thread shows `workers:1/1`, and the bot gets a new message when the worker
  finishes. Check back with `ergo show` or `ergo threads` rather than
  holding a `--wait` open for hours.
- `ergo worker-log <session> <worker-id>` shows a worker's recent output
  when you need to see why it stalled.
- Exit code 3 from `--wait` means it is still working. That is not a
  failure; check later.

## Approvals, failures and control

```bash
ergo approve <session> --wait     # run the tool calls the turn is waiting on
ergo approve <session> --deny
ergo resume <session>             # retry a turn that failed or hit its step limit
ergo stop <session>               # stop the running turn
ergo close <session>              # archive a finished thread
ergo models devbox                # models the bot's chats can use
ergo models --session <session> --set openai/gpt-6-sol
```

Approve only what the person asked for or would clearly want. Tool calls
that spend money, message people, merge, deploy or delete need the
person's own say-so. When in doubt, show them the pending call (`ergo show`)
and ask.

## Anything else

`ergo api METHOD PATH [JSON]` calls any endpoint, for example
`ergo api GET /bots/devbox/tree` or `ergo api GET /calls/<call-id>` for a
turn's full system prompt and transcript. Add `--json` to any command for
the raw API output. The schema browser is at `<server>/api/docs` for staff
users.
