import { useState, type CSSProperties } from 'react'
import { ChatView, type Attachment, type ChatStatus, type Message } from '@ergo/chat-ui'

const initial: Message[] = [
  { id: 'u1', role: 'user', parts: [{ type: 'text', text: 'Summarise the deploy and **check the logs**.' }] },
  {
    id: 'a1',
    role: 'assistant',
    parts: [
      {
        type: 'text',
        text: [
          '## Plan',
          '',
          '1. Read the log',
          '2. Deploy `v1.4.2`',
          '',
          '```python',
          'def deploy(env: str) -> bool:',
          '    return env == "staging"  # not prod',
          '```',
          '',
          '| env | status |',
          '|---|---|',
          '| staging | ok |',
          '',
          'Raw <b onclick="x()">html</b> stays text. [Unsafe](javascript:alert(1)) and [docs](https://example.com/docs).',
        ].join('\n'),
      },
      {
        type: 'tool',
        id: 'call-1',
        name: 'mcp__logs__tail',
        status: 'running',
        input: { path: '/var/log/app.log', lines: 50 },
      },
      {
        type: 'tool',
        id: 'call-2',
        name: 'search',
        status: 'done',
        input: { q: 'deploy' },
        result: [{ text: '3 matches' }],
      },
      {
        type: 'tool',
        id: 'call-3',
        name: 'deploy',
        status: 'error',
        input: { env: 'prod' },
        result: 'permission denied',
      },
      { type: 'custom', kind: 'approval', id: 'approval-1', data: { tool: 'deploy', env: 'staging' } },
      { type: 'future-widget', payload: 1 } as never,
    ],
  },
  {
    id: 'u2',
    role: 'user',
    parts: [
      { type: 'text', text: 'Here is the config.' },
      {
        type: 'attachment',
        attachment: { id: 'f1', name: 'config.yaml', mediaType: 'text/yaml', url: '/files/config.yaml', size: 1536 },
      },
    ],
  },
]

export default function Demo({ style }: { style?: CSSProperties } = {}) {
  const [messages, setMessages] = useState(initial)
  const [status, setStatus] = useState<ChatStatus>('idle')
  const [decision, setDecision] = useState('pending')
  return (
    <div
      style={
        style ?? { height: 640, maxWidth: 760, margin: '0 auto', border: '1px solid #ccc', fontFamily: 'Georgia, serif' }
      }
    >
      <ChatView
        messages={messages}
        status={status}
        onSend={async (text, attachments: Attachment[]) => {
          setStatus('sending')
          setMessages(m => [
            ...m,
            {
              id: `u${m.length}`,
              role: 'user',
              parts: [
                { type: 'text', text },
                ...attachments.map(attachment => ({ type: 'attachment' as const, attachment })),
              ],
            },
          ])
          setStatus('idle')
        }}
        onStop={() => setStatus('idle')}
        onAttach={async files => files.map(f => ({ id: f.name, name: f.name, mediaType: f.type, size: f.size }))}
        onRemoveAttachment={async () => {}}
        renderPart={part =>
          part.type === 'custom' && part.kind === 'approval' ? (
            <div data-testid="approval" style={{ border: '1px solid #b80', padding: 8 }}>
              Approve deploy? ({decision}) <button onClick={() => setDecision(`approved ${part.id}`)}>Approve</button>
            </div>
          ) : undefined
        }
      />
    </div>
  )
}
