import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import '@fontsource/inter/400.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/space-grotesk/700.css'
import '../src/index.css'
import { AttachmentView } from '../src/components/MediaGallery'
import { Transcript } from '../src/components/Transcript'
import type { Message } from '../src/api'
import './visual.css'

const initial: Message[] = [
  { line: 1, role: 'user', timestamp: null, blocks: [{ type: 'text', text: 'Could you summarize the release plan and keep the original document handy?' }] },
  { line: 2, role: 'assistant', timestamp: null, blocks: [{ type: 'text', text: 'Here is the synthetic release plan. Open the attachment to read the full document without leaving the conversation.' }] },
  { line: 3, role: 'user', timestamp: null, blocks: [{ type: 'text', text: 'Thanks. Please include the decision table and the implementation notes.' }] },
  { line: 4, role: 'assistant', timestamp: null, blocks: [{ type: 'text', text: 'The document below has the milestones, checklist, and a code example.' }] },
]

function Harness() {
  const [updates, setUpdates] = useState(0)
  // The browser check simulates a transcript update while the modal is open.
  useEffect(() => {
    const append = () => setUpdates(n => n + 1)
    window.addEventListener('synthetic-update', append)
    return () => window.removeEventListener('synthetic-update', append)
  }, [])
  const messages: Message[] = [...initial, ...Array.from({ length: updates }, (_, i): Message => ({
    line: 10 + i, role: 'assistant', timestamp: null,
    blocks: [{ type: 'text', text: `Synthetic live update ${i + 1}: the release plan remains ready for review.` }],
  }))]
  return <div className="harness-shell">
    <header className="harness-header"><strong>Ergonaut · Release planning</strong><span>Simplified harness chrome · synthetic data</span></header>
    <main className="harness-scroll" data-testid="transcript-scroll">
      <div className="harness-transcript">
        <Transcript messages={messages.slice(0, 4)} calls={[]} sessionId="fixture" />
        <div className="harness-card"><AttachmentView id="fixture-markdown" label="release-plan.md" media_type="text/markdown" /></div>
        <Transcript messages={messages.slice(4)} calls={[]} sessionId="fixture" />
        <div className="harness-filler">End of synthetic conversation</div>
      </div>
    </main>
    <footer className="harness-composer">Message the assistant… <span>Send</span></footer>
  </div>
}

createRoot(document.getElementById('root')!).render(<MemoryRouter><Harness /></MemoryRouter>)
