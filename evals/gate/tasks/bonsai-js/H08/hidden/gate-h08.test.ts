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

const remote = (cost?: number) =>
  fn({
    params: [],
    returns: t.number(),
    async: true,
    ...(cost === undefined ? {} : { cost }),
    run: async () => 1,
  })
const local = (cost?: number) =>
  fn({ params: [], returns: t.number(), ...(cost === undefined ? {} : { cost }), run: () => 1 })

/** The smallest step budget under which `f()` evaluates. */
async function fewestSteps(f: ReturnType<typeof fn>): Promise<number> {
  const program = bonsai({ functions: { f } }).compile('f()')
  const fits = async (maxSteps: number): Promise<boolean> => {
    try {
      await program.evaluate({}, { maxSteps })
      return true
    } catch (error) {
      if (error instanceof BonsaiError && error.code === 'STEP_LIMIT') return false
      throw error
    }
  }
  let low = 1
  let high = 100_000
  if (!(await fits(high))) throw new Error('f() does not fit in 100,000 steps')
  while (low < high) {
    const mid = Math.floor((low + high) / 2)
    if (await fits(mid)) high = mid
    else low = mid + 1
  }
  return low
}

describe('gate H08 async host function cost', () => {
  it('keeps sync functions and declared costs as they are', async () => {
    expect(await fewestSteps(local())).toBe(await fewestSteps(local(32)))
    expect((await fewestSteps(remote(40))) - (await fewestSteps(remote(5)))).toBe(35)
    const env = bonsai({ functions: { a: remote(7), s: local() } })
    expect(env.describeFunction('a')?.cost).toBe(7)
    expect(env.describeFunction('s')?.cost).toBe(32)
  })

  it('charges an async function without a declared cost more than a sync one', async () => {
    expect(await fewestSteps(remote())).toBeGreaterThan(await fewestSteps(remote(32)))
  })

  it('charges 250 steps by default for an async call', async () => {
    expect(await fewestSteps(remote())).toBe(await fewestSteps(remote(250)))
    expect((await fewestSteps(remote())) - (await fewestSteps(remote(5)))).toBe(245)
    const env = bonsai({ functions: { a: remote() } })
    expect(env.describeFunction('a')?.cost).toBe(250)
    expect(env.listFunctions().find((f) => f.name === 'a')?.cost).toBe(250)
  })
})
