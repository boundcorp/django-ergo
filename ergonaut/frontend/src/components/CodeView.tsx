import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import django from 'highlight.js/lib/languages/django'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import ini from 'highlight.js/lib/languages/ini'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import { useMemo } from 'react'

hljs.registerLanguage('bash', bash)
hljs.registerLanguage('css', css)
hljs.registerLanguage('django', django)
hljs.registerLanguage('dockerfile', dockerfile)
hljs.registerLanguage('ini', ini)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('python', python)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('yaml', yaml)

const BY_EXTENSION: Record<string, string> = {
  py: 'python',
  yaml: 'yaml',
  yml: 'yaml',
  json: 'json',
  md: 'markdown',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  css: 'css',
  html: 'xml',
  htm: 'xml',
  xml: 'xml',
  svg: 'xml',
  jhtml: 'django',
  sql: 'sql',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
}

export function languageFor(path: string): string | null {
  const name = path.split('/').pop() ?? ''
  if (name === 'Dockerfile') return 'dockerfile'
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
  return BY_EXTENSION[ext] ?? null
}

/** Highlighted HTML for a code block in a known language, or null. */
export function highlight(code: string, language: string): string | null {
  const name = BY_EXTENSION[language.toLowerCase()] ?? language.toLowerCase()
  if (!hljs.getLanguage(name) || code.length > 300_000) return null
  try {
    return hljs.highlight(code, { language: name, ignoreIllegals: true }).value
  } catch {
    return null
  }
}

/** Split highlighted HTML into lines, closing and reopening spans that cross a line break. */
function splitLines(html: string): string[] {
  const lines: string[] = []
  const open: string[] = []
  let current = ''
  const tokens = html.split(/(<span[^>]*>|<\/span>|\n)/)
  for (const token of tokens) {
    if (token === '\n') {
      lines.push(current + '</span>'.repeat(open.length))
      current = open.join('')
    } else if (token.startsWith('<span')) {
      open.push(token)
      current += token
    } else if (token === '</span>') {
      open.pop()
      current += token
    } else {
      current += token
    }
  }
  lines.push(current)
  return lines
}

// A file's text with line numbers, highlighted by its extension (plain text otherwise).
export default function CodeView({ path, text }: { path: string; text: string }) {
  const lines = useMemo(() => {
    const language = languageFor(path)
    if (!language || text.length > 300_000) return null
    try {
      return splitLines(hljs.highlight(text, { language, ignoreIllegals: true }).value)
    } catch {
      return null
    }
  }, [path, text])
  const plain = useMemo(() => (lines ? null : text.split('\n')), [lines, text])
  return (
    <table className="hljs-view font-mono text-xs">
      <tbody>
        {lines
          ? lines.map((line, i) => (
              <tr key={i}>
                <td className="pr-3 pl-3 text-right align-top text-zinc-400 select-none">{i + 1}</td>
                {/* hljs escapes the file's text; the only markup is its own spans. */}
                <td className="pr-3 whitespace-pre-wrap" dangerouslySetInnerHTML={{ __html: line || ' ' }} />
              </tr>
            ))
          : plain!.map((line, i) => (
              <tr key={i}>
                <td className="pr-3 pl-3 text-right align-top text-zinc-400 select-none">{i + 1}</td>
                <td className="pr-3 whitespace-pre-wrap">{line || ' '}</td>
              </tr>
            ))}
      </tbody>
    </table>
  )
}
