import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Todo } from '../api'
import { TodoButton, TodoList, todoProgress } from './TodoList'

const todos: Todo[] = [
  { content: 'Read the code', status: 'completed' },
  { content: 'Write the tools', status: 'in_progress' },
  { content: 'Open the PR', status: 'pending' },
]

const button = (items: Todo[], open: boolean) =>
  renderToStaticMarkup(createElement(TodoButton, { todos: items, open, controls: 'chat-todos', onToggle: () => {} }))

describe('TodoList', () => {
  test('lists each item with its status and the progress', () => {
    const html = renderToStaticMarkup(createElement(TodoList, { id: 'chat-todos', todos }))
    expect(html).toContain('id="chat-todos"')
    expect(html).toContain('1 of 3 done')
    expect(html).toContain('data-status="in_progress"')
    expect(html).toContain('Open the PR')
  })

  test('an empty list shows nothing', () => {
    expect(renderToStaticMarkup(createElement(TodoList, { todos: [] }))).toBe('')
    expect(todoProgress([])).toBe('0 of 0 done')
  })
})

describe('TodoButton', () => {
  test('shows done/total and whether the list is shown', () => {
    const closed = button(todos, false)
    expect(closed).toContain('Todo 1/3')
    expect(closed).toContain('aria-expanded="false"')
    expect(closed).toContain('aria-controls="chat-todos"')
    expect(button(todos, true)).toContain('aria-expanded="true"')
  })

  test('hides when there are no todos', () => {
    expect(button([], false)).toBe('')
  })
})
