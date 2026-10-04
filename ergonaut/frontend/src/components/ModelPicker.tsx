import { useEffect, useState } from 'react'
import type { BotModels } from '../api'
import { api } from '../api'

// Pick the model a chat uses, from providers.yaml. Picking a model on another engine converts
// the chat's history to that engine, so any available model works in any chat.
export default function ModelPicker({
  bot,
  value,
  onPick,
  large,
}: {
  bot: string
  value: string
  onPick: (model: string) => void
  large?: boolean
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
  const defaultLabel =
    models.models.find(m => m.id === models.default || m.name === models.default)?.label ?? models.default
  const select = (
    <select
      value={value}
      title="Model for this chat"
      aria-label={large ? 'Model' : undefined}
      className={
        large
          ? 'min-w-0 max-w-full rounded-control border-0 bg-transparent py-0.5 text-sm font-semibold'
          : 'max-w-[12rem] rounded-md border border-zinc-300 bg-transparent px-1.5 py-0.5 text-xs text-zinc-600 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-400'
      }
      onChange={e => onPick(e.target.value)}
    >
      <option value="">Default{defaultLabel ? ` (${defaultLabel})` : ''}</option>
      {models.models.map(m => (
        <option key={m.id} value={m.id} disabled={!m.available}>
          {m.provider} · {m.label}
          {m.available ? '' : ' — no API key'}
        </option>
      ))}
    </select>
  )
  return large ? (
    <div className="flex items-center gap-2 rounded-card border border-stroke bg-surface px-4 py-3 text-sm font-semibold">
      <span aria-hidden="true">Model ·</span>
      {select}
    </div>
  ) : (
    select
  )
}
