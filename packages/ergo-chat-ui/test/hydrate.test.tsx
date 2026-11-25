import { hydrateRoot } from 'react-dom/client'
import { renderToString } from 'react-dom/server'
import { afterEach, expect, test, vi } from 'vitest'
import { ChatView, type Message } from '../src'
import { act, cleanup } from './helpers'

afterEach(cleanup)

test('server markup hydrates without warnings', async () => {
  const messages: Message[] = [
    {
      id: '1',
      role: 'assistant',
      parts: [
        { type: 'text', text: 'hi\n\n```ts\nconst a = 1\n```' },
        { type: 'tool', id: 't', name: 'x', status: 'done', input: { a: 1 }, result: 'ok' },
      ],
    },
  ]
  const ui = <ChatView messages={messages} onSend={() => {}} onAttach={async () => []} />
  const el = document.createElement('div')
  document.body.appendChild(el)
  el.innerHTML = renderToString(ui)
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  await act(() => {
    hydrateRoot(el, ui)
  })
  expect(errors).not.toHaveBeenCalled()
  errors.mockRestore()
  expect(el.querySelector('.ergo-chat__tool')).not.toBeNull()
})
