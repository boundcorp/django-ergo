import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@ergo/chat-ui/styles.css'
import Demo from './Demo'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Demo />
  </StrictMode>,
)
