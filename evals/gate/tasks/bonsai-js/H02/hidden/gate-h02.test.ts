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

const q = (iso: string, zone?: string): unknown =>
  zone === undefined ? run('quarter(d)', { d: at(iso) }) : run('quarter(d, z)', { d: at(iso), z: zone })

describe('gate H02 quarter', () => {
  it('returns a quarter number', () => {
    for (const iso of ['2026-01-15T12:00:00Z', '2026-05-15T12:00:00Z', '2026-09-15T12:00:00Z']) {
      expect([1, 2, 3, 4]).toContain(q(iso))
    }
    expect(run('quarter(timestamp("2026-07-10T00:00:00Z"))')).toBe(q('2026-07-10T00:00:00Z'))
  })

  it('starts the year in April', () => {
    expect(q('2026-04-01T00:00:00Z')).toBe(1)
    expect(q('2026-05-15T12:00:00Z')).toBe(1)
    expect(q('2026-06-30T23:59:59Z')).toBe(1)
    expect(q('2026-07-01T00:00:00Z')).toBe(2)
    expect(q('2026-09-30T12:00:00Z')).toBe(2)
    expect(q('2026-10-10T12:00:00Z')).toBe(3)
    expect(q('2026-12-31T12:00:00Z')).toBe(3)
    expect(q('2026-01-15T12:00:00Z')).toBe(4)
    expect(q('2026-03-31T12:00:00Z')).toBe(4)
  })

  it('reads the month in the time zone', () => {
    // 31 March 23:30 UTC is 1 April 00:30 in London (summer time from 29 March 2026).
    expect(q('2026-03-31T23:30:00Z', 'Europe/London')).toBe(1)
    expect(q('2026-03-31T23:30:00Z')).toBe(4)
  })
})
