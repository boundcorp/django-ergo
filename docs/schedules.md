# Schedules

A schedule makes a bot do something on its own: send a weekly plan, pull
data every morning, post a report into a fresh thread. Schedules live in
the bot's `bot.yaml`.

```yaml
schedules:
  - name: weekly-meal-plan
    cron: "0 17 * * sun"
    message: Propose next week's dinners with the meal-planning skill.
```

At 5pm every Sunday, in each person's timezone, the bot gets that message in
their main chat under a `[Scheduled message: weekly-meal-plan]` header and
answers it like any other. The Telegram plugin passes the reply on for a
main chat, so the person sees it on their phone.

## Cron

Five fields: minute, hour, day of month, month, day of week. Each takes
`*`, numbers, `a-b` ranges, `*/n` and `a-b/n` steps, comma lists, and names
(`mon`, `jan`). Day of week 0 and 7 are Sunday. When both day fields are
set, either may match, as in cron.

| Cron | Runs |
| --- | --- |
| `0 8 * * *` | 8:00 every day |
| `0 8 * * mon-fri` | 8:00 on weekdays |
| `*/15 9-17 * * *` | every 15 minutes during the working day |
| `0 0,12 * * *` | midnight and noon |
| `0 9 1 * *` | 9:00 on the first of the month |

Times are read in the person's `timezone`, else the bot's `timezone`, else
Django's `TIME_ZONE`.

## Who it runs for

```yaml
    users: [lee]       # default: permissions.users, else everyone with a chat
    enabled: true      # false pauses it
```

A schedule runs once per person. A schedule with only `run` steps (below)
and no `users` runs once in total, as the first admin.

## Where the message goes

```yaml
    to: main                                   # default
    to: reports                                # a named chat
    to: {thread: "Weekly report {n}"}          # a new thread under main
    to: {thread: "Stats {date:%b %d}", in: reports}   # a new thread under a named chat
```

Thread names are templates: `{n}` is the run number, `{date:%b %d}` the run
date with strftime codes. A new thread per run keeps each report
separate and the main chat quiet.

## Actions

For more than one message, list `actions`. They run in order, and a failing
step stops the rest:

```yaml
schedules:
  - name: daily-ads
    cron: "0 7 * * *"
    actions:
      - run: tools/meta_ads.py:pull_yesterday     # a function in the bot folder
        args: {account: main}
      - prompt: "New ad numbers: {result}. Flag anything unusual."
        to: {thread: "Ads {date:%b %d}"}
```

- `run: <file.py>:<function>` calls a function from a Python file in the
  bot folder, with `args` as keyword arguments. It may take a `ToolContext`
  first (`ctx.table(...)`, `ctx.secret(...)`). Each run is recorded as a
  `BotJob` with its status, result and any traceback, listed on the bot's
  page.
- `stop_if_empty: true` on a `run` step ends the run there, as a success,
  when the function returns nothing (`None`, `""`, `[]` or `{}`). An alert
  check returns its findings, and the prompt after it only goes out when
  there are some.
- `prompt:` sends a message to `to` (default `main`). `{result}` is the
  value the last `run` step returned.
- `message:` with `to:` at the top level is shorthand for one `prompt`.

`run` steps are how a bot keeps its tables fresh without spending model
calls: pull the data in code, then prompt only when there's something to
say, or never and let a pinned page show it.

```python
# tools/meta_ads.py
def pull_yesterday(ctx, account: str) -> dict:
    rows = fetch_insights(ctx.secret("META_TOKEN"), account)
    for row in rows:
        ctx.table("AdStat").objects.update_or_create(date=row.date, campaign=row.campaign,
                                                     defaults={"spend": row.spend})
    return {"rows": len(rows), "spend": sum(r.spend for r in rows)}
```

## How schedules run

`django_ergo.bots.schedules.run_due(bots)` checks every bot's schedules and
starts what's due. A `ScheduleRun` row per bot, schedule, person and minute
makes sure nothing runs twice, even with several beat processes. A prompt
waits for a busy chat like any other message.

Ergonaut's Celery beat calls `run_due` every minute and hands each run to a
worker, so schedules need beat and a broker (Redis). Outside Ergonaut, call
`run_due` from your own scheduler and set `DJANGO_ERGO["SCHEDULE_RUNNER"]`
to queue runs.

The bot's page in Ergonaut lists its schedules with their next run time,
and recent jobs with their results.
