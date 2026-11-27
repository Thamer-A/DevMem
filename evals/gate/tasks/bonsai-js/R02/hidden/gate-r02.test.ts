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

const rv = (s: string): unknown => run('reverse(s)', { s })

describe('gate R02 reverse text', () => {
  it('reverses text', () => {
    expect(rv('abc')).toBe('cba')
    expect(rv('')).toBe('')
    expect(run('"hello".reverse()')).toBe('olleh')
  })

  it('still reverses lists', () => {
    expect(run('reverse([1, 2, 3])')).toEqual([3, 2, 1])
    expect(run('reverse(xs)', { xs: ['a', 'b'] })).toEqual(['b', 'a'])
  })

  it('keeps characters outside the BMP whole', () => {
    expect(rv('a\u{1F600}b')).toBe('b\u{1F600}a')
    expect(rv('\u{1F600}\u{1F680}')).toBe('\u{1F680}\u{1F600}')
  })
})
