# @ergo/chat-ui

A presentational React chat UI: a message transcript (markdown, code, tool calls, attachments) and a composer.
The host app owns everything else. The package has **no fetch, no auth, no router, no Ergonaut or PDS types**,
never executes tools and never sees provider secrets. It only renders the messages you pass in and calls the
callbacks you give it.

Status: first version (0.1.0), in this repository so Ergonaut changes stay atomic. Not published; consume the
`npm pack` tarball (see [Install](#install)). `package.json` is `private` until ownership and a registry are agreed.

- React `^18.2.0 || ^19.0.0` (peer; never bundled). Tested on 18.2 and 19.
- Prebuilt ESM + CJS, `.d.ts`, `./styles.css`. No runtime dependencies (markdown and highlighting are bundled).
- SSR-safe: no `window`/`document` at module scope; hydration is tested.
- Styling is precompiled and scoped: no Tailwind, no reset, nothing on `:root`/`body`, fonts inherit.

## Install

```sh
cd packages/ergo-chat-ui
npm ci && npm run build
npm pack                       # -> ergo-chat-ui-0.1.0.tgz
cd your-app && npm install /path/to/ergo-chat-ui-0.1.0.tgz
```

```tsx
import { ChatView } from '@ergo/chat-ui'
import '@ergo/chat-ui/styles.css' // Next Pages Router: import it in pages/_app.tsx
```

## Public API

```tsx
<ChatView
  messages={messages}            // Message[] (controlled; you own the list)
  status="idle"                  // 'idle' | 'sending' | 'streaming' | 'error'
  error="Connection lost"        // shown when status === 'error'
  onSend={async (text, attachments) => {}}      // reject to keep the draft
  onStop={() => {}}                              // Stop button while sending/streaming
  onAttach={async files => attachments}          // attach button appears only if given
  onRemoveAttachment={async attachment => {}}
  renderPart={(part, { message, index, defaultRender }) => undefined}
  empty={<p>Say hello</p>}
  theme="light"                  // 'light' | 'dark' | 'auto'
/>
```

`Transcript` and `Composer` are the same pieces for your own layout. Also exported: `Markdown`, `ToolCard`,
`AttachmentView`, `AttachmentList`, `safeLinkUrl`, `safeAttachmentUrl`.

### Types

```ts
type Attachment = { id: string; name: string; mediaType?: string; url?: string; size?: number }

type Part =
  | { type: 'text'; text: string }                                   // markdown
  | { type: 'attachment'; attachment: Attachment }
  | { type: 'tool'; id: string; name: string; status: 'running' | 'done' | 'error'; input?: unknown; result?: unknown }
  | { type: 'custom'; kind: string; id?: string; data?: unknown }    // host-defined: approvals, cards

type Message = { id: string; role: 'user' | 'assistant' | 'system'; parts: Part[]; createdAt?: string }
```

Parts with a `type` this version does not know (a newer host, a bad payload) render as a neutral
"Unsupported content: …" box, never as HTML. `custom` parts render the same placeholder unless `renderPart` handles them.

### Slots: `renderPart`

`renderPart(part, ctx)` runs for every part, including unknown ones. Return a node to replace the default,
`null` to hide it, or `undefined` to keep the default (`ctx.defaultRender` lets you wrap it).
This is how a host renders **approvals** and rich cards, with its own buttons and its own callbacks:

```tsx
renderPart={part =>
  part.type === 'custom' && part.kind === 'approval'
    ? <ApprovalCard id={part.id!} data={part.data} onDecide={decide} />   // yours, not the package's
    : undefined
}
```

`Composer` takes `draft`/`onDraftChange` and `attachments`/`onAttachmentsChange` to be controlled; without them it
keeps its own state. Enter sends, Shift+Enter is a newline, and an Enter that confirms an IME candidate is ignored.
A rejected `onSend` keeps the text and attachments and shows the error message.

### Safety

- Markdown: raw HTML is shown as text; links allow `http(s)`, `mailto`, `tel`, `#x` and same-origin paths only
  (`javascript:`, `data:`, `//host` and control characters are dropped to plain text); external links get
  `rel="noopener noreferrer nofollow"`. Markdown images are not rendered unless `allowMarkdownImages` (remote
  images from model text can track readers); then only `http(s)` with `referrerPolicy="no-referrer"`.
- Attachment URLs: `http(s)`, same-origin paths, `blob:` and raster `data:image/` only; otherwise the file name
  is shown without a link.
- Tool input/result are shown as text, never interpreted.

### Styling

`dist/styles.css` is plain CSS. Every selector starts with `.ergo-chat` (checked by a test); there is no reset,
no `:root`/`html`/`body` rule and no `font-family` other than `inherit` (and a monospace token for code).
Theme it by overriding variables on `.ergo-chat` (or `.my-host .ergo-chat`):

`--ergo-chat-fg`, `-muted`, `-bg`, `-surface`, `-raised`, `-border`, `-accent`, `-accent-fg`, `-user-bg`,
`-danger`, `-danger-bg`, `-success`, `-radius`, `-gap`, `-mono`, and `-hl-keyword|string|number|comment|title|attr`.

The component fills its parent: give `ChatView` a container with a height (`height: 100%` of a sized parent, or
a fixed height). The transcript scrolls; it sticks to the bottom until the reader scrolls up.
There are no portals or global overlays in this version, so MUI dialogs and z-index stacks are unaffected.

## What the host adapter owns

The package is controlled and stateless about the outside world. The adapter you write must handle:

| Concern | Adapter responsibility |
| --- | --- |
| Auth | Cookies/CSRF/tokens live in your transport. The package makes no requests; attachment `url`s must load with the reader's own credentials (or be pre-signed). Provider secrets stay on your server. |
| Transport | Load history, send, subscribe. Map your payloads to `Message[]`. |
| Stream replay and dedupe | Give every message and tool call a **stable id**; on reconnect, replay from your cursor and merge by id (replace, don't append) before setting `messages`. The package keys by id and will show duplicates if you pass them. Reconcile with a fresh snapshot after a gap. |
| Cancel vs unsubscribe | `onStop` means "ask the server to stop the turn". Closing the stream or unmounting is *not* a cancel: the server keeps running. Decide which your UI means and do it explicitly. |
| Send ambiguity | If `onSend` fails after the request left the browser, you may not know whether it was accepted. Use an idempotency key, check the replayed history before retrying, and reject with a message that says so. |
| Uploads | `onAttach` uploads (progress, limits, errors) and resolves with stored `Attachment`s; reject with a readable message. `onRemoveAttachment` deletes server-side; reject to keep it. Send only attachment ids you got back. |
| Approvals | Render them via `renderPart` as `custom` parts with a **stable approval id** and the revision of what is shown; submit decisions yourself, and treat a stale decision (call changed since display) as an error the server rejects. The package never runs the tool. |
| Status | Set `status` (`sending` after a send starts, `streaming` while events arrive, `error` + `error` text). |

## Ergonaut mapping

Ergonaut's `api.ts` types map onto the contract in an adapter (not yet written; see follow-ups).

| Ergonaut (`ergonaut/frontend/src/api.ts`) | `@ergo/chat-ui` | Notes |
| --- | --- | --- |
| `Message.line` | `Message.id` (`String(line)`) | Stable per session; use it as the dedupe key for SSE merges. |
| `Message.role` | `Message.role` | Other roles map to `system`. |
| `Message.timestamp` | `Message.createdAt` | |
| `Block {type:'text'}` | `{type:'text'}` | Markdown. |
| `Block {type:'tool_use'}` + matching `tool_result` | one `{type:'tool'}` | `id` = `tool_use.id`; `result` = `tool_result.content`; `status` = `error` if `is_error`, `done` if a result exists, else `running`. |
| `Block {type:'attachment'}` | `{type:'attachment'}` | `label`→`name`, `id`→`id`, `media_type`→`mediaType`; `url` from `api.ts` view/download URLs. |
| `Approval` (pending tool call) | `{type:'custom', kind:'approval', id, data}` | Host `renderPart` renders it; batching and revalidation stay in the adapter. |
| `Block {type:'thinking'}`, `{type:'context'}` | host `custom` parts or dropped | Not generic. |
| Delegated messages, worker/thread cards, voice, compaction | `custom` parts + `renderPart` | Stay in Ergonaut. |
| `chatSend.prepareChatSend` | adapter around `onSend` | Suggestion sends keep the draft; use `draft`/`onDraftChange`. |
| Chat status (`Chat.tsx`) | `status` | busy → `sending`/`streaming`. |
| Upload / delete attachment (`api.ts`) | `onAttach` / `onRemoveAttachment` | |
| Stop / interrupt | `onStop` | Interrupt (stop + new turn) stays in the host. |

Not in the package: voice recording and LiveKit, model and session controls, pins, workers dashboards, the
markdown reader portal, router links and thread chips.

## Develop

```sh
npm ci
npm run typecheck
npm test                 # React 19
npm run test:react18     # same tests on React 18.2 (installs it into .react18/)
npm run build            # dist/ (ESM, CJS, d.ts, styles.css)
npm run size
npm run fixtures         # npm pack, install the tarball into a Vite app and a Next 13.1 Pages Router app, build both
```

`fixtures/` holds a shared demo page (markdown, code, tool running/done/error, attachments, an unknown part and an
approval via `renderPart`) and two apps. The fixture script copies them to `.fixtures/` and installs the **tarball**,
so it exercises the real `exports` map. Fixtures are not in `files`, so they never reach the package or any host bundle.
Next fixture: Next 13.1.1, React 18.2, Pages Router, webpack, `experimental.esmExternals: false`, SSR/static prerender.

## Licenses

Bundled into `dist/`: react-markdown, remark-gfm and its unified/micromark/mdast dependencies (MIT), highlight.js
(BSD-3-Clause). Review the exact lockfile versions before any external distribution.
