import { useState } from 'react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { ChatView, Composer, type Attachment } from '../src'
import { act, cleanup, click, flush, key, render, type } from './helpers'

afterEach(cleanup)
const ta = (el: HTMLElement) => el.querySelector('textarea')!
const button = (el: HTMLElement, text: string) => [...el.querySelectorAll('button')].find(b => b.textContent === text)!

describe('Composer', () => {
  test('Enter sends trimmed text and clears; Shift+Enter and IME Enter do not send', async () => {
    const onSend = vi.fn(async () => {})
    const el = await render(<Composer onSend={onSend} />)
    expect(button(el, 'Send').disabled).toBe(true)
    await type(ta(el), '  hello  ')
    await key(ta(el), { key: 'Enter', shiftKey: true })
    await key(ta(el), { key: 'Enter', isComposing: true } as KeyboardEventInit)
    expect(onSend).not.toHaveBeenCalled()
    await key(ta(el), { key: 'Enter' })
    await flush()
    expect(onSend).toHaveBeenCalledWith('hello', [])
    expect(ta(el).value).toBe('')
  })

  test('a rejected send keeps the draft and attachments and shows an alert', async () => {
    const stored: Attachment = { id: 'f1', name: 'a.txt' }
    const onSend = vi.fn().mockRejectedValue(new Error('network down'))
    const el = await render(<Composer onSend={onSend} onAttach={async () => [stored]} />)
    await type(ta(el), 'important')
    await act(() => {
      const input = el.querySelector<HTMLInputElement>('input[type=file]')!
      Object.defineProperty(input, 'files', { value: [new File(['x'], 'a.txt')], configurable: true })
      input.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await flush()
    expect(el.textContent).toContain('a.txt')
    await click(button(el, 'Send'))
    await flush()
    expect(el.querySelector('[role=alert]')?.textContent).toBe('network down')
    expect(ta(el).value).toBe('important')
    expect(el.textContent).toContain('a.txt')
  })

  test('attach resolves into pending attachments that are sent; remove calls the host first', async () => {
    const onSend = vi.fn(async () => {})
    const onRemove = vi.fn(async () => {})
    const el = await render(
      <Composer
        onSend={onSend}
        onRemoveAttachment={onRemove}
        onAttach={async files => files.map((f, i) => ({ id: `id${i}`, name: f.name }))}
      />,
    )
    await act(() => {
      const input = el.querySelector<HTMLInputElement>('input[type=file]')!
      Object.defineProperty(input, 'files', {
        value: [new File(['x'], 'one.txt'), new File(['y'], 'two.txt')],
        configurable: true,
      })
      input.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await flush()
    await click(el.querySelector('[aria-label="Remove one.txt"]')!)
    await flush()
    expect(onRemove).toHaveBeenCalledWith({ id: 'id0', name: 'one.txt' })
    await click(button(el, 'Send')) // attachments alone are enough to send
    await flush()
    expect(onSend).toHaveBeenCalledWith('', [{ id: 'id1', name: 'two.txt' }])
  })

  test('a failed remove keeps the attachment', async () => {
    const el = await render(
      <Composer
        onSend={() => {}}
        attachments={[{ id: 'k', name: 'keep.txt' }]}
        onRemoveAttachment={() => Promise.reject(new Error('locked'))}
      />,
    )
    await click(el.querySelector('[aria-label="Remove keep.txt"]')!)
    await flush()
    expect(el.textContent).toContain('keep.txt')
    expect(el.querySelector('[role=alert]')?.textContent).toBe('locked')
  })

  test('controlled draft', async () => {
    function Host() {
      const [draft, setDraft] = useState('start')
      return <Composer onSend={() => {}} draft={draft} onDraftChange={t => setDraft(t.toUpperCase())} />
    }
    const el = await render(<Host />)
    expect(ta(el).value).toBe('start')
    await type(ta(el), 'abc')
    expect(ta(el).value).toBe('ABC')
  })

  test('busy with onStop shows Stop and calls it', async () => {
    const onStop = vi.fn()
    const el = await render(<Composer onSend={() => {}} onStop={onStop} busy />)
    expect(button(el, 'Send')).toBeUndefined()
    await click(button(el, 'Stop'))
    expect(onStop).toHaveBeenCalledOnce()
  })

  test('no attach button without onAttach', async () => {
    const el = await render(<Composer onSend={() => {}} />)
    expect(el.querySelector('[aria-label="Attach files"]')).toBeNull()
  })
})

describe('ChatView', () => {
  test('wires status to the composer and shows the error', async () => {
    const onStop = vi.fn()
    const el = await render(
      <ChatView messages={[]} status="streaming" onSend={() => {}} onStop={onStop} empty="Say hi" />,
    )
    expect(button(el, 'Stop')).toBeDefined()
    expect(el.querySelectorAll('.ergo-chat')).toHaveLength(1) // one token root
    const err = await render(<ChatView messages={[]} status="error" error="Lost connection" onSend={() => {}} />)
    expect(err.querySelector('[role=alert]')?.textContent).toBe('Lost connection')
  })
})
