import { execFileSync } from 'node:child_process'
import { statSync } from 'node:fs'
import path from 'node:path'

import { beforeEach, expect, test, vi } from 'vitest'

import { preflightStateDb } from './state-db-preflight'

vi.mock('node:child_process', () => ({ execFileSync: vi.fn() }))
vi.mock('node:fs', () => ({ statSync: vi.fn() }))

beforeEach(() => vi.resetAllMocks())

test('a large database and pending WAL can finish their snapshot before shutdown', () => {
  vi.mocked(statSync).mockImplementation((file) => ({
    size: path.basename(String(file)) === 'state.db' ? 2 * 1024 ** 3 : 1024 ** 3
  }) as ReturnType<typeof statSync>)
  vi.mocked(execFileSync).mockImplementation((_command, _args, options) => {
    if ((options?.timeout ?? 0) < 330_000) {
      throw new Error('ETIMEDOUT')
    }

    return '{"path":"snapshot.bak"}'
  })
  const log = vi.fn()

  expect(() => preflightStateDb({ python: 'python', script: 'snapshot.py', home: 'profile', log })).not.toThrow()
  expect(log).toHaveBeenCalledWith(expect.stringContaining('snapshot.bak'))
})

test('a fresh profile keeps a bounded snapshot budget and still refuses a failed backup', () => {
  vi.mocked(statSync).mockReturnValue(undefined as ReturnType<typeof statSync>)
  vi.mocked(execFileSync).mockImplementation(() => { throw new Error('snapshot failed') })

  expect(() => preflightStateDb({ python: 'python', script: 'snapshot.py', home: 'profile', log: vi.fn() }))
    .toThrow('Update cancelled before backend shutdown')
  expect(execFileSync).toHaveBeenCalledWith('python', expect.any(Array), expect.objectContaining({ timeout: 30_000 }))
})
