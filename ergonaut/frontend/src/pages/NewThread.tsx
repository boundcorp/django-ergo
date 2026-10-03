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
      <h1 className="page-title mb-1">New thread with {name}</h1>
      <p className="page-lede mb-6 flex flex-wrap items-center gap-3">
        <span>Say what it's about; the thread gets its title from your message.</span>
        <span className="block basis-full">
          <ModelPicker bot={name} value={model} onPick={setModel} />
        </span>
      </p>
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
      <form
        className="surface-card flex max-w-3xl flex-wrap items-end gap-3 p-4"
        onSubmit={e => {
          e.preventDefault()
          start()
        }}
      >
        <input ref={picker} type="file" multiple hidden onChange={e => add(e.target.files)} />
        <button
          type="button"
          disabled={busy}
          title="Attach images, PDFs or other files"
          className="rounded-control border border-stroke bg-raised px-3 py-2 text-lg disabled:opacity-50"
          onClick={() => picker.current?.click()}
        >
          📎
        </button>
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
          placeholder="What's this thread about?"
          className="min-w-48 flex-1 resize-none rounded-card border border-stroke bg-raised px-4 py-3 focus:outline-none"
        />
        <button
          disabled={busy || (!text.trim() && !files.length)}
          className="rounded-control bg-accent px-5 py-2.5 font-semibold text-canvas disabled:opacity-50"
        >
          {busy ? 'Starting…' : 'Send'}
        </button>
      </form>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </div>
  )
}
