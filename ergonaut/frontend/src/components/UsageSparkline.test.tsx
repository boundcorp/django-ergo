import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import type { SessionUsage } from '../api'
import { UsageBars, barTitle, hourLabel } from './UsageSparkline'

let window: Window
let root: Root
let createRoot: typeof CreateRoot
let container: HTMLDivElement

const usage = (hours: [number, number][]): SessionUsage => ({
  until: '2026-10-29T16:00:00Z',
  hours: hours.map(([thread, agents], i) => ({
    start: `2026-10-29T${10 + i}:00:00Z`,
    thread_tokens: thread,
    agent_tokens: agents,
  })),
  thread_tokens: hours.reduce((n, [t]) => n + t, 0),
  agent_tokens: hours.reduce((n, [, a]) => n + a, 0),
})

beforeEach(async () => {
  window = new Window()
  Object.assign(globalThis, { window, document: window.document, IS_REACT_ACT_ENVIRONMENT: true })
  ;({ createRoot } = await import('react-dom/client'))
  container = window.document.createElement('div') as unknown as HTMLDivElement
  window.document.body.appendChild(container as never)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  window.close()
})

describe('UsageSparkline', () => {
  test('labels rolling hours, newest last', () => {
    expect(hourLabel(5, 6)).toBe('Last hour')
    expect(hourLabel(0, 6)).toBe('6–5h ago')
    expect(barTitle({ start: '', thread_tokens: 12_000, agent_tokens: 0 }, 4, 6)).toBe('2–1h ago: 12k thread')
    expect(barTitle({ start: '', thread_tokens: 900, agent_tokens: 1_500_000 }, 5, 6)).toBe(
      'Last hour: 900 thread · 1.5M agents',
    )
  })

  test('stacks thread and agent tokens, scaled to the busiest hour', () => {
    act(() =>
      root.render(
        createElement(UsageBars, {
          usage: usage([
            [0, 0],
            [0, 0],
            [5000, 0],
            [0, 0],
            [10_000, 12_000],
            [1000, 0],
          ]),
        }),
      ),
    )
    const bars = container.querySelectorAll('[data-hour]')
    expect(bars.length).toBe(6)
    const part = (i: number, name: string) =>
      (bars[i].querySelector(`[data-part="${name}"]`) as HTMLElement | null)?.style.height
    expect(part(0, 'thread')).toBeUndefined()
    expect(part(4, 'thread')).toBe('11px')
    expect(part(4, 'agents')).toBe('13px')
    expect(part(2, 'thread')).toBe('5px')
    expect(part(5, 'thread')).toBe('2px') // small but visible
    expect(container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
      '28k tokens in the last 6h (16k thread, 12k agents)',
    )
  })
})
