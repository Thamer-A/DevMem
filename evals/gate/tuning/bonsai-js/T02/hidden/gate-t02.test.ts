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

const fb = (n: number): unknown => run('formatBytes(n)', { n })

describe('gate T02 formatBytes', () => {
  it('writes small sizes in bytes', () => {
    expect(fb(0)).toBe('0 B')
    expect(fb(512)).toBe('512 B')
    expect(fb(1023)).toBe('1023 B')
  })

  it('uses 1,024-based units with one decimal', () => {
    expect(fb(1024)).toBe('1.0 KiB')
    expect(fb(1536)).toBe('1.5 KiB')
    expect(fb(1024 * 1024)).toBe('1.0 MiB')
    expect(fb(1.5 * 1024 * 1024)).toBe('1.5 MiB')
    expect(fb(5 * 1024 * 1024 * 1024)).toBe('5.0 GiB')
    expect(run('formatBytes(2048)')).toBe('2.0 KiB')
  })
})
