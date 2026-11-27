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

const sow = (iso: string, zone?: string): string => {
  const out = (
    zone === undefined ? run('startOfWeek(d)', { d: at(iso) }) : run('startOfWeek(d, z)', { d: at(iso), z: zone })
  ) as Date
  return out.toISOString()
}

describe('gate T01 startOfWeek', () => {
  it('returns midnight', () => {
    expect(sow('2026-11-25T15:00:00Z').endsWith('T00:00:00.000Z')).toBe(true)
  })

  it('starts weeks on Sunday', () => {
    expect(sow('2026-11-25T15:00:00Z')).toBe('2026-11-22T00:00:00.000Z')
    expect(sow('2026-11-22T10:00:00Z')).toBe('2026-11-22T00:00:00.000Z')
    expect(sow('2026-11-23T10:00:00Z')).toBe('2026-11-22T00:00:00.000Z')
    expect(sow('2026-11-28T23:00:00Z')).toBe('2026-11-22T00:00:00.000Z')
    expect(sow('2026-11-29T00:00:00Z')).toBe('2026-11-29T00:00:00.000Z')
    expect(sow('2027-01-01T12:00:00Z')).toBe('2026-12-27T00:00:00.000Z')
  })

  it('reads the day in the time zone', () => {
    // 2026-11-28T23:30Z is Sunday 29 November, 00:30, in Berlin.
    expect(sow('2026-11-28T23:30:00Z', 'Europe/Berlin')).toBe('2026-11-28T23:00:00.000Z')
  })
})
