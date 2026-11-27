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

const r = (value: number, step: number): unknown => run('roundToNearest(v, s)', { v: value, s: step })

describe('gate H07 roundToNearest', () => {
  it('rounds to the nearest multiple', () => {
    expect(r(12, 5)).toBe(10)
    expect(r(13, 5)).toBe(15)
    expect(r(1.3, 0.25)).toBe(1.25)
    expect(r(7.9, 0.5)).toBe(8)
    expect(run('roundToNearest(99, 10)')).toBe(100)
  })

  it('rejects a step that is not positive', () => {
    expect(codeOf('roundToNearest(5, s)', { s: 0 })).toBe('INVALID_ARGUMENT')
    expect(codeOf('roundToNearest(5, s)', { s: -5 })).toBe('INVALID_ARGUMENT')
  })

  it('sends halves to the even multiple', () => {
    expect(r(25, 10)).toBe(20)
    expect(r(35, 10)).toBe(40)
    expect(r(2.5, 1)).toBe(2)
    expect(r(3.5, 1)).toBe(4)
    expect(r(1.125, 0.25)).toBe(1)
    expect(r(15, 10)).toBe(20)
    expect(r(-25, 10)).toBe(-20)
  })
})
