---
name: ergo-developer
description: How to develop Ergo itself (the django-ergo library, Ergonaut and the bots) by using the platform. Delegate the work to Ergo bots through the client, watch how the bots handle it, then improve the bot, the library or Ergonaut, whichever layer fell short. Use it for any feature, fix or review in django-ergo or a bot repo.
install: [claude, codex]
---
# Developing Ergo through Ergo

The goal is that Ergo builds Ergo. Most feature work should run **through
the platform**: you hand the task to a bot with the `ergo-client` skill,
the bot does it (often with a coding worker), and you watch. Each task is
then two results:

1. **The feature or fix itself.**
2. **What the run taught us.** Where the bot was slow, confused, blocked or
   wasteful, improve the right layer so the next run goes better.

Writing code yourself is still allowed: for a small fix, when the platform
is down, or when the fix is to the platform itself. Say which path you took
and why.

## The layers, and where a fix belongs

| What went wrong | Layer | Where |
| --- | --- | --- |
| The bot lacked a procedure, a tool, context or permissions, or its prompt misled it | The bot | Its folder in the bot config repo (`ergo-bot-development`, or ask the bot to change itself with `skillbuilder`) |
| Every bot would hit it: tool loading, compaction, context, structured calls, workers, plugins, the skill library | The Ergo library | `django-ergo/src/django_ergo/` |
| Hosting, API, web app, Celery, upgrades, costs, auth | Ergonaut | `django-ergo/ergonaut/` |
| Instructions for agents, these skills included | The skill library | `django-ergo/src/django_ergo/bots/skill_library/` |

Fix it in the narrowest layer that solves it for everyone who would hit it.
A one-off prompt tweak to cover a library bug hides the bug.

## The loop

1. **Delegate.** `ergonaut-remote new <bot> "<task>" --title "<short name>"`. Write a
   brief that stands alone: the repo, the goal, what done means (usually a
   PR), and the constraints. For code work, pick the bot that runs coding
   workers. `ergonaut-remote bots` and `ergonaut-remote bot <name>` show what each one can do.
2. **Watch, without hovering.** Use `ergonaut-remote threads --bot <bot>`,
   `ergonaut-remote show <session>`, and `ergonaut-remote worker-log` for a stalled worker. Answer
   questions and approvals as the person would want, and escalate anything
   only they can decide. Don't hold a `--wait` open for hours; check back.
3. **Review the output** the way a reviewer would: the PR diff, its CI and
   its tests. Send corrections to the same thread with `ergonaut-remote send`, so the
   bot learns in context.
4. **Review the run.** Read the transcript (`ergonaut-remote show --full`, and
   `ergonaut-remote api GET /calls/<id>` for the exact prompt). Look at the Costs page
   for tokens. Note each place where the bot:
   - didn't find a tool, or loaded the wrong skill
   - repeated work, or had a context gap or a compaction loss
   - got a tool error, or stalled on an approval
   - needed a human for something it could have done
5. **Improve.** Open a focused PR for each improvement in the right layer,
   and link the thread that showed the problem. Then run the same kind of
   task again to confirm it helped.

## Working in django-ergo

- Read `CLAUDE.md` and `docs/development.md` first. Tests: `make pytest`
  (embedded Postgres), and `cd ergonaut && make test`. Run ruff, and
  prettier for the frontend; every pre-commit hook must pass.
- Docs change in the same PR: `docs/bots.md` and the matching guide for
  bot.yaml keys, plugins, tools or Ergonaut behavior. Keep `TODO.md` current
  as items land.
- **django-ergo is public.** Keep anything about a specific deployment
  (hosts, clusters, private repos, people, keys) out of it. That belongs
  in the deployment's own private repo or the bot config repo.
- A server may follow `main` automatically (`ERGONAUT_UPGRADE_CHANNEL=branch:main`),
  so treat a merge as a deploy. Only the person merges, unless they said
  otherwise for that PR. A migration that rewrites data needs a backup
  first.
- Bot configs live in their own repo, not in django-ergo. `examples/`
  holds public examples only.

## Setting up a machine for this

On your dev box, install these skills for Claude Code and Codex from a
django-ergo checkout:

```bash
python3 src/django_ergo/bots/skill_library/install.py --bin ~/.local/bin
ergonaut-remote login <server-url>          # with an API key from that server
```

Symlinks keep the skills current with `git pull`. Then start delegating:
`ergonaut-remote bots`, then `ergonaut-remote new <bot> "..."`.
