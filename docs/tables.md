# Data tables

A bot can own structured data in real Django models, called **tables**.
Tools and schedules fill them, the bot queries and edits them, and live
pages chart them. Use a table when you'll filter, count, sum or chart the
data; use the [knowledge base](memory.md) for prose.

## Declaring tables

List the files that define them:

```yaml
# bot.yaml
tables: [tables.py]
```

```python
# tables.py
from django.db import models
from django_ergo.bots import BotTable

class House(BotTable):
    """Houses we've looked at for the property search."""

    address = models.CharField(max_length=200)
    price = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, default="new",
                              choices=[("new", "New"), ("visited", "Visited"), ("passed", "Passed")])
    notes = models.TextField(blank=True)
```

- Each bot is its own Django app, labelled `ergo_bot_<name>`; a model's
  table is `ergo_bot_<name>_<model>`. Two bots can both have a `House`.
- Every row gets `id`, `created_at` and `updated_at`.
- The docstring's first line describes the table to the bot. When it
  loads the `tables` skill it also sees each field's type, whether it's
  optional (`null=True`) and its `choices`.
- Any Django field type works. Validation runs on every add and update by
  the bot (`full_clean`).
- Everyone who uses the bot shares the rows; tables have no per-user
  ownership.

## Migrations

Schema changes are ordinary Django migrations kept in the bot folder's
`migrations/`, next to `tables.py`, so a schema change is reviewed with the
model change and data migrations have a place to live.

```bash
ergonaut manage ergo_bot_makemigrations path/to/bot           # write new migrations
ergonaut manage ergo_bot_makemigrations path/to/bot --check   # fail if one is missing (CI)
ergonaut manage ergo_bot_migrate path/to/bots                 # apply what's committed
```

Ergonaut runs `ergo_bot_migrate` when it starts and after pulling a bot
repo that changed. Only migrations in the folder are applied; nothing
alters tables on its own. When a bot proposes a table change through
`bot_management`, the plugin runs `ergo_bot_makemigrations` first, so the
proposal carries its migration.

Outside Ergonaut, `python -m django ergo_bot_makemigrations` and
`ergo_bot_migrate` work in any project with `django_ergo` installed.

## What the bot can do

A bot with tables gets the `tables` skill. Loading it shows each table's
fields, then offers:

| Tool | Does |
| --- | --- |
| `ergo_table_schema(table)` | field metadata, writable names, related tables and declared unique keys; default values are never returned |
| `ergo_table_query(table, filters, order_by, limit, count_only, fields, offset, exclude, predicate)` | validated lookups, projection and deterministic pages of at most 200 rows; returns total `count`, `rows`, `has_more`, `next_offset` |
| `ergo_table_aggregate(table, metrics, group_by, filters, exclude, predicate, limit)` | bounded count, sum, avg, min and max summaries |
| `ergo_table_batch(table, operations)` | atomic add, update and unique-key upsert of at most 100 operations |
| `ergo_table_add(table, values)` | add a validated row |
| `ergo_table_update(table, id, values)` | change fields of one row |
| `ergo_table_delete(table, id)` | delete one row, after approval |

Use `ergo_table_schema` for writable field names. Foreign keys to another
table of the same bot use the ID attname (for example `goal_id`); for a
`to_field` foreign key, supply the target field's value rather than its primary
key. Returned rows retain the relation name (`goal`) with that stored value.
Relation traversal is restricted to forward foreign keys to the same bot.
Reverse and many-to-many fields and cross-app relations are unavailable.
JSON field lookups can traverse keys and array indexes within the path limit.
Predicates are lookup objects combined using `{"and": [...]}`, `{"or": [...]}`,
or `{"not": {...}}`. `exclude` negates its lookup object. Predicate trees and
nested JSON filter values have depth and size limits. Equivalent lookup aliases
cannot be specified twice in the same lookup object. Pagination uses the primary
key as a tie-breaker; concurrent writes can still shift offsets.

For a goal journal, first query `StrategicGoal`, then call
`ergo_table_add` with `{"table":"GoalProgress","values":{"goal_id":42,"notes":"Made progress"}}`.
To page recent progress, call `ergo_table_query` with
`{"table":"GoalProgress","filters":{"goal_id":42},"order_by":["-created_at"],"fields":["id","goal_id","notes"],"limit":20,"offset":0}`.
Advance using `next_offset` while `has_more` is true.

For example, `metrics={"n":{"op":"count"},"total":{"op":"sum","field":"price"}}`
summarizes rows; `group_by=["status"]` returns up to `limit` groups.
Batch operations use `{"op":"add","values":{...}}`,
`{"op":"update","id":1,"values":{...}}`, or
`{"op":"upsert","match":{"unique_field":"value"},"values":{...}}`.
An upsert match must be a declared unconditional unique key; each operation
validates and saves normally, and any failure rolls back the whole batch.
Null upsert keys, conditional or deferred constraints, optimistic concurrency,
count distinct, batch delete and filter-wide updates are not supported.
Offsets are limited to 100,000; beyond that boundary `has_more` may be true
with no `next_offset`. Grouped summaries return at most 200 groups.

Load it from the start in chats that work with the data:
`chats: {main: {skills: [tables]}}`.

## From code

Tools, schedule steps and workers get a table with `ctx.table(name)`
(any case), which is the Django model:

```python
from django_ergo.bots import bot_tool

@bot_tool(takes_context=True)
def log_visit(ctx, address: str, notes: str) -> str:
    """Record that we visited a house."""
    House = ctx.table("House")
    house, _ = House.objects.update_or_create(address=address,
                                              defaults={"status": "visited", "notes": notes})
    return f"Saved visit to {house.address}."
```

A common shape is a schedule whose `run` step pulls data into a table each
morning and a pinned page that charts it, so the model is only involved
when there's something to say. See [Schedules](schedules.md#actions).

Tables are regular models, so the Django shell, admin code and your own
views can use them: `bot.table("House")` returns the model.

## Pages over tables

A `.jhtml` file is a Jinja template rendered over the tables each time
someone opens it:

```html
<h1>House search</h1>
{{ blocks.metric(label="Visited", table="House", filters={"status": "visited"}) }}
{{ blocks.metric(label="Average price", table="House", aggregate="avg", field="price", format="money") }}
{{ blocks.table(table="House", columns=["address", "price", "status"], order_by=["-created_at"], limit=20) }}
{% for row in table("House").group("status", n="count") %}
  {{ row.status }}: {{ row.n }}<br>
{% endfor %}
```

- `table(name)` is read-only: `filter`, `exclude`, `order_by`, `limit`,
  `count`, `sum`, `avg`, `min`, `max`, `group(fields..., alias="sum:field")`,
  `first` and `rows`.
- `blocks.metric`, `blocks.table` and `blocks.chart(table, x, y, kind="line"|"bar", group, aggregate)`
  render common pieces; `blocks.heading`, `markdown` and `html` fill in the
  rest.
- Pin a page in a chat with `chats.<name>.pins: [pages/houses.jhtml]`, or
  let the bot write its own with the `pages` plugin.
- A page updates itself when a table it reads changes; see
  [Live refresh](#live-refresh). Buttons call the bot's
  [page actions](bots.md#page-actions).

More in [Pages and pins](bots.md#pages-and-pins).

### Editable page blocks

Use `blocks.form(table="House")` to add a validated row, or
`blocks.table(table="House", edit=true, delete=true)` for inline editing and
approved deletes. Inputs follow the model field type; choices are selects and
booleans are checkboxes. The controls call the built-in `ergo.table.*` page
actions, so they use the same `clean` and `full_clean` checks as the table
tools. A model can opt out of both with:

```python
class ImportLog(BotTable):
    page_writes = False
```

## Live refresh

A page that reads a table re-renders when that table changes, so nobody
reloads it by hand. Saving or deleting a row of any `BotTable` sends the
`table_changed` signal (`django_ergo.bots.tables`) once the transaction
commits, once per table however many rows it touched.

Bulk writes skip Django's model signals: `.update()`, `bulk_create`, a
queryset's `.delete()` and raw SQL. After one, call `touch()` so open pages
notice:

```python
ctx.table("Pantry").objects.filter(name=item).update(on_order=qty)
ctx.table("Pantry").touch()
```

`touch()` also waits for the open transaction to commit. Ergonaut turns the
signal into a stream the page viewer listens to; see
[Live refresh](bots.md#live-refresh).

## In Ergonaut

The bot's page lists its tables with row counts, and each opens in a row
browser.
