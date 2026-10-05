# Agent skills

Ergo ships four skills for coding agents (Claude Code and Codex) and for
Ergo bots. They live in `src/django_ergo/bots/skill_library/`:

| Skill | For |
| --- | --- |
| `ergo-client` | Using an Ergonaut server's bots from outside: list bots, start threads, send messages and wait for replies, follow workers and PRs, answer approvals. Ships the `ergonaut-remote` command. |
| `ergo-hosting` | Setting up and running Ergonaut: a VPS devbox, install, providers and subscriptions, users and keys, testing, upgrades, backups, troubleshooting. |
| `ergo-bot-development` | Writing and testing bot folders in a bot repo. |
| `ergo-developer` | Developing Ergo through Ergo: delegate work to the bots, watch the runs, and improve the bot, the library or Ergonaut, whichever fell short. |

## Install for Claude Code and Codex

From a django-ergo checkout:

```bash
python3 src/django_ergo/bots/skill_library/install.py --bin ~/.local/bin
```

This links each skill into `~/.claude/skills` and `~/.codex/skills`. Those
follow `CLAUDE_CONFIG_DIR` and `CODEX_HOME` when set. It also links the
`ergonaut-remote` command into `~/.local/bin`. Because they are symlinks, `git pull`
updates them. Other options:

- `--target claude` or `--target codex`: install for one agent only.
- `--dest DIR`: install into another skills folder.
- `--copy`: copy the files instead of linking them.
- `--uninstall`: remove them.
- `--list`: show what would be installed.

A skill folder of the same name that the installer didn't make is left
alone.

Which skills get installed comes from each skill's front matter:
`install: [claude, codex]`. Ergo itself ignores the key.

## The ergonaut-remote command

`ergonaut-remote` (`ergo-client/scripts/ergonaut_remote.py`) is a standard-library Python client
for the [API](ergonaut.md#api-keys):

```bash
ergonaut-remote login https://ergo.example.com --name prod     # saves the URL and an API key in ~/.config/ergonaut/remote.json
ergonaut-remote bots
ergonaut-remote new devbox "Fix the flaky upload test; open a PR" --wait
ergonaut-remote threads --bucket waiting
ergonaut-remote show <session>
ergonaut-remote send <session> "Also add a regression test" --wait
ergonaut-remote approve <session>
ergonaut-remote api GET /bots/devbox/tree                      # any endpoint
```

A session can be given as an id, a 6+ character id prefix, `BOT` for your
main chat with it, or `BOT:CHAT`. `ERGONAUT_URL` with `ERGONAUT_API_KEY` override
the saved server, and `--json` prints the API's output. Where Ergonaut is
installed, `ergonaut remote ...` runs the same commands; `ergonaut` on its own
runs a server, while `ergonaut-remote` needs nothing but Python. `ergonaut-remote -h` lists
every command.

## In a bot

A bot gets any of these by naming it, like the rest of the library
(`skills: {include: [ergo-client]}`; see [Skills](skills.md#ergos-skill-library)).
`ergo-client` gives a bot the `ergo_client_*` tools. They talk to the
server named by the `ERGONAUT_URL` and `ERGONAUT_API_KEY` secrets, which is useful
when a bot drives bots on another server. The other three are instructions
only.
