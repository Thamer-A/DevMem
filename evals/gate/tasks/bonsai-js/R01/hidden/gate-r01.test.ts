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

const w = (s: string): unknown => run('words(s)', { s })

describe('gate R01 words', () => {
  it('splits at runs of whitespace', () => {
    expect(w('a b c')).toEqual(['a', 'b', 'c'])
    expect(w('  a  b\tc\nd ')).toEqual(['a', 'b', 'c', 'd'])
    expect(w('one')).toEqual(['one'])
    expect(run('"x y".words()')).toEqual(['x', 'y'])
  })

  it('finds no words in empty or blank text', () => {
    expect(w('')).toEqual([])
    expect(w('   ')).toEqual([])
    expect(w(' \t\n ')).toEqual([])
  })
})
