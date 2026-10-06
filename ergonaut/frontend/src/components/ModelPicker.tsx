import { useEffect, useState } from 'react'
import type { BotModels } from '../api'
import { api } from '../api'

/** A bot's model choices from providers.yaml; null until they load, or when it has none. An empty bot waits. */
export function useBotModels(bot: string): BotModels | null {
  const [models, setModels] = useState<BotModels | null>(null)
  useEffect(() => {
    if (!bot) return
    let current = true
    api
      .models(bot)
      .then(m => current && setModels(m.models.length ? m : null))
      .catch(() => current && setModels(null))
    return () => {
      current = false
    }
  }, [bot])
  return models
}

function defaultLabel(models: BotModels): string {
  return models.models.find(m => m.id === models.default || m.name === models.default)?.label ?? models.default
}

/** The chat's model in words: "Sonnet 5.5", or the bot's default when the chat has none picked. */
export function modelLabel(models: BotModels, value: string): string {
  if (!value) return `${defaultLabel(models)} (default)`
  return models.models.find(m => m.id === value)?.label ?? value
}

// Pick the model a chat uses, from providers.yaml. Picking a model on another engine converts
// the chat's history to that engine, so any available model works in any chat.
export default function ModelPicker({
  models,
  value,
  onPick,
  large,
}: {
  models: BotModels | null
  value: string
  onPick: (model: string) => void
  large?: boolean
}) {
  if (!models) return null
  const fallback = defaultLabel(models)
  const select = (
    <select
      value={value}
      title="Model and effort for this chat"
      aria-label="Model and effort"
      className={
        large
          ? 'min-w-0 max-w-full rounded-control border-0 bg-transparent py-0.5 text-sm font-semibold'
          : 'max-w-[12rem] rounded-md border border-zinc-300 bg-transparent px-1.5 py-0.5 text-xs text-zinc-600 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-400'
      }
      onChange={e => onPick(e.target.value)}
    >
      <option value="">Default{fallback ? ` (${fallback})` : ''}</option>
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
