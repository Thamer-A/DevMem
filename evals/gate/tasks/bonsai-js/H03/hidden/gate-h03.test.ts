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

const b = (value: unknown, low: unknown, high: unknown): unknown =>
  run('between(v, lo, hi)', { v: value, lo: low, hi: high })

describe('gate H03 between', () => {
  it('tells whether a number is inside a range', () => {
    expect(b(5, 0, 10)).toBe(true)
    expect(b(9.5, 0, 10)).toBe(true)
    expect(b(-1, 0, 10)).toBe(false)
    expect(b(11, 0, 10)).toBe(false)
    expect(run('between(75, 50, 100)')).toBe(true)
  })

  it('counts the low end and not the high end', () => {
    expect(b(0, 0, 10)).toBe(true)
    expect(b(10, 0, 10)).toBe(false)
    expect(run('between(10, 0, 10)')).toBe(false)
    expect(run('between(50, 50, 100)')).toBe(true)
    expect(run('between(100, 50, 100)')).toBe(false)
  })

  it('works on timestamps the same way', () => {
    const start = at('2026-01-01T00:00:00Z')
    const end = at('2026-02-01T00:00:00Z')
    expect(b(at('2026-01-15T00:00:00Z'), start, end)).toBe(true)
    expect(b(at('2026-01-01T00:00:00Z'), start, end)).toBe(true)
    expect(b(at('2026-02-01T00:00:00Z'), start, end)).toBe(false)
    expect(b(at('2025-12-31T23:59:59Z'), start, end)).toBe(false)
  })
})
