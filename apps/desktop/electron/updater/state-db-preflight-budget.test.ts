import path from 'node:path'

import { beforeEach, expect, test, vi } from 'vitest'

import { preflightStateDb } from './state-db-preflight'

const { runSnapshot, stat } = vi.hoisted(() => ({ runSnapshot: vi.fn(), stat: vi.fn() }))

vi.mock('node:child_process', async () => {
  const { promisify } = await import('node:util')

  return { execFile: Object.assign(vi.fn(), { [promisify.custom]: runSnapshot }) }
})
vi.mock('node:fs', () => ({ default: { statSync: stat } }))

beforeEach(() => vi.resetAllMocks())

test('a large database and pending WAL can finish their snapshot before shutdown', async () => {
  stat.mockImplementation((file: string) => ({
    size: path.basename(file) === 'state.db' ? 2 * 1024 ** 3 : 1024 ** 3
  }))
  runSnapshot.mockImplementation(async (_command, _args, options) => {
    if (options.timeout < 330_000) {
      throw new Error('ETIMEDOUT')
    }

    return { stdout: '{"path":"snapshot.bak"}' }
  })
  const log = vi.fn()

  await expect(preflightStateDb({ python: 'python', script: 'snapshot.py', home: 'profile', log })).resolves.toBeUndefined()
  expect(log).toHaveBeenCalledWith(expect.stringContaining('snapshot.bak'))
})

test('a fresh profile keeps a bounded snapshot budget and still refuses a failed backup', async () => {
  stat.mockImplementation(() => { throw new Error('ENOENT') })
  runSnapshot.mockRejectedValue(new Error('snapshot failed'))

  await expect(preflightStateDb({ python: 'python', script: 'snapshot.py', home: 'profile', log: vi.fn() }))
    .rejects.toThrow('Update cancelled before backend shutdown')
  const options = runSnapshot.mock.calls[0]![2]

  expect(options.timeout).toBeGreaterThan(0)
  expect(options.timeout).toBeLessThanOrEqual(1_800_000)
})
