import * as React from 'react'
import { createRoot, type Root } from 'react-dom/client'
import * as TestUtils from 'react-dom/test-utils'
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

// React 18.2 only has act in test-utils; 18.3+ and 19 export it from react.
export const act: (fn: () => unknown) => Promise<void> | void =
  (React as unknown as { act?: typeof TestUtils.act }).act ?? TestUtils.act

const mounted: { root: Root; el: HTMLElement }[] = []

export async function render(node: React.ReactNode) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  await act(() => root.render(node))
  mounted.push({ root, el })
  return el
}

export function cleanup() {
  for (const { root, el } of mounted.splice(0)) {
    act(() => root.unmount())
    el.remove()
  }
}

export async function click(el: Element) {
  await act(() => (el as HTMLElement).click())
}

export async function type(el: HTMLTextAreaElement, value: string) {
  await act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
    setter.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

export async function key(el: Element, init: KeyboardEventInit) {
  await act(() => {
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }))
  })
}

export async function flush() {
  await act(async () => {
    await new Promise(r => setTimeout(r, 0))
  })
}
