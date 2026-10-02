import { useEffect, useState } from 'react'
import type { BotModels } from '../api'
import { api } from '../api'

// Pick the model a chat uses, from providers.yaml. A chat keeps its engine (its messages are
// stored per engine), so models of another engine are shown but only a new thread can use them.
export default function ModelPicker({
  bot,
  value,
  engineType,
  onPick,
}: {
  bot: string
  value: string
  engineType?: string
  onPick: (model: string) => void
}) {
  const [models, setModels] = useState<BotModels | null>(null)

  useEffect(() => {
    let current = true
    api
      .models(bot)
      .then(m => current && setModels(m))
      .catch(() => current && setModels(null))
    return () => {
      current = false
    }
  }, [bot])

  if (!models?.models.length) return null
  const defaultLabel = models.models.find(m => m.id === models.default)?.label ?? models.default
  return (
    <select
      value={value}
      title="Model for this chat"
      className="max-w-[12rem] rounded-md border border-zinc-300 bg-transparent px-1.5 py-0.5 text-xs text-zinc-600 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-400"
      onChange={e => onPick(e.target.value)}
    >
      <option value="">Default{defaultLabel ? ` (${defaultLabel})` : ''}</option>
      {models.models.map(m => {
        const otherEngine = !!engineType && m.engine_type !== engineType
        const why = !m.available ? ' — no API key' : otherEngine ? ' — new thread only' : ''
        return (
          <option key={m.id} value={m.id} disabled={!m.available || otherEngine}>
            {m.provider} · {m.label}
            {why}
          </option>
        )
      })}
    </select>
  )
}
