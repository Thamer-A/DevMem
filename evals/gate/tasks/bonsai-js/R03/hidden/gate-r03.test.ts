import { describe, expect, it } from 'vitest'
import { BonsaiError, bonsai, fn, t } from '../src/index.js'

const env = bonsai()
const run = (source: string, context: Record<string, unknown> = {}): unknown =>
  env.evaluateSync(source, context)
const codeOf = (source: string, context: Record<string, unknown> = {}): string => {
  try {
    env.evaluateSync(source, context)
  } catch (error) {
    if (error instanceof BonsaiError) return error.code
    throw error
  }
  return 'OK'
}
const at = (iso: string): Date => new Date(iso)
void fn
void t
void codeOf
void at

describe('gate R03 zip', () => {
  it('pairs items by position', () => {
    expect(run('zip([1, 2], [3, 4])')).toEqual([
      [1, 3],
      [2, 4],
    ])
    expect(run('zip(a, b)', { a: ['x'], b: [true] })).toEqual([['x', true]])
    expect(run('zip([], [])')).toEqual([])
  })

  it('stops at the end of the shorter list', () => {
    expect(run('zip([1, 2, 3], ["a", "b"])')).toEqual([
      [1, 'a'],
      [2, 'b'],
    ])
    expect(run('zip([1], [1, 2, 3])')).toEqual([[1, 1]])
    expect(run('zip(a, b)', { a: [1, 2, 3, 4], b: [] })).toEqual([])
  })
})
