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

const sw = (a: string, b: string, zone?: string): unknown =>
  zone === undefined
    ? run('sameWeek(a, b)', { a: at(a), b: at(b) })
    : run('sameWeek(a, b, z)', { a: at(a), b: at(b), z: zone })

describe('gate T03 sameWeek', () => {
  it('compares two timestamps', () => {
    expect(sw('2026-11-24T08:00:00Z', '2026-11-25T20:00:00Z')).toBe(true)
    expect(sw('2026-11-24T08:00:00Z', '2026-11-24T08:00:00Z')).toBe(true)
    expect(sw('2026-11-24T08:00:00Z', '2026-12-24T08:00:00Z')).toBe(false)
  })

  it('runs weeks from Sunday to Saturday', () => {
    expect(sw('2026-11-22T09:00:00Z', '2026-11-28T18:00:00Z')).toBe(true)
    expect(sw('2026-11-21T12:00:00Z', '2026-11-22T12:00:00Z')).toBe(false)
    expect(sw('2026-11-23T08:00:00Z', '2026-11-29T08:00:00Z')).toBe(false)
    expect(sw('2026-11-29T08:00:00Z', '2026-12-05T22:00:00Z')).toBe(true)
  })

  it('reads the day in the time zone', () => {
    // 2026-11-28T23:30Z is already Sunday in Berlin.
    expect(sw('2026-11-28T23:30:00Z', '2026-11-28T12:00:00Z')).toBe(true)
    expect(sw('2026-11-28T23:30:00Z', '2026-11-28T12:00:00Z', 'Europe/Berlin')).toBe(false)
  })
})
