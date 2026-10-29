import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { getStatus } from '@/hermes'

import { deferred } from '../../../test/deferred'

import { useStatusSnapshot } from './use-status-snapshot'

vi.mock('@/hermes', () => ({ getStatus: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('publishes inference readiness while optional account metadata is still pending', async () => {
  vi.useFakeTimers()
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.mocked(getStatus).mockResolvedValue({} as never)
  const account = deferred<unknown>()

  const requestGateway = vi.fn(async (method: string) => {
    if (method === 'free_tier.status') {
      return account.promise
    }

    return method === 'setup.runtime_check' ? { ok: true } : { provider_configured: true }
  }) as <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

  const { result } = renderHook(() => useStatusSnapshot('open', requestGateway))

  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(result.current.inferenceStatus).toMatchObject({ ready: true, source: 'runtime_check' })
  expect(requestGateway).toHaveBeenCalledWith('free_tier.status')
  await act(async () => account.resolve({}))
})
