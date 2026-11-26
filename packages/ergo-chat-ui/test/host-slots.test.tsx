import { createRef } from 'react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { Composer, Transcript, type Message } from '../src'
import { act, cleanup, click, flush, key, render, type } from './helpers'

afterEach(cleanup)
const ta = (el: HTMLElement) => el.querySelector('textarea')!
const button = (el: HTMLElement, text: string) => [...el.querySelectorAll('button')].find(b => b.textContent === text)!

const messages: Message[] = [
  { id: '1', role: 'user', parts: [{ type: 'text', text: 'hi' }], meta: { line: 1 } },
  {
    id: '2',
    role: 'assistant',
    parts: [
      { type: 'text', text: 'hello' },
      { type: 'custom', kind: 'card', id: 'c' },
      { type: 'custom', kind: 'hidden', id: 'h' },
    ],
  },
]

describe('Transcript host slots', () => {
  test('renderMessage replaces the article, receives rendered parts and the meta, and can fall back', async () => {
    const el = await render(
      <Transcript
        messages={messages}
        renderPart={part =>
          part.type === 'custom' ? part.kind === 'hidden' ? null : <b>card:{part.id}</b> : undefined
        }
        renderMessage={(message, ctx) =>
          message.id === '1' ? (
            <section id={`m-${(message.meta as { line: number }).line}`} data-count={ctx.parts.length}>
              mine {ctx.parts}
            </section>
          ) : undefined
        }
      />,
    )
    const mine = el.querySelector('#m-1')!
    expect(mine.textContent).toContain('mine')
    expect(mine.textContent).toContain('hi')
    expect(el.querySelectorAll('article')).toHaveLength(1) // message 2 kept the package article
    expect(el.querySelector('article')!.textContent).toContain('card:c')
    expect(el.querySelector('article')!.textContent).not.toContain('hidden')
  })

  test('renderMessage returning null hides the message; ctx.parts leaves hidden parts out', async () => {
    let seen = -1
    const el = await render(
      <Transcript
        messages={messages}
        renderPart={part => (part.type === 'custom' && part.kind === 'hidden' ? null : undefined)}
        renderMessage={(m, ctx) => {
          if (m.id === '2') seen = ctx.parts.length
          return m.id === '1' ? null : undefined
        }}
      />,
    )
    expect(seen).toBe(2) // text + the unknown "card" placeholder; "hidden" dropped
    expect(el.querySelector('[data-message-id="1"]')).toBeNull()
  })

  test('scroll="host" renders a plain list: no log role, no tab stop, header and footer in place', async () => {
    const el = await render(
      <Transcript scroll="host" header={<i>top</i>} footer={<i>end</i>} messages={messages.slice(0, 1)} />,
    )
    const list = el.firstElementChild as HTMLElement
    expect(list.getAttribute('role')).toBeNull()
    expect(list.hasAttribute('tabindex')).toBe(false)
    expect(list.className).toContain('ergo-chat--bare')
    expect(list.firstElementChild!.textContent).toBe('top')
    expect(list.lastElementChild!.textContent).toBe('end')
  })

  test('scroll="host" never moves the scroll position', async () => {
    const el = await render(<Transcript scroll="host" messages={messages} />)
    const list = el.firstElementChild as HTMLElement
    Object.defineProperty(list, 'scrollHeight', { value: 999, configurable: true })
    expect(list.scrollTop).toBe(0)
  })
})

describe('Composer host integration', () => {
  test('canSend is the host guard: Enter reaches onSend even when the button is disabled', async () => {
    const onSend = vi.fn(async () => {})
    const el = await render(<Composer onSend={onSend} canSend={false} />)
    expect(button(el, 'Send').disabled).toBe(true)
    await type(ta(el), 'x')
    await key(ta(el), { key: 'Enter' })
    await flush()
    expect(onSend).toHaveBeenCalledWith('x', [])
  })

  test('canSend true enables Send without text (voice recording, say)', async () => {
    const onSend = vi.fn(async () => {})
    const el = await render(<Composer onSend={onSend} canSend />)
    await click(button(el, 'Send'))
    await flush()
    expect(onSend).toHaveBeenCalledWith('', [])
  })

  test('clearOnSend={false} leaves the draft and attachments to the host', async () => {
    const onSend = vi.fn(async () => {})
    const el = await render(<Composer onSend={onSend} clearOnSend={false} />)
    await type(ta(el), 'keep')
    await key(ta(el), { key: 'Enter' })
    await flush()
    expect(onSend).toHaveBeenCalled()
    expect(ta(el).value).toBe('keep')
  })

  test('sendWhileBusy keeps Send beside Stop and the actions slot sits between them', async () => {
    const onStop = vi.fn()
    const onSend = vi.fn(async () => {})
    const el = await render(
      <Composer
        onSend={onSend}
        onStop={onStop}
        busy
        sendWhileBusy
        canSend
        actions={<button type="button">Interrupt</button>}
        afterInput={<span data-slot="voice" />}
        footer={<div data-slot="footer" />}
      />,
    )
    const labels = [...el.querySelectorAll('button')].map(b => b.textContent)
    expect(labels).toEqual(['Stop', 'Interrupt', 'Send'])
    expect(el.querySelector('[data-slot=voice]')!.previousElementSibling).toBe(ta(el))
    expect(el.querySelector('[data-slot=footer]')).not.toBeNull()
    await click(button(el, 'Stop'))
    expect(onStop).toHaveBeenCalled()
  })

  test('without sendWhileBusy, Stop still replaces Send', async () => {
    const el = await render(<Composer onSend={async () => {}} onStop={() => {}} busy />)
    expect(button(el, 'Send')).toBeUndefined()
    expect(button(el, 'Stop')).toBeDefined()
  })

  test('stopDisabled disables Stop', async () => {
    const el = await render(<Composer onSend={async () => {}} onStop={() => {}} busy stopDisabled />)
    expect(button(el, 'Stop').disabled).toBe(true)
  })

  test('textareaRef, buttons, titles, icons and unstyled classNames', async () => {
    const ref = createRef<HTMLTextAreaElement>()
    const el = await render(
      <Composer
        onSend={async () => {}}
        onAttach={async () => []}
        textareaRef={ref}
        unstyled
        classNames={{ root: 'h-root', input: 'h-input', send: 'h-send', attach: 'h-attach', row: 'h-row' }}
        buttons={{ send: 'Send it', attach: 'Add files' }}
        titles={{ send: 'Send now' }}
        icons={{ send: '↑', attach: '+' }}
      />,
    )
    expect(ref.current).toBe(ta(el))
    const form = el.querySelector('form')!
    expect(form.className).toContain('h-root')
    expect(form.className).not.toContain('ergo-chat__composer')
    expect(ta(el).className).toBe('h-input')
    const send = el.querySelector<HTMLButtonElement>('button[type=submit]')!
    expect(send.className).toBe('h-send')
    expect(send.getAttribute('aria-label')).toBe('Send it')
    expect(send.title).toBe('Send now')
    expect(send.textContent).toBe('↑')
    expect(el.querySelector('[aria-label="Add files"]')!.textContent).toBe('+')
    expect(el.querySelector('input[type=file]')!.hasAttribute('hidden')).toBe(true)
  })

  test('pasting files uploads through onAttach; pasting text is left alone', async () => {
    const onAttach = vi.fn(async (files: File[]) => files.map(f => ({ id: f.name, name: f.name })))
    const el = await render(<Composer onSend={async () => {}} onAttach={onAttach} />)
    const paste = (files: File[]) => {
      const event = new Event('paste', { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'clipboardData', { value: { files } })
      act(() => void ta(el).dispatchEvent(event))
      return event
    }
    expect(paste([]).defaultPrevented).toBe(false)
    expect(paste([new File(['x'], 'shot.png')]).defaultPrevented).toBe(true)
    await flush()
    expect(onAttach).toHaveBeenCalledTimes(1)
    expect(el.textContent).toContain('shot.png')
  })

  test('renderPending replaces the chip and remove() takes the normal path', async () => {
    const onRemove = vi.fn(async () => {})
    const el = await render(
      <Composer
        onSend={async () => {}}
        attachments={[{ id: 'a', name: 'a.txt' }]}
        onAttachmentsChange={() => {}}
        onRemoveAttachment={onRemove}
        renderPending={(file, remove) => (
          <button type="button" onClick={remove}>
            drop {file.name}
          </button>
        )}
      />,
    )
    await click(button(el, 'drop a.txt'))
    await flush()
    expect(onRemove).toHaveBeenCalledWith({ id: 'a', name: 'a.txt' })
  })
})
