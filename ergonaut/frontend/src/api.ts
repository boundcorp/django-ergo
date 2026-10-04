// Thin client for the Ergonaut API (django-ninja at /api, Django session auth).

export type User = { id: string; username: string; email: string; first_name: string; last_name: string }

export type Bot = {
  name: string
  description: string
  icon?: string // bot.yaml icon (an emoji); '' = its first letter
  color?: string // bot.yaml color: a palette name or hex; '' = picked from the name
  orchestration: boolean
  knowledge: boolean
  parent: string
  root_session_id: string | null
  chats?: { name: string; description: string; session_id: string | null }[]
}

export type Session = {
  id: string
  bot: string
  title: string
  role: string
  parent_id: string | null
  started_by?: string // another bot's chat that started this thread
  started_by_id?: string | null
  status: string
  username: string
  created_at: string
  updated_at: string
  open_in?: number
  open_out?: number
  busy?: boolean // a turn is running now
  unread?: boolean // a reply came after the owner last opened it
  attention?: boolean // the latest turn waits on the user: an approval, a question, or a failure
  engine_type?: string // openai or claude: the engine its history is stored for
  model?: string // the provider/model picked for this chat ('' = the bot's default)
  resolved_by?: string // the bot that resolved this thread (ergo_thread_resolve)
  resolved_summary?: string // its one-line summary of how the thread ended
  // Threads by status (components/ThreadList): the group, why it waits, the bot's status line.
  bucket?: 'waiting' | 'working' | 'review' | 'idle' | 'resolved' | ''
  waiting_for?: 'approval' | 'question' | 'failure' | ''
  status_line?: string // the bot's one line from its latest reply (a resolved thread's summary)
  pinned?: boolean
  last_activity?: string | null // when its latest turn moved
  workers_running?: number
  workers_total?: number
  prs?: PrLink[] // pull requests it reported, newest first
}

export type ModelChoice = {
  id: string // provider/model
  name: string
  label: string
  provider: string
  engine_type: string
  available: boolean // its provider's API key is set
}

export type BotModels = { default: string; default_engine_type: string; models: ModelChoice[] }

export type DelegatedRequest = {
  id: string
  direction: 'in' | 'out'
  other_session_id: string | null
  other: string
  text: string
  status: 'queued' | 'delivered' | 'waiting' | 'answered' | 'failed'
  reply: string
  created_at: string
  updated_at: string
}

export type Block =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'attachment'; label: string; id?: string; kind?: string; media_type?: string }
  | { type: 'context'; text: string }
  | { type: 'tool_use'; id: string; name: string; input: unknown }
  | { type: 'tool_result'; tool_use_id: string; name?: string; content: unknown; is_error?: boolean }

export type Message = { line: number; role: string; blocks: Block[]; timestamp: string | null }

export type Approval = { id: string; name: string; input: unknown; preview?: string; preview_error?: boolean }

export type Call = {
  id: string
  kind: string
  // in_progress, awaiting_approval, completed, failed, turn_limited or stopped
  status: string
  request: string
  response: { type?: string; text?: string; suggestions?: string[] } | null
  error: string
  first_sequence: number | null
  last_sequence: number | null
  model_name: string
  input_tokens: number
  output_tokens: number
  turns_used: number
  pending_approvals: Approval[]
  tools: string[]
  created_at: string
  error_summary?: string // a failure in plain words
  error_hint?: string // what to do about it
  dismissed?: boolean
}

export type KB = {
  id: string
  name: string
  kind: string
  location: string
  articles: { path: string; title: string; root: boolean }[]
}

export type KBArticle = { path: string; title: string; body: string }

export type CostBucket = {
  name: string
  calls: number
  // Uncached input only; cache writes and reads are separate.
  input_tokens: number
  cache_write_tokens: number
  cache_read_tokens: number
  output_tokens: number
  reasoning_tokens: number // part of output_tokens
  input_cost: number
  cache_write_cost: number
  cache_read_cost: number
  output_cost: number
  cost: number
  unpriced_calls: number
}

export type Costs = {
  days: number
  total: CostBucket
  by_kind: CostBucket[]
  chat_reply_by_bot: CostBucket[]
  by_model: CostBucket[]
  by_day: { date: string; cost: number; calls: number }[]
  unpriced_models: string[]
}

export type BotDetail = Bot & {
  engine: string
  model: string
  timezone: string
  folder: string
  instructions: string
  plugins: string[]
  tools: { name: string; description: string; requires_approval: boolean }[]
  skills: {
    name: string
    description: string
    body: string
    source?: string
    tools?: { name: string; description: string; requires_approval: boolean }[]
    always_in?: string[]
  }[]
  manages_repo?: boolean
  schedules?: {
    name: string
    cron: string
    users: string[]
    enabled: boolean
    next_run: string | null
    actions: {
      kind: 'prompt' | 'run'
      message: string
      to: string
      thread_title: string
      thread_in: string
      run: string
      args: Record<string, unknown>
    }[]
  }[]
  tables?: { name: string; description: string; rows: number | null }[]
  pages?: { path: string; url: string; exists: boolean }[]
  jobs?: {
    id: number
    name: string
    target: string
    status: string
    error: string
    result: unknown
    created_at: string
    completed_at: string | null
  }[]
}

export type AttachmentFile = {
  id: string
  filename: string
  media_type: string
  kind: string
  size: number | null
  source: 'message' | 'upload' | 'bot'
  message_sequence: number | null
  pinned: boolean
  view: '' | 'page' | 'html' | 'image' | 'pdf' | 'media' | 'markdown' | 'csv' | 'json' | 'text'
  created_at: string
  updated_at: string
  archived_at?: string | null
  link?: PrLink | null // a pull request the bot reported (no stored file)
}

// A pull request a bot or worker reported (django_ergo.conversation.links); state is read with gh.
export type PrLink = {
  id: string
  url: string
  repo: string
  number: number | null
  title: string
  state: '' | 'open' | 'draft' | 'merged' | 'closed'
  checks: '' | 'passing' | 'failing' | 'pending'
}

// A chat as thread cards and links show it (api/bots.py thread_summary).
export type ThreadSummary = {
  id: string
  title: string
  bot: string
  role: string
  archived: boolean
  attention: boolean
  state: 'working' | 'waiting_for_approval' | 'idle'
  started_by: string
  started_by_id: string
  working_for: number
  waiting_on: number
  workers_running: number
  resolved_by?: string
  resolved_summary?: string
  ready_to_resolve?: boolean // orchestrator.thread_status: its work looks done
}

// A request this chat sent to another chat (ergo_thread_send, ergo_message_up).
export type SentCard = {
  message_id: string
  created_at: string
  status: 'queued' | 'working' | 'waiting' | 'done' | 'failed'
  text: string
  reply: string
  thread: ThreadSummary
  prs: PrLink[]
}

export type SidebarPin = { name: string; url: string; icon?: string; filename?: string }

// Something pinned in a chat: a bot-folder file (chats.<name>.pins) or a pinned chat file.
export type Pin = {
  kind: 'bot_file' | 'file'
  name: string // its title, or the file name
  icon?: string // an emoji; '' = one for the file type (pinIcon)
  url: string
  path?: string
  id?: string
  filename?: string
  exists?: boolean
}

export type PullRequest = {
  number: number
  title: string
  url: string
  branch: string
  author: string
  created_at: string
  body: string
  additions: number
  deletions: number
  changed_files: number
}

export type Changes = {
  managed_by: string
  repo: string
  mode: string
  draft_diff: string
  pull_requests: PullRequest[]
  error: string
}

// Long-running work a chat started (django_ergo.bots.workers).
export type Worker = {
  id: string
  title: string
  function: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: string
  result: unknown
  error: string
  created_at: string
  completed_at: string | null
  prs?: PrLink[] // pull requests its result links
}

export type SessionDetail = {
  session: Session
  messages: Message[]
  calls: Call[]
  requests?: DelegatedRequest[]
  workers?: Worker[]
  inbox?: { id: string; text: string; files: number }[] // sent mid-turn, not yet given to the model
  first_line?: number | null // the oldest line returned
  has_more?: boolean // older messages exist: ask with before=first_line
  message_count?: number // in the whole session
  sent?: SentCard[] // requests this chat sent, newest first
  prs?: PrLink[] // pull requests reported in this chat
}

export type Turn = {
  session_id: string
  call_id: string | null
  type: string | null
  text: string
  suggestions: string[]
  approvals: Approval[]
  error: string
  queued?: boolean
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function csrfToken(): string {
  return document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)?.[1] ?? ''
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(method === 'GET' ? {} : { 'X-CSRFToken': csrfToken() }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'X-CSRFToken': csrfToken() },
    body: form,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  csrf: () => request<{ csrftoken: string }>('GET', '/auth/csrf'),
  me: () => request<User>('GET', '/auth/me'),
  login: (username: string, password: string) => request<User>('POST', '/auth/login', { username, password }),
  logout: () => request<{ ok: boolean }>('POST', '/auth/logout'),
  bots: () => request<Bot[]>('GET', '/bots'),
  bot: (name: string) => request<BotDetail>('GET', `/bots/${name}`),
  costs: (days: number) => request<Costs>('GET', `/costs?days=${days}`),
  kbs: (bot: string) => request<KB[]>('GET', `/bots/${bot}/kbs`),
  kbArticle: (bot: string, kb: string, path: string) =>
    request<KBArticle>('GET', `/bots/${bot}/kbs/${kb}/article?path=${encodeURIComponent(path)}`),
  sessions: (params: { bot?: string; q?: string; status?: string } = {}) => {
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][])
    return request<Session[]>('GET', `/sessions?${query}`)
  },
  openRoot: (bot: string) => request<Session>('POST', `/bots/${bot}/root`),
  openChat: (bot: string, name: string) => request<Session>('POST', `/bots/${bot}/chats/${name}`),
  newThread: (bot: string, title: string, message = '', model = '') =>
    request<Session>('POST', `/bots/${bot}/threads`, { title, message, model }),
  models: (bot: string) => request<BotModels>('GET', `/bots/${bot}/models`),
  setModel: (id: string, model: string) => request<Session>('POST', `/sessions/${id}/model`, { model }),
  // The newest page of messages, or the page before line `before`.
  session: (id: string, before?: number) =>
    request<SessionDetail>('GET', `/sessions/${id}${before == null ? '' : `?before=${before}`}`),
  call: (id: string) =>
    request<Call & { system_prompt: string; transcript: unknown[]; metadata: unknown }>('GET', `/calls/${id}`),
  // While a turn runs, "send" steers it and "interrupt" stops it and starts a new one.
  send: (id: string, text: string, attachmentIds: string[] = [], mode: 'send' | 'interrupt' = 'send') =>
    request<Turn>('POST', `/sessions/${id}/messages`, { text, attachment_ids: attachmentIds, mode }),
  stop: (id: string) => request<Turn>('POST', `/sessions/${id}/stop`),
  resume: (id: string) => request<Turn>('POST', `/sessions/${id}/resume`),
  dismissCall: (callId: string) => request<Call>('POST', `/calls/${callId}/dismiss`),
  unsend: (id: string, itemId: string) =>
    request<{ text: string; attachment_ids: string[] }>('DELETE', `/sessions/${id}/inbox/${itemId}`),
  approve: (id: string, approve: boolean, approvalIds?: string[]) =>
    request<Turn>('POST', `/sessions/${id}/approvals`, { approve, approval_ids: approvalIds }),
  close: (id: string) => request<Session>('POST', `/sessions/${id}/close`),
  pinSession: (id: string, pinned: boolean) => request<Session>('POST', `/sessions/${id}/pin`, { pinned }),
  changes: (bot: string) => request<Changes>('GET', `/bots/${bot}/changes`),
  changeDiff: (bot: string, n: number) => request<{ diff: string }>('GET', `/bots/${bot}/changes/${n}/diff`),
  mergeChange: (bot: string, n: number) => request<{ result: string }>('POST', `/bots/${bot}/changes/${n}/merge`),
  closeChange: (bot: string, n: number) => request<{ result: string }>('POST', `/bots/${bot}/changes/${n}/close`),
  discardDraft: (bot: string) => request<{ result: string }>('POST', `/bots/${bot}/changes/draft/discard`),
  attachments: (id: string) => request<AttachmentFile[]>('GET', `/sessions/${id}/attachments`),
  uploadAttachment: (id: string, file: File) => upload<AttachmentFile>(`/sessions/${id}/attachments`, file),
  deleteAttachment: (id: string) => request<{ ok: boolean }>('DELETE', `/attachments/${id}`),
  downloadUrl: (id: string) => `/api/attachments/${id}/download`,
  viewUrl: (id: string) => `/api/attachments/${id}/download?inline=true`,
  botTree: (bot: string, version = 'live') =>
    request<{ files: { path: string; size: number; status: string }[]; truncated: boolean }>(
      'GET',
      `/bots/${bot}/tree?version=${encodeURIComponent(version)}`,
    ),
  botSource: (bot: string, path: string, version = 'live') =>
    request<{
      path: string
      size: number
      media_type: string
      text: string | null
      url: string
      deleted: boolean
      diff: string
    }>(
      'GET',
      `/bots/${bot}/source/${path.split('/').map(encodeURIComponent).join('/')}?version=${encodeURIComponent(version)}`,
    ),
  botProposals: (bot: string) =>
    request<{
      managed_by: string
      error?: string
      proposals: {
        version: string
        title: string
        number: number | null
        url: string
        changed: Record<string, string>
      }[]
    }>('GET', `/bots/${bot}/proposals`),
  botErrors: () => request<{ folder: string; name: string; error: string }[]>('GET', '/bot-errors'),
  tableRows: (bot: string, table: string, opts: { page?: number; order?: string; q?: string } = {}) =>
    request<{
      table: string
      description: string
      fields: { name: string; type: string }[]
      count: number
      page: number
      page_size: number
      rows: Record<string, unknown>[]
    }>(
      'GET',
      `/bots/${bot}/tables/${encodeURIComponent(table)}/rows?page=${opts.page ?? 1}&order=${encodeURIComponent(opts.order ?? '')}&q=${encodeURIComponent(opts.q ?? '')}`,
    ),
  allPins: () => request<Record<string, SidebarPin[]>>('GET', '/pins'),
  pins: (id: string) => request<Pin[]>('GET', `/sessions/${id}/pins`),
  pin: (id: string, pinned: boolean) => request<AttachmentFile>('POST', `/attachments/${id}/pin`, { pinned }),
}
