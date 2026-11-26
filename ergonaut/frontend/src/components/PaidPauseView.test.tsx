import { afterEach, beforeEach, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api, type PoolSettings } from '../api'
import { PoolsSection } from './Pools'

const snapshot: PoolSettings = {
  legacy: false,
  pools: [],
  active: '',
  mode: 'auto',
  admin: true,
  paid_paused: true,
  paid_pause_version: 'old',
}
let win: Window
let root: Root
let container: HTMLDivElement
const oldPools = api.pools
const oldPause = api.setPaidPause

beforeEach(async () => {
  win = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window: win,
    document: win.document,
    navigator: win.navigator,
    HTMLElement: win.HTMLElement,
    Event: win.Event,
    MouseEvent: win.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  win.confirm = () => true
  container = win.document.createElement('div')
  win.document.body.append(container)
  root = (await import('react-dom/client')).createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  api.pools = oldPools
  api.setPaidPause = oldPause
  win.happyDOM.close()
})

test('stale pause error remains visible after refreshing the state', async () => {
  let reads = 0
  api.pools = async () => ({ ...snapshot, paid_pause_version: ++reads === 1 ? 'old' : 'new' })
  api.setPaidPause = async (_paused, version) => {
    expect(version).toBe('old')
    throw new Error('Paid dispatch state changed; refresh and try again')
  }
  await act(async () => {
    root.render(createElement(PoolsSection))
  })
  const button = Array.from(container.querySelectorAll('button')).find(b => b.textContent === 'Resume paid dispatch')
  expect(button).toBeDefined()
  await act(async () => {
    button!.click()
  })
  expect(reads).toBe(2)
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('state changed')
})
