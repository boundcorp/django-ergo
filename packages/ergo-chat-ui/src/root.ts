import { createContext, useContext } from 'react'
import type { ChatTheme } from './types'

/** True inside `ChatView`, whose root already carries the tokens and theme. */
const InRoot = createContext(false)
export const RootProvider = InRoot.Provider

/** Class and attributes for a component's outermost element: the root (tokens, theme) unless it sits in a `ChatView`. */
export function useRoot(theme: ChatTheme, cls: string, extra: string) {
  const nested = useContext(InRoot)
  const className = `${nested ? '' : 'ergo-chat '}${cls} ${extra}`.trim()
  return nested ? { className } : { className, 'data-theme': theme }
}
