import { Children, isValidElement, type ComponentProps, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CopyButton } from './CopyButton'
import { highlight } from './highlight'
import { safeLinkUrl } from './url'

function Link({ href, children }: ComponentProps<'a'>) {
  const safe = safeLinkUrl(href)
  if (!safe) return <span className="ergo-chat__md-unsafe">{children}</span>
  const external = /^https?:/i.test(safe)
  return (
    <a href={safe} {...(external ? { target: '_blank', rel: 'noopener noreferrer nofollow' } : {})}>
      {children}
    </a>
  )
}

function textOf(node: ReactNode): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

function Pre({ children }: ComponentProps<'pre'>) {
  const child = Children.toArray(children)[0]
  const className = isValidElement<{ className?: string }>(child) ? (child.props.className ?? '') : ''
  const language = /language-([\w+-]+)/.exec(className)?.[1]
  const code = textOf(children).replace(/\n$/, '')
  const html = language ? highlight(code, language) : null
  return (
    <div className="ergo-chat__code">
      {language && <span className="ergo-chat__code-lang">{language}</span>}
      <CopyButton text={code} label="code" className="ergo-chat__copy" />
      <pre tabIndex={0}>
        {html ? (
          // hljs escapes the text; the only markup is its own spans.
          <code className="ergo-chat__hljs" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <code>{code}</code>
        )}
      </pre>
    </div>
  )
}

/**
 * Markdown with GitHub tables, task lists and strikethrough.
 * Raw HTML is never rendered (react-markdown escapes it), links go through `safeLinkUrl`,
 * and images are dropped to their alt text unless `allowImages` is set.
 */
export function Markdown({
  text,
  allowImages = false,
  className = '',
}: {
  text: string
  allowImages?: boolean
  className?: string
}) {
  return (
    <div className={`ergo-chat__md ${className}`.trim()}>
      <MarkdownContent
        text={text}
        urlTransform={url => safeLinkUrl(url) ?? ''}
        components={{
          a: Link,
          pre: Pre,
          img: ({ src, alt }: ComponentProps<"img">) =>
            allowImages && typeof src === 'string' && /^https?:/i.test(src) ? (
              <img
                src={src}
                alt={alt ?? ''}
                loading="lazy"
                referrerPolicy="no-referrer"
                className="ergo-chat__md-img"
              />
            ) : (
              <span className="ergo-chat__md-unsafe">{alt || 'image'}</span>
            ),
        }}
      />
    </div>
  )
}

/** Host-configurable markdown renderer for applications with existing link and code behavior. */
export function MarkdownContent({ text, components, rehypePlugins, urlTransform }: {
  text: string
  components?: Record<string, any>
  rehypePlugins?: any[]
  urlTransform?: (url: string) => string | undefined
}) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} rehypePlugins={rehypePlugins} urlTransform={urlTransform}>
      {text}
    </ReactMarkdown>
  )
}
