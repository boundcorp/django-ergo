import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Todo } from '../api'
import { TodoList, todoProgress } from './TodoList'

const todos: Todo[] = [
  { content: 'Read the code', status: 'completed' },
  { content: 'Write the tools', status: 'in_progress' },
  { content: 'Open the PR', status: 'pending' },
]

describe('TodoList', () => {
  test('an unfinished list is open, with each item and its status', () => {
    const html = renderToStaticMarkup(createElement(TodoList, { todos }))
    expect(html).toContain('aria-expanded="true"')
    expect(html).toContain('1 of 3 done')
    expect(html).toContain('data-status="in_progress"')
    expect(html).toContain('Open the PR')
  })

  test('a finished list folds to its header', () => {
    const done = todos.map(t => ({ ...t, status: 'completed' as const }))
    const html = renderToStaticMarkup(createElement(TodoList, { todos: done }))
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('3 of 3 done')
    expect(html).not.toContain('Read the code')
  })

  test('an empty list shows nothing', () => {
    expect(renderToStaticMarkup(createElement(TodoList, { todos: [] }))).toBe('')
    expect(todoProgress([])).toBe('0 of 0 done')
  })
})
