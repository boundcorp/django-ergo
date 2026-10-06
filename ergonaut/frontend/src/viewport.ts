import { useEffect } from 'react'

/**
 * Keeps the app shell exactly as tall as what the reader can see.
 *
 * Browsers that resize the layout viewport for the on-screen keyboard (Chrome with
 * `interactive-widget=resizes-content`) are already covered by `100dvh`. iOS Safari shrinks only
 * the visual viewport and scrolls it to the focused field, so the shell follows the visual
 * viewport's height and offset (`--app-height`, `--app-top`) and the composer stays above the
 * keyboard instead of under it. Pinch-zoom also changes the visual viewport, so it is ignored
 * while zoomed.
 */
export function useVisualViewport(): void {
  useEffect(() => {
    const viewport = window.visualViewport
    if (!viewport) return
    const root = document.documentElement
    const update = () => {
      if (viewport.scale > 1.01) return
      root.style.setProperty('--app-height', `${viewport.height}px`)
      root.style.setProperty('--app-top', `${viewport.offsetTop}px`)
    }
    update()
    viewport.addEventListener('resize', update)
    viewport.addEventListener('scroll', update)
    return () => {
      viewport.removeEventListener('resize', update)
      viewport.removeEventListener('scroll', update)
      root.style.removeProperty('--app-height')
      root.style.removeProperty('--app-top')
    }
  }, [])
}
