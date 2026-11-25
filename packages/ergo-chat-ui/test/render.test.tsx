import { afterEach, describe, expect, test } from 'vitest'
import { Transcript, type Message, type Part } from '../src'
import { cleanup, click, render } from './helpers'

afterEach(cleanup)

const msg = (parts: Part[], role: Message['role'] = 'assistant', id = 'm1'): Message => ({ id, role, parts })

describe('Transcript', () => {
  test('renders markdown, code and the empty state', async () => {
    const el = await render(
      <Transcript
        messages={[
          msg([{ type: 'text', text: '# Title\n\n- a\n- b\n\n```python\nprint("hi")\n```\n\n| x |\n|---|\n| 1 |' }]),
        ]}
      />,
    )
    expect(el.querySelector('h1')?.textContent).toBe('Title')
    expect(el.querySelectorAll('li')).toHaveLength(2)
    expect(el.querySelector('.ergo-chat__code-lang')?.textContent).toBe('python')
    expect(el.querySelector('.hljs-built_in, .hljs-string')).not.toBeNull()
    expect(el.querySelector('table td')?.textContent).toBe('1')
    const empty = await render(<Transcript messages={[]} empty="Nothing yet" />)
    expect(empty.textContent).toContain('Nothing yet')
  })

  test('raw HTML is text, unsafe links and images are neutralised', async () => {
    const el = await render(
      <Transcript
        messages={[
          msg([
            {
              type: 'text',
              text: '<img src=x onerror="alert(1)"> <script>boom()</script>\n\n[bad](javascript:alert(1)) [data](data:text/html,x) [net](//evil.test) [ok](https://example.com/a) [rel](/files/1)\n\n![pixel](https://tracker.test/p.png)',
            },
          ]),
        ]}
      />,
    )
    expect(el.querySelector('script')).toBeNull()
    expect(el.querySelector('img')).toBeNull()
    expect(el.textContent).toContain('<script>boom()</script>')
    const hrefs = [...el.querySelectorAll('a')].map(a => a.getAttribute('href'))
    expect(hrefs).toEqual(['https://example.com/a', '/files/1'])
    expect(el.querySelector('a[href^="https"]')?.getAttribute('rel')).toContain('noopener')
  })

  test('markdown images render only when allowed, from http(s)', async () => {
    const el = await render(
      <Transcript
        allowMarkdownImages
        messages={[msg([{ type: 'text', text: '![a](https://x.test/a.png) ![b](javascript:1)' }])]}
      />,
    )
    expect([...el.querySelectorAll('img')].map(i => i.getAttribute('src'))).toEqual(['https://x.test/a.png'])
  })

  test('tool parts: running, done, error', async () => {
    const el = await render(
      <Transcript
        messages={[
          msg([
            { type: 'tool', id: 't1', name: 'mcp__files__read', status: 'running', input: { path: '/a/b.txt' } },
            {
              type: 'tool',
              id: 't2',
              name: 'search',
              status: 'done',
              input: '{"q":"x"}',
              result: [{ text: 'found 3' }],
            },
            {
              type: 'tool',
              id: 't3',
              name: 'deploy',
              status: 'error',
              input: { env: 'prod' },
              result: 'permission denied',
            },
          ]),
        ]}
      />,
    )
    const tools = [...el.querySelectorAll<HTMLElement>('.ergo-chat__tool')]
    expect(tools.map(t => t.dataset.status)).toEqual(['running', 'done', 'error'])
    expect(tools[0].textContent).toContain('files · read')
    expect(tools[0].querySelector('.ergo-chat__sr')?.textContent).toBe('running:')
    // folded until opened
    expect(tools[1].textContent).not.toContain('found 3')
    await click(tools[1].querySelector('button')!)
    expect(tools[1].textContent).toContain('found 3')
    await click(tools[2].querySelector('button')!)
    expect(tools[2].querySelector('details')?.hasAttribute('open')).toBe(true)
    expect(tools[2].textContent).toContain('permission denied')
    expect(tools[0].querySelector('button')?.getAttribute('aria-expanded')).toBe('false')
  })

  test('attachments: image, audio, file, unsafe url', async () => {
    const el = await render(
      <Transcript
        messages={[
          msg(
            [
              {
                type: 'attachment',
                attachment: { id: 'a', name: 'cat.png', mediaType: 'image/png', url: '/f/cat.png' },
              },
              {
                type: 'attachment',
                attachment: { id: 'b', name: 'clip.mp3', mediaType: 'audio/mpeg', url: 'https://x.test/c.mp3' },
              },
              {
                type: 'attachment',
                attachment: {
                  id: 'c',
                  name: 'r.pdf',
                  mediaType: 'application/pdf',
                  url: 'https://x.test/r.pdf',
                  size: 2048,
                },
              },
              { type: 'attachment', attachment: { id: 'd', name: 'evil.html', url: 'javascript:alert(1)' } },
            ],
            'user',
          ),
        ]}
      />,
    )
    expect(el.querySelector('img')?.getAttribute('src')).toBe('/f/cat.png')
    expect(el.querySelector('audio')).not.toBeNull()
    expect(el.textContent).toContain('r.pdf (2.0 KB)')
    const evil = [...el.querySelectorAll('.ergo-chat__file')].find(f => f.textContent?.includes('evil.html'))!
    expect(evil.tagName).toBe('SPAN')
  })

  test('unknown parts and custom parts fall back safely', async () => {
    const el = await render(
      <Transcript
        messages={[
          msg([
            { type: 'hologram', payload: { x: 1 } } as unknown as Part,
            { type: 'custom', kind: 'approval', id: 'ap1' },
            { type: 'tool' } as unknown as Part,
            { type: 'text', text: 'after' },
          ]),
        ]}
      />,
    )
    const unknown = [...el.querySelectorAll('.ergo-chat__unknown')].map(n => n.textContent)
    expect(unknown).toEqual(['Unsupported content: hologram', 'Unsupported content: approval', 'Unsupported tool call'])
    expect(el.textContent).toContain('after')
  })

  test('renderPart overrides: approval card slot, wrapping default, hiding, undefined keeps default', async () => {
    const decisions: string[] = []
    const el = await render(
      <Transcript
        messages={[
          msg([
            { type: 'custom', kind: 'approval', id: 'ap1', data: { tool: 'deploy' } },
            { type: 'text', text: 'plain' },
            { type: 'custom', kind: 'hidden' },
          ]),
        ]}
        renderPart={(part, { defaultRender }) => {
          if (part.type === 'custom' && part.kind === 'approval')
            return <button onClick={() => decisions.push(`approve:${part.id}`)}>Approve deploy</button>
          if (part.type === 'custom') return null
          if (part.type === 'text') return <div className="wrap">{defaultRender}</div>
          return undefined
        }}
      />,
    )
    await click([...el.querySelectorAll('button')].find(b => b.textContent === 'Approve deploy')!)
    expect(decisions).toEqual(['approve:ap1'])
    expect(el.querySelector('.wrap .ergo-chat__md')?.textContent).toBe('plain')
    expect(el.querySelector('.ergo-chat__unknown')).toBeNull()
  })

  test('tokens live on one root; theme attribute is set', async () => {
    const el = await render(<Transcript theme="dark" messages={[]} />)
    expect(el.firstElementChild?.className).toContain('ergo-chat ')
    expect(el.firstElementChild?.getAttribute('data-theme')).toBe('dark')
  })
})
