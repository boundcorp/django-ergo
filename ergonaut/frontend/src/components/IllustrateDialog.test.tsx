import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import type { IllustrationResult, IllustrationSuggestions } from '../api'
import { api } from '../api'
import { IllustrateDialog, MAX_SELECTIONS, toggleSuggestion } from './IllustrateDialog'

const suggestions: IllustrationSuggestions = {
  snapshot: 'signed-snapshot',
  as_of_line: 12,
  suggestions: [
    {
      id: 'timeline',
      title: 'Timeline',
      purpose: 'Show events in order.',
      kind: 'timeline',
      evidence_refs: [10],
      availability: 'available',
      reason: 'The chat has decisions.',
    },
    {
      id: 'status',
      title: 'Status',
      purpose: 'Show current work.',
      kind: 'status',
      evidence_refs: [11],
      availability: 'available',
      reason: 'The chat has open work.',
    },
    {
      id: 'plan',
      title: 'Plan',
      purpose: 'Show the next steps.',
      kind: 'plan',
      evidence_refs: [12],
      availability: 'available',
      reason: 'The chat has a plan.',
    },
  ],
}

const illustration: IllustrationResult = {
  attachment: {
    id: 'file-1',
    filename: 'illustration.html',
    media_type: 'text/html',
    kind: 'document',
    size: 10,
    source: 'bot',
    message_sequence: null,
    pinned: false,
    view: 'html',
    created_at: '',
    updated_at: '',
    archived_at: null,
    link: null,
  },
  summary: 'A useful illustration',
  as_of_line: 12,
}

let window: Window
let root: Root
let createRoot: typeof CreateRoot
let container: HTMLDivElement
const originalSuggestions = api.illustrateSuggestions
const originalIllustrate = api.illustrate

function button(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!found) throw new Error(`Missing ${label} button`)
  return found as HTMLButtonElement
}

function inputCustom(value: string) {
  const input = container.querySelector('#illustrate-custom') as HTMLTextAreaElement
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set
  if (!setter) throw new Error('Textarea value setter is unavailable')
  act(() => {
    setter.call(input, value)
    input.dispatchEvent(new window.InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }))
    input.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
}

async function resolveSuggestions() {
  await act(async () => {
    await Promise.resolve()
  })
}

function render(disabled = false, onCreated = () => undefined) {
  act(() => {
    root.render(createElement(IllustrateDialog, { sessionId: 'thread-1', disabled, onCreated }))
  })
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    HTMLDialogElement: window.HTMLDialogElement,
    Event: window.Event,
    MouseEvent: window.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true
  }
  window.HTMLDialogElement.prototype.close = function () {
    this.open = false
  }
  // React DOM captures the document while it initializes; load it only after Happy DOM is installed.
  createRoot = (await import('react-dom/client')).createRoot
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  api.illustrateSuggestions = originalSuggestions
  api.illustrate = originalIllustrate
  window.happyDOM.close()
})

describe('IllustrateDialog', () => {
  test('loads suggestions, toggles checkboxes, and caps selections', async () => {
    api.illustrateSuggestions = async () => suggestions
    render()

    act(() => button('✦ Illustrate').click())
    expect(container.textContent).toContain('Finding evidence-grounded visual options…')
    await resolveSuggestions()

    const checkboxes = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
    expect(checkboxes).toHaveLength(MAX_SELECTIONS)
    expect(button('Create illustration').disabled).toBe(true)
    act(() => checkboxes[0].click())
    expect(checkboxes[0].checked).toBe(true)
    expect(button('Create illustration').disabled).toBe(false)
    act(() => checkboxes[0].click())
    expect(checkboxes[0].checked).toBe(false)
    expect(toggleSuggestion(['one', 'two', 'three'], 'four')).toEqual(['one', 'two', 'three'])
  })

  test('submits a custom-only request', async () => {
    api.illustrateSuggestions = async () => suggestions
    const created: IllustrationResult[] = []
    let request: unknown[] = []
    api.illustrate = async (...args) => {
      request = args
      return illustration
    }
    render(false, result => created.push(result))

    act(() => button('✦ Illustrate').click())
    await resolveSuggestions()
    inputCustom('Map the taco decision')
    act(() => button('Create illustration').click())
    await resolveSuggestions()

    expect(request).toEqual(['thread-1', 'signed-snapshot', [], 'Map the taco decision'])
    expect(created).toEqual([illustration])
    expect((container.querySelector('dialog') as HTMLDialogElement).open).toBe(false)
  })

  test('submits selected suggestions together with a custom request', async () => {
    api.illustrateSuggestions = async () => suggestions
    let request: unknown[] = []
    api.illustrate = async (...args) => {
      request = args
      return illustration
    }
    render()

    act(() => button('✦ Illustrate').click())
    await resolveSuggestions()
    act(() => (container.querySelector('input[type="checkbox"]') as HTMLInputElement).click())
    inputCustom('Emphasize the final choice')
    act(() => button('Create illustration').click())
    await resolveSuggestions()

    expect(request).toEqual(['thread-1', 'signed-snapshot', ['timeline'], 'Emphasize the final choice'])
  })

  test('disables the opener and reports a failed suggestion fetch', async () => {
    let calls = 0
    api.illustrateSuggestions = async () => {
      calls += 1
      throw new Error('Suggestions are unavailable')
    }
    render(true)
    expect(button('✦ Illustrate').disabled).toBe(true)
    act(() => button('✦ Illustrate').click())
    expect(calls).toBe(0)

    render()
    act(() => button('✦ Illustrate').click())
    await resolveSuggestions()
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Suggestions are unavailable')
    expect(button('Create illustration').disabled).toBe(true)
  })

  test('disables controls while generation is pending and recovers from failure', async () => {
    api.illustrateSuggestions = async () => suggestions
    let reject: (reason: Error) => void = () => undefined
    api.illustrate = async () =>
      new Promise<IllustrationResult>((_resolve, rejectRequest) => {
        reject = rejectRequest
      })
    render()

    act(() => button('✦ Illustrate').click())
    await resolveSuggestions()
    inputCustom('Map the decision')
    act(() => button('Create illustration').click())
    expect(button('Creating…').disabled).toBe(true)
    expect((container.querySelector('#illustrate-custom') as HTMLTextAreaElement).disabled).toBe(true)
    expect((container.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true)

    await act(async () => {
      reject(new Error('Model timed out'))
      await Promise.resolve()
    })
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Model timed out')
    expect(button('Create illustration').disabled).toBe(false)
  })
})
