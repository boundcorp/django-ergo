import { describe, expect, test } from 'bun:test'
import { QUICK_REPLY_HOLD_MS, QUICK_REPLY_MOVE_TOLERANCE_PX, QuickReplyHoldController } from './quickReplyHold'

function fakeTimers() {
  let now = 0
  let next = 1
  const pending = new Map<number, { at: number; fn: () => void }>()
  return {
    setTimeout: (fn: () => void, ms: number) => {
      const id = next++
      pending.set(id, { at: now + ms, fn })
      return id
    },
    clearTimeout: (handle: unknown) => pending.delete(handle as number),
    advance(ms: number) {
      const end = now + ms
      for (;;) {
        const due = [...pending.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
        if (!due) break
        pending.delete(due[0])
        now = due[1].at
        due[1].fn()
      }
      now = end
    },
  }
}

function setup() {
  const timers = fakeTimers()
  const holding: (string | null)[] = []
  const sent: string[] = []
  const controller = new QuickReplyHoldController(
    text => holding.push(text),
    text => sent.push(text),
    timers,
  )
  return { controller, holding, sent, timers }
}

describe('QuickReplyHoldController', () => {
  test('a short pointer click does not send and clears its fill', () => {
    const { controller, holding, sent, timers } = setup()

    expect(controller.beginPointer('Send it', 1, 10, 10)).toBe(true)
    timers.advance(QUICK_REPLY_HOLD_MS - 1)
    controller.cancelPointer(1)
    timers.advance(1)

    expect(sent).toEqual([])
    expect(holding).toEqual(['Send it', null])
  })

  test('a full pointer hold sends exactly once', () => {
    const { controller, holding, sent, timers } = setup()

    controller.beginPointer('Send it', 1, 10, 10)
    timers.advance(QUICK_REPLY_HOLD_MS)
    controller.cancelPointer(1)
    timers.advance(QUICK_REPLY_HOLD_MS)

    expect(sent).toEqual(['Send it'])
    expect(holding).toEqual(['Send it', null])
  })

  test('pointer movement beyond the tolerance and leaving cancel the hold', () => {
    const { controller, holding, sent, timers } = setup()

    controller.beginPointer('Move', 1, 0, 0)
    controller.movePointer(1, QUICK_REPLY_MOVE_TOLERANCE_PX + 1, 0)
    timers.advance(QUICK_REPLY_HOLD_MS)
    controller.beginPointer('Leave', 2, 0, 0)
    controller.cancel()
    timers.advance(QUICK_REPLY_HOLD_MS)

    expect(sent).toEqual([])
    expect(holding).toEqual(['Move', null, 'Leave', null])
  })

  test('pointer cancellation clears the fill without sending', () => {
    const { controller, holding, sent, timers } = setup()

    controller.beginPointer('Cancel', 1, 0, 0)
    controller.cancelPointer(1)
    timers.advance(QUICK_REPLY_HOLD_MS)

    expect(sent).toEqual([])
    expect(holding).toEqual(['Cancel', null])
  })

  test('keyboard needs the same hold duration and ignores repeat starts', () => {
    const { controller, holding, sent, timers } = setup()

    expect(controller.beginKeyboard('Keyboard')).toBe(true)
    expect(controller.beginKeyboard('Keyboard')).toBe(false)
    controller.endKeyboard()
    timers.advance(QUICK_REPLY_HOLD_MS)
    expect(sent).toEqual([])

    expect(controller.beginKeyboard('Keyboard')).toBe(true)
    timers.advance(QUICK_REPLY_HOLD_MS)

    expect(sent).toEqual(['Keyboard'])
    expect(holding).toEqual(['Keyboard', null, 'Keyboard', null])
  })

  test('disabled or pending chat state prevents a hold', () => {
    const { controller, holding, sent, timers } = setup()

    controller.setDisabled(true)
    expect(controller.beginPointer('Disabled', 1, 0, 0)).toBe(false)
    controller.setDisabled(false)
    controller.beginPointer('Pending', 2, 0, 0)
    controller.setDisabled(true)
    timers.advance(QUICK_REPLY_HOLD_MS)

    expect(sent).toEqual([])
    expect(holding).toEqual(['Pending', null])
  })

  test('does not double-fire until the enclosing chat resets it', () => {
    const { controller, sent, timers } = setup()

    controller.beginPointer('One', 1, 0, 0)
    timers.advance(QUICK_REPLY_HOLD_MS)
    expect(controller.beginPointer('Two', 2, 0, 0)).toBe(false)
    controller.reset()
    expect(controller.beginPointer('Two', 2, 0, 0)).toBe(true)
    timers.advance(QUICK_REPLY_HOLD_MS)

    expect(sent).toEqual(['One', 'Two'])
  })
})
