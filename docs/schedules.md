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
their main chat as an automated event from `Schedule: weekly-meal-plan`.
The model sees an `[Event from Schedule: weekly-meal-plan: automated
notification, not a message from the user. …]` header, not a new message or
approval from the chat's owner. It still starts a turn and the Telegram
plugin passes the reply on for a main chat, so the person sees it on their phone.

## Cron

Five fields: minute, hour, day of month, month, day of week. Each takes
`*`, numbers, `a-b` ranges, `*/n` and `a-b/n` steps, comma lists, and names
(`mon`, `jan`). Day of week 0 and 7 are Sunday. When both day fields are
set, either may match, as in cron.

| Cron              | Runs                                    |
| ----------------- | --------------------------------------- |
| `0 8 * * *`       | 8:00 every day                          |
| `0 8 * * mon-fri` | 8:00 on weekdays                        |
| `*/15 9-17 * * *` | every 15 minutes during the working day |
| `0 0,12 * * *`    | midnight and noon                       |
| `0 9 1 * *`       | 9:00 on the first of the month          |

Times are read in the person's `timezone`, else the bot's `timezone`, else
Django's `TIME_ZONE`.

Set `timezone: UTC` (or another IANA timezone) on a schedule to use a fixed
timezone instead of each person's timezone.

## Who it runs for

```yaml
users: [lee] # default: permissions.users, else everyone with a chat
enabled: true # false pauses it
```

A schedule runs once per person. A schedule with only `run` steps (below)
and no `users` runs once in total, as the first admin.

## Where the message goes

```yaml
to: main # default
to: reports # a named chat
to: { thread: "Weekly report {n}" } # a new thread under main
to: { thread: "Stats {date:%b %d}", in: reports } # a new thread under a named chat
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
      - run: tools/meta_ads.py:pull_yesterday # a function in the bot folder
        args: { account: main }
      - prompt: "New ad numbers: {result}. Flag anything unusual."
        to: { thread: "Ads {date:%b %d}" }
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

## Workflows

Use `workflow` instead of `actions` for deterministic checks, named results,
and conditional prompts. Existing `message` and `actions` schedules still
work unchanged.

```yaml
schedules:
  - name: weekly-model-discovery
    cron: "0 9 * * sun"
    timezone: UTC
    users: [lee]
    workflow:
      - python: ergo.models.check_available
        save_as: models
      - if: models.notify
      - prompt: |
          Review this model catalog diff: {models}
          Propose catalog/config updates as a PR. Do not enable models or
          change routing automatically; unknown pricing needs verification.
        to: { thread: "New models available {date:%Y-%m-%d}" }
```

Steps run in order:

- `python: ergo.models.check_available` selects the registered built-in check.
  `python: tools/checks.py:check` calls a trusted bot-folder function via the
  same loader as bot tools, not an arbitrary installed Python import.
  It receives a context dictionary first, followed by optional `args` keyword
  arguments. The context contains `bot`, `user`, `run`, `result` (the previous
  Python result), and results named by earlier `save_as` steps. Return JSON
  data, not credentials or provider response objects.
- `save_as: models` names a Python result for later steps. `{models}` in a
  prompt inserts its JSON representation; `{result}` uses the previous result.
- `if: models.notify` continues only when the expression is truthy. A false
  condition ends the run successfully without creating a chat/thread, sending
  a message, or running an LLM turn. Conditions support JSON values, names,
  dictionary field/index lookups, `and`, `or`, `not`, and comparisons; they
  cannot call functions or execute arbitrary Python.
- `prompt` uses the existing targets: main chat, named chat, or a new thread
  with a date/title template. By default it queues an LLM turn. `llm: false`
  writes the message without invoking a model.

Bot repositories are trusted executable configuration. Python steps have the
same process permissions as bot tools; the condition evaluator is not a
sandbox for untrusted bot Python. Keep secrets in-process, never in returned
results, prompt templates, or step arguments.

### Model availability

`ergo.models.check_available` reads catalogs only; it never performs inference,
enables a model, or modifies providers, routing, or a bot's model table.
It persists the last successful catalog snapshot in the database and returns
`new`, `removed`, `changed`, `flagged`, `notify`, and provider `coverage`.
Repeated checks of an unchanged catalog produce empty diffs and
`notify: false`. A provider fetch failure fails the step rather than treating
the provider's entire catalog as removed.
The complete report is validated against workflow JSON limits before the
snapshot commits (10,000 values, depth 32, 1 MB encoded JSON). Flags contain a
`selector`, `reason`, and `severity`, referring to the full model record in
the diff instead of copying its metadata for every warning. If a report
exceeds a limit, the check fails visibly and retains its prior snapshot;
the discovery diff is not consumed.

Each discovered model carries policy and metadata flags, including
`allowed_for_coding`, `priced`, and `private`. Coding candidates are limited
to `anthropic`, `openai`, `openai-codex`, and `xai-oauth`. Other providers,
including Fireworks, are quarantined for coding; private pool models are
never shared/general-routing candidates. Unknown pricing stays unknown.
Removal of a model used by configured routing is flagged for review.

List endpoints often omit prices. Catalog metadata and its provenance are
reported when available; check the provider's official pricing before
recommending a configuration change. Discovery is a review trigger, not an
automatic routing decision.

Sources are discovered from the bot registry and the host's configured omp
providers. The omp model cache is opened read-only; saved OAuth credential
payloads are never read or refreshed. Cache membership is not proof that a
subscription will accept a model, and cache prices may be stale.

| Provider        | Non-inference source                                                                                                                          | Pricing                                                                                                                       |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Anthropic       | CLI subscription: local omp cache. API: paginated [`GET /v1/models`](https://platform.claude.com/docs/en/api/models/list) with configured key | Cache metadata, with [official pricing](https://platform.claude.com/docs/en/about-claude/pricing) linked for review           |
| OpenAI          | API `GET /v1/models`; local cache enriches matching IDs                                                                                       | Cache metadata; [official pricing](https://openai.com/api/pricing/) is not included in the list response                      |
| OpenAI Codex    | Local omp subscription catalog; no inference or saved OAuth token use                                                                         | Catalog prices, not a claim about subscription billing                                                                        |
| xAI / xAI OAuth | API [`GET /v1/models`](https://docs.x.ai/developers/rest-api-reference/inference/models) / local OAuth catalog                                | API token prices normalized to USD/million; cache prices retain catalog provenance                                            |
| Fireworks       | Local catalog; paginated [account model list](https://docs.fireworks.ai/api-reference/list-models) when key and `FIREWORKS_ACCOUNT_ID` exist  | Catalog metadata; [official pricing](https://fireworks.ai/pricing) requires review. Always private/non-routable here          |
| Google          | Local catalog; paginated [`GET /v1beta/models`](https://ai.google.dev/api/models) with `GEMINI_API_KEY` or `GOOGLE_API_KEY`                   | List response has capabilities, not prices; [official pricing](https://ai.google.dev/gemini-api/docs/pricing) requires review |

Missing credentials/catalogs, unsupported providers, and custom endpoints
appear explicitly in `coverage`; their prior snapshot is retained, not
interpreted as an empty catalog. Subscription sources never substitute an
API account's membership for the subscription's catalog. Official requests
use fixed HTTPS list endpoints, never redirects or billable inference.
The first successful check reports all discovered models as new.

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

Every run records its status, timestamps, step results, and sanitized errors
on `ScheduleRun`. A failed step stops later steps and posts a failure notice
to the bot's main chat without an LLM turn. Nothing-to-report success stays
silent. Scheduled LLM prompts are queued events: the run records enqueueing,
not completion of the later LLM turn.
