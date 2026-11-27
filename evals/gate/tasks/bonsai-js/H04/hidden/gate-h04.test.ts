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

const m = (s: string): unknown => run('mask(s)', { s })
const DOT = '•'

describe('gate H04 mask', () => {
  it('keeps the end of the number visible', () => {
    const out = m('4111111111111234') as string
    expect(out.endsWith('1234')).toBe(true)
    expect(out.includes('4111')).toBe(false)
    expect(m('')).toBe('')
  })

  it('hides every other character with a bullet', () => {
    expect(m('4111111111111234')).toBe(DOT.repeat(12) + '1234')
    expect(m('123456789')).toBe(DOT.repeat(5) + '6789')
    expect(run('"987654321".mask()')).toBe(DOT.repeat(5) + '4321')
  })

  it('keeps spaces and dashes as they are', () => {
    expect(m('4111 1111 1111 1234')).toBe(`${DOT.repeat(4)} ${DOT.repeat(4)} ${DOT.repeat(4)} 1234`)
    expect(m('12-34-5678')).toBe(`${DOT.repeat(2)}-${DOT.repeat(2)}-5678`)
  })
})
