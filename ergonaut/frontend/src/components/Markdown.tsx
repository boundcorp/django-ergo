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

// Documents can contain untrusted links. Reject browser-normalized network paths
// and control characters as well as schemes other than the explicitly allowed ones.
function documentUrl(url: string): string | undefined {
  if (/[\u0000-\u0020\u007f\\]/.test(url) || url.startsWith('//')) return undefined
  const scheme = /^([a-z][a-z\d+.-]*):/i.exec(url)
  if (scheme && !/^(https?|mailto)$/i.test(scheme[1])) return undefined
  return url
}

function DocumentLink({ href, children }: ComponentProps<'a'>) {
  if (href === undefined) return <>{children}</>
  const external = /^https?:/i.test(href)
  return (
    <a href={href} {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>
      {children}
    </a>
  )
}

function NoImage() {
  return null
}

/** Markdown with GitHub tables, task lists and strikethrough. Raw HTML is shown as text. */
export default function Markdown({
  text,
  className = '',
  mode = 'chat',
}: {
  text: string
  className?: string
  mode?: 'chat' | 'document'
}) {
  const document = mode === 'document'
  return (
    <div className={`markdown ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRawAsText]}
        urlTransform={document ? documentUrl : undefined}
        components={document ? { code: Code, a: DocumentLink, img: NoImage } : { code: Code, a: Link }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
