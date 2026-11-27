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

const tc = (s: string): unknown => run('titleCase(s)', { s })

describe('gate H01 titleCase', () => {
  it('capitalizes ordinary words', () => {
    expect(tc('hello world')).toBe('Hello World')
    expect(run('"red apple".titleCase()')).toBe('Red Apple')
    expect(tc('')).toBe('')
  })

  it('keeps the agreed short words in lower case inside a title', () => {
    expect(tc('the lord of the rings')).toBe('The Lord of the Rings')
    expect(tc('war and peace')).toBe('War and Peace')
    expect(tc('a tale of two cities')).toBe('A Tale of Two Cities')
    expect(tc('one for all and all for one')).toBe('One for All and All for One')
    expect(tc('to be or not to be')).toBe('To Be or Not to Be')
    expect(tc('a room by the sea')).toBe('A Room by the Sea')
    expect(tc('neither here nor there')).toBe('Neither Here nor There')
    expect(tc('an evening in paris but not at home')).toBe('An Evening in Paris but Not at Home')
    expect(tc('notes on a scandal')).toBe('Notes on a Scandal')
  })

  it('capitalizes other short words', () => {
    expect(tc('gone with the wind')).toBe('Gone With the Wind')
    expect(tc('up from the ground')).toBe('Up From the Ground')
  })

  it('capitalizes a short word that comes first', () => {
    expect(tc('of mice and men')).toBe('Of Mice and Men')
    expect(tc('the end')).toBe('The End')
  })
})
