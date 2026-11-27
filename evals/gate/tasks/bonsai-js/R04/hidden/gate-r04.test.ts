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

describe('gate R04 chunk', () => {
  it('splits a list into lists of a size', () => {
    expect(run('chunk([1, 2, 3, 4, 5], 2)')).toEqual([[1, 2], [3, 4], [5]])
    expect(run('chunk(xs, 3)', { xs: [1, 2, 3, 4, 5, 6] })).toEqual([
      [1, 2, 3],
      [4, 5, 6],
    ])
    expect(run('chunk([1, 2], 5)')).toEqual([[1, 2]])
    expect(run('chunk([], 2)')).toEqual([])
  })

  it('rejects a size that is not a positive whole number', () => {
    expect(codeOf('chunk([1, 2], n)', { n: 0 })).toBe('INVALID_ARGUMENT')
    expect(codeOf('chunk([1, 2], n)', { n: -2 })).toBe('INVALID_ARGUMENT')
    expect(codeOf('chunk([1, 2, 3], n)', { n: 1.5 })).toBe('INVALID_ARGUMENT')
  })
})
