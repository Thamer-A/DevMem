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

const fd = (source: string): unknown => run(`formatDuration(${source})`)

describe('gate H06 formatDuration', () => {
  it('writes short durations', () => {
    expect(fd('hours(3)')).toBe('3h')
    expect(fd('seconds(42)')).toBe('42s')
    expect(fd('minutes(5) + seconds(12)')).toBe('5m 12s')
    expect(fd('days(2) + hours(3)')).toBe('2d 3h')
  })

  it('shows only the two largest non-zero units', () => {
    expect(fd('days(2) + hours(3) + minutes(4) + seconds(5)')).toBe('2d 3h')
    expect(fd('hours(7) + minutes(30) + seconds(15)')).toBe('7h 30m')
    expect(fd('days(1) + minutes(30) + seconds(15)')).toBe('1d 30m')
    expect(fd('hours(1) + seconds(5)')).toBe('1h 5s')
  })

  it('drops the rest without rounding', () => {
    expect(fd('minutes(5) + seconds(12) + milliseconds(900)')).toBe('5m 12s')
    expect(fd('minutes(59) + seconds(59) + milliseconds(999)')).toBe('59m 59s')
    expect(fd('hours(2) + minutes(59) + seconds(59)')).toBe('2h 59m')
  })
})
