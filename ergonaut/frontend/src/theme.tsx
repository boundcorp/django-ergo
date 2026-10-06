import { useEffect, useState } from 'react'

// Dark (the design) or light, remembered per browser. index.html applies it before first paint.
export type Theme = 'dark' | 'light'

const KEY = 'ergonaut-theme'
const EVENT = 'ergonaut-theme'

export function currentTheme(): Theme {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

export function setTheme(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    // private mode: the choice lasts this page view
  }
  window.dispatchEvent(new Event(EVENT))
}

export function useTheme(): Theme {
  const [theme, set] = useState(currentTheme)
  useEffect(() => {
    const update = () => set(currentTheme())
    window.addEventListener(EVENT, update)
    return () => window.removeEventListener(EVENT, update)
  }, [])
  return theme
}

// A URL with ?theme= added, for pages (bots.pages) shown in the app.
export function themed(url: string, theme: Theme): string {
  return `${url}${url.includes('?') ? '&' : '?'}theme=${theme}`
}

export function ThemeToggle() {
  const theme = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      className="theme-toggle rounded-md border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 hover:text-zinc-900 dark:border-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-100"
      title={`Switch to ${next} mode`}
      aria-label={`Switch to ${next} mode`}
      onClick={() => setTheme(next)}
    >
      <span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span>
      <span className="theme-toggle-label">{theme === 'dark' ? ' Light' : ' Dark'}</span>
    </button>
  )
}
