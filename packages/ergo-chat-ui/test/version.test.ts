import { version } from 'react'
import { expect, test } from 'vitest'

test('runs against the React major the script asked for', () => {
  expect(version.split('.')[0]).toBe(process.env.REACT_MAJOR ?? '19')
})
