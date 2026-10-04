import { useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import ModelPicker from '../components/ModelPicker'

// Start a thread by writing its first message (with files). The thread gets a title from the
// message right away, and a generated one (new_thread_metadata) a moment later.
export function NewThread({ onChange }: { onChange: () => void }) {
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const [text, setText] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState('')
  const [error, setError] = useState('')
  const picker = useRef<HTMLInputElement>(null)

  function add(list: FileList | File[] | null) {
    if (list?.length) setFiles(current => [...current, ...Array.from(list)])
    if (picker.current) picker.current.value = ''
  }

  async function start() {
    if ((!text.trim() && !files.length) || busy) return
    setBusy(true)
    setError('')
    try {
      const message = text.trim() || `(${files.map(f => f.name).join(', ')})`
      const thread = await api.newThread(name, '', message, model)
      const ids = []
      for (const file of files) ids.push((await api.uploadAttachment(thread.id, file)).id)
      await api.send(thread.id, text, ids)
      onChange()
      navigate(`/s/${thread.id}`)
      // The generated title lands a moment later; refresh the sidebar for it.
      setTimeout(onChange, 4000)
    } catch (e) {
      setError(String((e as Error).message ?? e))
      setBusy(false)
    }
  }

  return (
    <div className="page-content h-full overflow-y-auto">
      <p className="eyebrow">New thread</p>
      <h1 className="page-title mt-3">New thread / {name}</h1>
      <p className="page-lede mt-3">Set the model, write your first prompt, and add context files before starting.</p>
      <div className="mt-8 flex flex-wrap items-center gap-3">
        <ModelPicker bot={name} value={model} onPick={setModel} large />
      </div>
      {!!files.length && (
        <div className="mb-2 flex flex-wrap gap-2">
          {files.map((file, i) => (
            <span
              key={`${file.name}-${i}`}
              className="flex items-center gap-1 rounded-full border border-zinc-300 px-2 py-0.5 text-xs dark:border-zinc-700"
            >
              📎 {file.name}
              <button
                className="text-zinc-400 hover:text-red-600"
                title="Remove"
                onClick={() => setFiles(current => current.filter((_, j) => j !== i))}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
      <p className="eyebrow mt-8">Your first message</p>
      <form
        className="surface-card mt-3 max-w-3xl p-4"
        onDragOver={e => e.preventDefault()}
        onDrop={e => {
          e.preventDefault()
          add(e.dataTransfer.files)
        }}
        onSubmit={e => {
          e.preventDefault()
          start()
        }}
      >
        <input ref={picker} type="file" multiple hidden onChange={e => add(e.target.files)} />
        {/* Voice input goes here. */}
        <textarea
          autoFocus
          value={text}
          disabled={busy}
          onChange={e => setText(e.target.value)}
          onPaste={e => {
            const pasted = Array.from(e.clipboardData.files)
            if (pasted.length) {
              e.preventDefault()
              add(pasted)
            }
          }}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              start()
            }
          }}
          rows={4}
          aria-label="First message"
          placeholder="What's this thread about?"
          className="block w-full resize-none rounded-card border border-stroke bg-raised px-4 py-3 focus:outline-none"
        />
        <div className="mt-3 flex items-center gap-3 border-t border-stroke pt-3">
          <button
            type="button"
            disabled={busy}
            title="Attach images, PDFs or other files"
            className="rounded-control px-2 py-2 text-sm font-semibold text-mint hover:bg-raised disabled:opacity-50"
            onClick={() => picker.current?.click()}
          >
            + Attach
          </button>
          <button
            disabled={busy || (!text.trim() && !files.length)}
            className="ml-auto rounded-control bg-accent px-5 py-2.5 font-semibold text-canvas disabled:opacity-50"
          >
            {busy ? 'Starting…' : 'Send'}
          </button>
        </div>
      </form>
      <button
        type="button"
        className="mt-6 flex min-h-20 w-full max-w-sm flex-col items-center justify-center rounded-card border border-accent bg-indigo-tint/50 px-4 py-3 text-sm font-semibold text-ink hover:bg-indigo-tint"
        onClick={() => picker.current?.click()}
      >
        Drop files here to attach
        <span className="mt-1 text-xs font-normal text-muted">or browse from your device</span>
      </button>
      {busy && (
        <p
          role="status"
          className="mt-6 inline-flex items-center gap-3 rounded-card border border-mint bg-surface px-4 py-3 text-sm font-semibold"
        >
          <span className="h-3 w-3 rounded-full bg-indigo-500" aria-hidden="true" />
          Working · generating reply
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="mt-6 flex max-w-xl flex-wrap items-center gap-3 rounded-card border border-danger bg-surface px-4 py-3 text-sm"
        >
          <span className="h-3 w-3 shrink-0 rounded-full bg-danger" aria-hidden="true" />
          <span className="font-semibold">Request failed</span>
          <button type="button" className="font-semibold text-accent hover:underline" onClick={start}>
            Retry
          </button>
          <span className="min-w-0 flex-1 truncate text-muted">{error}</span>
        </div>
      )}
    </div>
  )
}
