import type { ComponentProps } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { highlight } from './CodeView'
import { ThreadLink } from './Threads'

type Node = { type: string; value?: string; children?: Node[] }

// react-markdown drops raw HTML; show it as the text it was (a bot writing "<board>" means the text).
function rehypeRawAsText() {
  const walk = (node: Node) => {
    if (node.type === 'raw') node.type = 'text'
    node.children?.forEach(walk)
  }
  return walk
}

// Code blocks are highlighted like files (CodeView); inline code stays plain.
function Code({ className, children, ...props }: ComponentProps<'code'>) {
  const language = /language-([\w+-]+)/.exec(className ?? '')?.[1]
  const code = String(children ?? '').replace(/\n$/, '')
  const html = language ? highlight(code, language) : null
  if (html)
    // hljs escapes the text; the only markup is its own spans.
    return <code className={`${className} hljs-view`} dangerouslySetInnerHTML={{ __html: html }} />
  return (
    <code className={className} {...props}>
      {children}
    </code>
  )
}

function Link({ href, children, ...props }: ComponentProps<'a'>) {
  // Another chat (linkThreads): a chip with its status.
  const thread = /^\/s\/([0-9a-f-]{36})$/.exec(href ?? '')
  if (thread) return <ThreadLink id={thread[1]}>{children}</ThreadLink>
  const external = !!href && /^https?:/.test(href)
  return (
    <a href={href} {...(external ? { target: '_blank', rel: 'noreferrer' } : {})} {...props}>
      {children}
    </a>
  )
}

/** Markdown (with GitHub tables, task lists and strikethrough). Raw HTML in the text is shown as text. */
export default function Markdown({ text, className = '' }: { text: string; className?: string }) {
  return (
    <div className={`markdown ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRawAsText]} components={{ code: Code, a: Link }}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
