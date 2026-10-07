# Spec: page actions and live pages

Repo: boundcorp/django-ergo (public; keep deploy details out).
Files: `src/django_ergo/bots/pages.py`, `bots/tools.py`, `bots/runtime.py`,
`bots/tables.py`, `ergonaut/ergonaut/api/bots.py`, a new
`ergonaut/ergonaut/api/page_events.py`, `ergonaut/frontend/src/components/Pins.tsx`
(`PageViewer`), `ergonaut/frontend/src/api.ts`, tests, and docs (`docs/tables.md`,
`docs/bots.md`, `docs/skills.md`, the `page-design` and `skillbuilder` library skills).

## Goal

`.jhtml` pages become small apps. Today they only read tables. After this:

1. A page can call functions the bot declared (**page actions**), as the viewer,
   with a confirm step for writes that need approval.
2. A page re-renders by itself when a table it reads changes (**live refresh**).
3. Pages get form and row-edit blocks over the bot's tables, with no code.
4. A page can hand a request to the bot (**ask**), which goes through a chat turn.

Build in that order. 1 and 2 are the first PR; 3 and 4 can follow.

## Current state (what this builds on)

- `render_page` (`bots/pages.py`) renders Jinja in `ImmutableSandboxedEnvironment`;
  `PageContext.table(name)` returns a read-only `TableView`. Every table read goes
  through `PageContext.table`.
- Chat attachment pages are served with `Content-Security-Policy: sandbox allow-scripts ...`
  (`SANDBOXED` in `api/bots.py`), so they have an opaque origin and can't call the
  API with the viewer's login.
- Bot-folder pages (`GET /api/bots/{bot}/files/{path}`) are served with
  `sandboxed=False`, in the app's origin. Any script in one can already call the
  whole API as the viewer. This spec closes that (see "Sandbox folder pages").
- Pages show in `PageViewer` (`Pins.tsx`) as an iframe; "Open in a new tab" opens the
  raw URL.
- Approval exists only inside a model turn (`conversation/runner.py` `PendingApproval`).
- Live chat updates: `ergonaut.apps.bots.tasks.notify()` publishes on Redis and
  `api/stream.py` serves SSE, polling when Redis is absent.
- `BotTable` rows have `created_at`/`updated_at`. Table tools are `ergo_table_query`,
  `ergo_table_add`, `ergo_table_update`, `ergo_table_delete` (delete needs approval).

## 1. Page actions

### Declaring

New decorator in `bots/tools.py`, exported from `django_ergo.bots`:

```python
from django_ergo.bots import page_action

@page_action(requires_approval=True, approval_preview=lambda ctx, item, qty: f"Order {qty} × {item}")
def restock(ctx, item: str, qty: int = 1) -> dict:
    """Order more of an item."""
    ctx.table("Pantry").objects.filter(name=item).update(on_order=qty)
    ctx.table("Pantry").touch()
    return {"message": f"Ordered {qty} {item}"}
```

- Lives in the bot's tool files, found at load time the same way `@bot_tool` is
  (`fn.__page_action__ = PageAction(name, function, parameters, required,
  requires_approval, approval_preview)`); `Bot.page_actions` is a dict by name.
  Parameters are inferred like `bot_tool` (first parameter is always the context).
- A function may carry both `@bot_tool` and `@page_action`; they stay independent.
  Plain tools are never callable from pages.
- The context is a `ToolContext` with `user` = the viewer, `session` = the chat the
  page was opened from (or None), and a new `page` attribute (the page path or
  attachment id).
- The return value must be JSON-serializable. Recognized keys, all optional:
  `message` (shown as a toast), `reload` (bool, force a re-render), `open` (a URL
  for the viewer to open). Anything else is passed to the page.
- Long work: the action starts `ctx.tasks.start(...)` and returns at once; the task
  reports progress by writing rows, which live refresh shows. No task-polling API
  in this version.

### Calling from a page

Every rendered page (attachment or folder, with or without its own `<html>`) gets a
small inline bridge script injected by `render_page`, defining `window.ergo`:

```js
const result = await ergo.call("restock", {item: "flour", qty: 2})
ergo.on("table:Pantry", rows => ...)   // see live refresh
ergo.reload()
```

`ergo.call` posts `{type: "ergo:call", id, name, args}` to `window.parent`. The page
never makes the HTTP request itself.

`PageViewer` listens for `message` events, accepts only those whose `source` is its
own iframe's `contentWindow`, and calls the API. The bot and session come from the
viewer's pin, never from the page, so a page can only call its own bot's actions.
It posts `{type: "ergo:result", id, ok, result | error}` back.

"Open in a new tab" points to a new minimal SPA route (`/pages/view?…`) that hosts
the same `PageViewer` full-screen, so the bridge works there too. A page opened with
no parent gets an `ergo.call` that rejects with "Open this page in Ergonaut to use
its buttons".

### API

`POST /api/bots/{bot}/actions/{name}` with `{args, page, session_id, approval}`.
Session login (with CSRF) or API key, like the other routes; `get_bot` checks the
bot is visible to the user; `session_id`, when given, must be in `visible_sessions`.

- Unknown action: 404. Arguments are checked against the inferred schema (missing
  required, wrong JSON type): 400 with the message.
- `requires_approval` and no `approval`: respond
  `{"needs_approval": true, "preview": "...", "approval": "<token>"}`. The token is
  `django.core.signing` over (user id, bot, action, sha256 of the args JSON), valid
  5 minutes. The viewer shows a confirm dialog with the preview; on Yes it repeats
  the call with the token. The token goes to the viewer, not to the page.
- Exceptions return 400 with the exception text for `ValueError`/`ValidationError`
  and a generic 500 otherwise (logged).
- Runs synchronously in the request with a 30 s budget; anything longer belongs in
  `ctx.tasks`.

### Record

New model `PageActionCall` (in Ergonaut's bots app): bot, action, user, session
(nullable), page, args, result or error, approved (bool), duration ms, created_at.
Admin list view. When the call came from a chat, the bot sees it on its next turn
through a built-in context section "Page actions since your last reply" (one line
each: who, action, args, outcome), so it knows what people did. Keep it to the last
20.

### Sandbox folder pages

Serve folder `.jhtml` with `sandboxed=True` too, once the bridge exists, so no page
script can call the API directly. Check that folder assets (`.js`, `.mjs`, `.css`,
images) still load from a sandboxed page: they are requested from an opaque origin,
so module scripts need `Access-Control-Allow-Origin` and the login cookie may not be
sent. If cookies aren't sent, give asset URLs a short-lived signed query parameter
(`?sig=`) added by `render_page` and accepted by `bot_file` in place of login. Pinned
dashboards in existing bot repos must keep rendering; test with one.

## 2. Live refresh

### What a page reads

`PageContext` records every table name passed to `table()` (including through
`blocks`). `render_page` injects them into the bridge as `ergo.tables`. On load the
bridge posts `{type: "ergo:ready", tables, listening}` to the viewer, where
`listening` lists tables the page handles itself with `ergo.on`.

### Change notices

- `django_ergo.bots.tables` defines a Django signal `table_changed(bot_name, table)`.
- A receiver on `post_save` and `post_delete` for every `BotTable` subclass sends it
  in `transaction.on_commit`.
- Bulk writes (`.update()`, `bulk_create`, `.delete()` on querysets, raw SQL) skip
  those signals, so `BotTable.touch()` (a classmethod) sends it by hand. The table
  tools' bulk paths and `ctx.table(...)` docs mention it; the `skillbuilder` skill
  tells bots to call it after bulk writes.
- Ergonaut connects `table_changed` to a Redis publish on `ergonaut:tables` with
  payload `"<bot>:<Table>"`, best-effort like `notify()`.

### Stream

`GET /api/bots/{bot}/tables/events?tables=A,B` (new `api/page_events.py`, same shape
as `api/stream.py`: session or API key, Redis wake with poll fallback, ends after a
few minutes, EventSource reconnects). Events are `{"changed": ["A"]}`.

Missed events: the stream keeps a fingerprint per table, `(count, max(updated_at),
max(pk))`, computed on connect and on each poll tick when Redis is absent, and on
connect compares it with `?since=<fingerprints>` from the client, so a viewer that
was disconnected catches up with one event. No new storage.

### Viewer behavior

- One EventSource per open page for the tables it reported.
- On `changed`: tables in `listening` are forwarded to the page as
  `{type: "ergo:changed", table}` (the page's `ergo.on` handler gets the event and
  may call `ergo.reload()` or fetch nothing). Others trigger a re-render.
- Re-render is debounced to 1 s after the last change and deferred while the page
  reports a focused form field (the bridge posts focus/blur), so typing isn't lost.
- Scroll is kept: before reloading the viewer asks the bridge for `scrollY`, then
  passes it back after load (`ergo:restore`).
- After a page action resolves with `reload: true` the page re-renders at once.
- A small "Live" dot in the viewer header shows the stream is connected.

## 3. Form and row blocks

Built on page actions, with built-in action names under `ergo.table.*`:
`ergo.table.add`, `ergo.table.update`, `ergo.table.delete`. They reuse the table
tools' code (`clean`, `full_clean`), so validation is identical. Delete needs
approval (preview names the row). A table can opt out with `page_writes = False` on
the model.

Blocks (add to `BLOCK_TYPES`, and to the pages plugin's block list):

- `blocks.form(table, fields=None, values=None, submit="Add", title="")`: one input
  per field (type from the model field; choices become selects; booleans
  checkboxes); submit calls `ergo.table.add`, shows field errors from the 400.
- `blocks.table(..., edit=False, delete=False)`: per-row Edit (inline form calling
  `ergo.table.update`) and Delete buttons.
- `blocks.button(label, action, args={}, confirm="")`: calls any page action.

Live refresh shows the new rows, so blocks never patch the DOM themselves.

## 4. Ask the bot

Built-in action `ergo.ask` with `{text, chat}` where `chat` is `"main"`, a named
chat, or `"new"` (a new thread under main, titled from the text). It posts `text` as
a message from the viewer into that chat through the normal send path, prefixed
with the page name (`From the Pantry page: …`), and returns the session id. The
viewer toasts "Sent to <chat>" with an Open link. The reply and any approvals happen
in the chat as usual. `ergo.ask(text, {chat})` in the bridge;
`blocks.button(..., ask="…")` as sugar.

## Tests

- `page_action` discovery, schema inference, `Bot.page_actions`.
- API: unknown action, bad args, user without the bot, session not visible, approval
  round trip (token required, wrong args hash rejected, expired rejected), result
  recorded in `PageActionCall`.
- Context section lists recent calls for the session.
- `PageContext` records tables read; `render_page` injects the bridge and table list
  into pages with and without `<html>`.
- `table_changed` fires on save/delete after commit and on `touch()`, not on a rolled
  back transaction.
- Stream: emits on a Redis notice and on fingerprint change without Redis; `since`
  catch-up.
- Folder page served sandboxed and its assets still load (signed URL path if needed).
- Blocks: form fields per field type, update/delete buttons, `page_writes = False`.
- `ergo.ask` posts into main, a named chat and a new thread.
- Frontend: the viewer ignores messages from other windows; approval dialog flow.

## Docs

`docs/tables.md` (live refresh, `touch()`), `docs/bots.md` and `docs/skills.md`
(page actions, blocks), the `page-design` skill (how to write interactive pages:
`ergo.call`, `ergo.on`, forms) and `skillbuilder` (declare actions next to tools;
call `touch()` after bulk writes). Add a short example to an example bot in
`examples/`.

## Out of scope

Per-user row permissions (rows stay shared), calling actions from outside Ergonaut,
WebSockets, task progress polling, and actions that stream output.
