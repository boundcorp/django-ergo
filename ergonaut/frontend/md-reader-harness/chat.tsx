import { createRoot } from 'react-dom/client'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import '@fontsource/inter/400.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/space-grotesk/700.css'
import '../src/index.css'
import { api, type Message, type SessionDetail } from '../src/api'
import { Chat } from '../src/pages/Chat'
import { MarkdownReaderOwner } from '../src/components/MarkdownReaderOwner'
import { ComposerVoiceProvider } from '../src/components/VoiceRecorder'
import './visual.css'

const messages: Message[] = Array.from({ length: 30 }, (_, i) => ({
  line: i + 1,
  role: i % 2 ? 'assistant' : 'user',
  timestamp: null,
  blocks: i === 8
    ? [{ type: 'text', text: 'Please review the attached release plan.' }, { type: 'attachment', id: 'fixture-markdown', label: 'release-plan.md', media_type: 'text/markdown' }]
    : [{ type: 'text', text: `Conversation message ${i + 1}: discussing the synthetic release plan and its milestones.` }],
}))
const detail: SessionDetail = {
  debug_shortcut: false,
  session: { id: 'fixture', bot: 'release-helper', title: 'Release planning', role: 'thread', status: 'active', parent_id: null, username: 'demo', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' },
  messages,
  calls: [],
}
api.session = async () => detail
api.attachments = async () => []
api.pins = async () => []
api.models = async () => ({ models: [], default: '', default_engine_type: '' })
api.realtimeVoiceConfig = async () => ({ enabled: false })

api.voiceConfig = async () => ({ enabled: false, max_duration: 0 })
api.sessionUsage = async () => ({ until: '', hours: [], thread_tokens: 0, agent_tokens: 0, agent_usage_complete: false, agent_usage_coverage: { status: 'partial', excluded: ['orca', 'native_session', 'legacy'], collection: 'periodic', zero_is_conclusive: false } })
class FixtureEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null
  private update = () => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ messages: [{ line: 31, role: 'assistant', timestamp: null, blocks: [{ type: 'text', text: 'Synthetic live update arrived while reading.' }] }], calls: [] }) }))
  constructor(_url: string) { window.addEventListener('synthetic-chat-update', this.update) }
  close() { window.removeEventListener('synthetic-chat-update', this.update) }
}
window.EventSource = FixtureEventSource as unknown as typeof EventSource

const router = createMemoryRouter(
  [{ path: '/s/:id', element: <ComposerVoiceProvider><MarkdownReaderOwner><Chat onChange={() => undefined} /></MarkdownReaderOwner></ComposerVoiceProvider> }],
  { initialEntries: ['/s/fixture'] },
)
createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />)
