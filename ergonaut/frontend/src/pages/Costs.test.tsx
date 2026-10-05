import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { Costs } from '../api'
import { AgentSessions } from './Costs'

describe('AgentSessions', () => {
  it('renders separate agent metrics and a chat-linked worker row', () => {
    const data = {
      agents: {
        headline: { sessions: 1, tokens: 150, cache_hit: 0.25 },
        rows: [
          {
            worker_id: 'worker-1',
            worker_title: 'Implement usage',
            agent: 'codex',
            model: 'gpt-6-codex',
            chat_id: 'chat-1',
            chat_title: 'Costs work',
            bot: 'developer',
            worker_status: 'completed',
            input_tokens: 100,
            cache_write_tokens: 0,
            cache_read_tokens: 25,
            output_tokens: 25,
            reasoning_tokens: 5,
            tokens: 150,
            cache_hit: 0.2,
            requests: 3,
          },
        ],
      },
    } as Costs

    const page = renderToStaticMarkup(
      <MemoryRouter>
        <AgentSessions data={data} />
      </MemoryRouter>,
    )

    expect(page).toContain('Agent sessions')
    expect(page).toContain('Implement usage')
    expect(page).toContain('gpt-6-codex')
    expect(page).toContain('25%')
    expect(page).toContain('href="/s/chat-1"')
  })
})
