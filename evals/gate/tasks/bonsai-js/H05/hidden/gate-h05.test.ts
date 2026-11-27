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

describe('gate H05 formatList', () => {
  it('joins names in a sentence', () => {
    expect(run('formatList(["Ann", "Bob"])')).toBe('Ann and Bob')
    expect(run('formatList(["Ann"])')).toBe('Ann')
    expect(run('formatList([])')).toBe('')
  })

  it('takes a locale', () => {
    expect(run('formatList(xs, "en-US")', { xs: ['Ann', 'Bob', 'Cy'] })).toBe('Ann, Bob, and Cy')
    expect(run('formatList(xs, "de-DE")', { xs: ['Ann', 'Bob', 'Cy'] })).toBe('Ann, Bob und Cy')
  })

  it('defaults to British English', () => {
    expect(run('formatList(["Ann", "Bob", "Cy"])')).toBe('Ann, Bob and Cy')
    expect(run('formatList(xs)', { xs: ['red', 'green', 'blue', 'white'] })).toBe(
      'red, green, blue and white',
    )
  })
})
