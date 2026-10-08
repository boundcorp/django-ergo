import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react'
import { Link } from 'react-router-dom'
import type { User, Worker } from '../api'
import { ThemeToggle } from '../theme'

const menuItemSelector = '[role="menuitem"]'

export function nextMenuIndex(key: string, current: number, count: number): number | null {
  if (!count) return null
  if (key === 'ArrowDown') return (current + 1) % count
  if (key === 'ArrowUp') return (current - 1 + count) % count
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  return null
}

export function useDismissablePopover(
  open: boolean,
  onClose: (restoreFocus: boolean) => void,
  trigger: RefObject<HTMLButtonElement | null>,
  panel: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    if (!open) return
    const outside = (event: MouseEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node))
        onClose(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose(true)
    }
    document.addEventListener('mousedown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [open, onClose, panel, trigger])
}

function focusMenuItem(event: ReactKeyboardEvent<HTMLElement>, close: () => void) {
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  const items = [...event.currentTarget.closest('[role="menu"]')!.querySelectorAll<HTMLElement>(menuItemSelector)]
  const target = event.target as HTMLElement
  const next = nextMenuIndex(event.key, Math.max(0, items.indexOf(target)), items.length)
  if (next == null) return
  event.preventDefault()
  items[next]?.focus()
}

export function AccountMenu({
  user,
  onSignOut,
  initialOpen = false,
}: {
  user: User
  onSignOut: () => void
  initialOpen?: boolean
}) {
  const [open, setOpen] = useState(initialOpen)
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const name = user.first_name || user.username
  const initial = name.trim().charAt(0).toUpperCase() || 'U'
  const close = (restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) requestAnimationFrame(() => trigger.current?.focus())
  }
  useDismissablePopover(open, close, trigger, panel)

  return (
    <div className="shell-popover">
      <button
        ref={trigger}
        type="button"
        className="account-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={id}
        aria-label={`Account menu for ${name}`}
        onClick={() => setOpen(value => !value)}
        onKeyDown={event => {
          if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
          event.preventDefault()
          setOpen(true)
          requestAnimationFrame(() => {
            const items = panel.current?.querySelectorAll<HTMLElement>(menuItemSelector)
            items?.[event.key === 'End' || event.key === 'ArrowUp' ? items.length - 1 : 0]?.focus()
          })
        }}
      >
        <span className="account-avatar" aria-hidden="true">
          {initial}
        </span>
        <span className="account-name">{name}</span>
        <span className="account-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <div
          ref={panel}
          id={id}
          className="account-menu"
          role="menu"
          aria-label="Account menu"
          onKeyDown={event => focusMenuItem(event, () => close(true))}
        >
          <div className="account-identity" role="none">
            <div className="font-semibold text-ink">{name}</div>
            {user.email && <div className="truncate text-xs text-muted">{user.email}</div>}
          </div>
          <div className="account-menu-group" role="none">
            <Link role="menuitem" to="/sessions" onClick={() => close()}>
              History
            </Link>
            <Link role="menuitem" to="/costs" onClick={() => close()}>
              Costs
            </Link>
            <Link role="menuitem" to="/routing" onClick={() => close()}>
              Routing
            </Link>
            <Link role="menuitem" to="/api-keys" onClick={() => close()}>
              API keys
            </Link>
          </div>
          <div className="account-menu-group" role="none">
            <ThemeToggle menuItem />
            <button type="button" role="menuitem" onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

type WorkerState = { state: 'loading' | 'ready' | 'error'; workers: Worker[] }

export function WorkerStatus({
  data,
  onRetry,
  initialOpen = false,
}: {
  data: WorkerState
  onRetry: () => void
  initialOpen?: boolean
}) {
  const [open, setOpen] = useState(initialOpen)
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const active = data.workers.filter(worker => worker.status === 'queued' || worker.status === 'running')
  const running = active.filter(worker => worker.status === 'running').length
  const queued = active.length - running
  const close = (restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) requestAnimationFrame(() => trigger.current?.focus())
  }
  useDismissablePopover(open, close, trigger, panel)

  if (data.state === 'loading' && !active.length)
    return (
      <span className="sr-only" aria-live="polite">
        Checking active work
      </span>
    )
  if (data.state === 'ready' && !active.length) return null
  const label = data.state === 'error' ? 'Worker status unavailable' : `Working · ${active.length} active`

  return (
    <div className="shell-popover worker-status">
      <button
        ref={trigger}
        type="button"
        className="worker-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(value => !value)}
      >
        <span className="worker-mark" aria-hidden="true">
          ●
        </span>
        {label}
      </button>
      {open && (
        <div ref={panel} id={id} className="worker-popover" role="dialog" aria-label="Active work">
          {data.state === 'error' ? (
            <div className="space-y-3">
              <p className="m-0 text-sm text-ink">Worker status is unavailable.</p>
              <button type="button" className="shell-action" onClick={onRetry}>
                Retry
              </button>
            </div>
          ) : !active.length ? (
            <p className="m-0 text-sm text-muted">No active work.</p>
          ) : (
            <ul className="m-0 list-none space-y-2 p-0">
              {active.map(worker => (
                <li key={worker.id} className="worker-row">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-ink">{worker.title}</div>
                    <div className="text-xs text-muted">{worker.status === 'queued' ? 'Queued' : 'Running'}</div>
                  </div>
                  {worker.progress && <span className="text-xs text-muted">{worker.progress}</span>}
                </li>
              ))}
            </ul>
          )}
          {data.state === 'ready' && active.length > 0 && (
            <p className="sr-only">
              {running} running, {queued} queued
            </p>
          )}
        </div>
      )}
    </div>
  )
}
