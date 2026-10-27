import { describe, expect, it, vi } from 'vitest'

import { withTimeout } from './with-timeout'

describe('withTimeout', () => {
  it('pauses for a live update but still bounds a backend that hangs after the hold clears', async () => {
    vi.useFakeTimers()
    let held = true

    try {
      const result = withTimeout(new Promise<never>(() => undefined), 2000, 'startup expired', undefined, () => held)
      let expired = false
      void result.catch(() => { expired = true })
      const rejected = expect(result).rejects.toThrow('startup expired')
      await vi.advanceTimersByTimeAsync(300_000)
      expect(expired).toBe(false)
      held = false
      await vi.advanceTimersByTimeAsync(3000)
      await rejected
    } finally { vi.useRealTimers() }
  })

  it('rejects with an onTimeout exception instead of letting it escape the timer callback', async () => {
    vi.useFakeTimers()

    try {
      const callbackFailure = new Error('abort callback failed')

      const result = withTimeout(new Promise<never>(() => undefined), 10, 'work timed out', () => {
        throw callbackFailure
      })

      const rejection = expect(result).rejects.toBe(callbackFailure)

      await vi.advanceTimersByTimeAsync(10)
      await rejection
    } finally {
      vi.useRealTimers()
    }
  })
})
