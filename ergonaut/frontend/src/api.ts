// Thin client for the Ergonaut API (django-ninja at /api, Django session auth).

export type User = { id: string; username: string; email: string; first_name: string; last_name: string }

export type Bot = { name: string; description: string; orchestration: boolean; root_session_id: string | null }

export type Session = {
  id: string
  bot: string
  title: string
  role: string
  parent_id: string | null
  status: string
  username: string
  created_at: string
  updated_at: string
}

export type Block =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'attachment'; label: string }
  | { type: 'context'; text: string }
  | { type: 'tool_use'; id: string; name: string; input: unknown }
  | { type: 'tool_result'; tool_use_id: string; name?: string; content: unknown; is_error?: boolean }

export type Message = { line: number; role: string; blocks: Block[]; timestamp: string | null }

export type Approval = { id: string; name: string; input: unknown }

export type Call = {
  id: string
  kind: string
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
  created_at: string
}

export type SessionDetail = { session: Session; messages: Message[]; calls: Call[] }

export type Turn = {
  session_id: string
  call_id: string | null
  type: string | null
  text: string
  suggestions: string[]
  approvals: Approval[]
  error: string
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

export const api = {
  csrf: () => request<{ csrftoken: string }>('GET', '/auth/csrf'),
  me: () => request<User>('GET', '/auth/me'),
  login: (username: string, password: string) => request<User>('POST', '/auth/login', { username, password }),
  logout: () => request<{ ok: boolean }>('POST', '/auth/logout'),
  bots: () => request<Bot[]>('GET', '/bots'),
  sessions: (params: { bot?: string; q?: string; status?: string } = {}) => {
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][])
    return request<Session[]>('GET', `/sessions?${query}`)
  },
  openRoot: (bot: string) => request<Session>('POST', `/bots/${bot}/root`),
  newThread: (bot: string, title: string) => request<Session>('POST', `/bots/${bot}/threads`, { title }),
  session: (id: string) => request<SessionDetail>('GET', `/sessions/${id}`),
  call: (id: string) => request<Call & { system_prompt: string; transcript: unknown[]; metadata: unknown }>('GET', `/calls/${id}`),
  send: (id: string, text: string) => request<Turn>('POST', `/sessions/${id}/messages`, { text }),
  approve: (id: string, approve: boolean) => request<Turn>('POST', `/sessions/${id}/approvals`, { approve }),
  close: (id: string) => request<Session>('POST', `/sessions/${id}/close`),
}
