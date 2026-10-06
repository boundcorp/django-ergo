import { describe, expect, test } from 'bun:test'
import { prepareChatSend } from './chatSend'

describe('prepareChatSend', () => {
  test('sends a suggestion while preserving a populated composer verbatim through a response refresh', () => {
    const draft = 'Keep this draft.\n  Including whitespace.  \n'
    const attachments = [{ id: 'draft-file' }]
    const plan = prepareChatSend('suggestion', 'Use the suggested reply', draft, attachments)

    expect(plan.message).toBe('Use the suggested reply')
    expect(plan.attachmentIds).toEqual([])
    expect(plan.composer).toBe('preserve')
    expect(plan.draft).toBe(draft)
    expect(plan.outgoing).toBe(attachments)
  })

  test('sends a suggestion from an empty composer without creating a draft', () => {
    const plan = prepareChatSend('suggestion', 'Suggested reply', '', [])

    expect(plan.message).toBe('Suggested reply')
    expect(plan.attachmentIds).toEqual([])
    expect(plan.draft).toBe('')
    expect(plan.composer).toBe('preserve')
  })

  test('a normal composer send still clears the composer and sends its attachments', () => {
    expect(prepareChatSend('composer', 'Manual message', 'Manual message', [{ id: 'one' }, { id: 'two' }])).toEqual({
      attachmentIds: ['one', 'two'],
      composer: 'clear',
      draft: '',
      message: 'Manual message',
      outgoing: [],
      sentFiles: [{ id: 'one' }, { id: 'two' }],
    })
  })
})
