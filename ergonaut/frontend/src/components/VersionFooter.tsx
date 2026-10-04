import { useEffect, useState } from 'react'

import { api, type Version } from '../api'
import { ago } from '../time'

function short(sha: string) {
  return /^[0-9a-f]{40}$/.test(sha) ? sha.slice(0, 7) : sha
}

function day(iso: string) {
  return iso ? new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }) : ''
}

// The running version and its date at the bottom of the sidebar, a notice when a
// newer one is out, and for admins what the last upgrade check did.
export default function VersionFooter() {
  const [version, setVersion] = useState<Version | null>(null)

  useEffect(() => {
    let live = true
    const load = () =>
      api
        .version()
        .then(v => live && setVersion(v))
        .catch(() => {})
    load()
    const timer = window.setInterval(load, 5 * 60 * 1000)
    return () => {
      live = false
      window.clearInterval(timer)
    }
  }, [])

  if (!version) return null
  const commitUrl = version.commit ? `https://github.com/${version.repo}/commit/${version.commit}` : undefined
  const latest = version.latest
  const check = version.last_check
  const attempt = version.last_attempt
  const failed = attempt?.status === 'failed'

  return (
    <div className="space-y-1 px-3 pt-2 text-xs text-muted">
      {version.available && latest && (
        <a
          href={latest.url || undefined}
          target="_blank"
          rel="noreferrer"
          className="block rounded-md border border-accent/40 bg-indigo-tint px-2 py-1.5 text-ink hover:bg-raised"
          title={latest.name || latest.tag}
        >
          <span className="font-semibold">New version available</span>
          <br />
          {latest.tag === 'main' || !latest.tag ? short(latest.sha) : latest.tag}
          {latest.date && ` · ${day(latest.date)}`}
          {version.upgrader && version.auto_seconds ? (
            <span className="block text-muted">Installs automatically once nothing is running.</span>
          ) : version.upgrader !== undefined ? (
            <span className="block text-muted">
              {version.upgrader ? 'Run ergonaut upgrade to install it.' : 'No upgrader is set (ERGONAUT_UPGRADER).'}
            </span>
          ) : null}
        </a>
      )}
      <div title={version.date ? new Date(version.date).toLocaleString() : undefined}>
        Ergonaut{' '}
        {commitUrl ? (
          <a href={commitUrl} target="_blank" rel="noreferrer" className="font-mono hover:underline">
            {short(version.commit)}
          </a>
        ) : (
          'unknown version'
        )}
        {version.date && ` · ${day(version.date)}`}
      </div>
      {check?.at && (
        <div title={check.result} className="truncate">
          Checked {ago(new Date(check.at * 1000).toISOString())}: {check.result}
        </div>
      )}
      {failed && (
        <div title={attempt.error} className="text-danger">
          Upgrade to {attempt.tag} failed: {attempt.error.slice(0, 160)}
        </div>
      )}
      {version.error && <div title={version.error}>Couldn't reach GitHub: {version.error.slice(0, 120)}</div>}
    </div>
  )
}
