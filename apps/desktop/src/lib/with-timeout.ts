/** Initial descriptor/native ticket resolution can include runtime discovery,
 * the main process's 180s ensure protocol and its 15s client grace. Out-wait that
 * chain so a valid cold boot cannot complete behind a latched failure screen.
 * Steady-state reconnects retain their shorter bound below. */
export const BACKEND_BOOT_WAIT_TIMEOUT_MS = 240_000

// desktop.getConnection() / getConnectionFor() / revalidateConnection() /
// resolveGatewayWsUrl() are IPC round-trips into the main process with no
// timeout of their own (#93454). A wedged main-process round-trip (e.g. a
// stuck revalidation after a liveness-probe trip) otherwise hangs an awaiting
// caller forever. Every caller of these bounds them with this shared budget.
export const RECONNECT_ATTEMPT_TIMEOUT_MS = 20_000

/** Budget for each bounded step of a source switch (store/connections
 * selectConnection): the phase-1 open of the target, the OAuth REST probe and
 * the activation (ensureGatewayAgent) each get this whole budget, as does a
 * registry secondary's getConnectionFor in store/gateway. It is sized for the
 * worst of them, the cold dial. Unlike the two above, the cold dial is not one IPC:
 * it is the main process's whole remote bring-up chain — one ssh connect
 * (DEFAULT_CONNECT_TIMEOUT_MS 15 s in electron/ssh-connection.ts), the
 * sequential ssh execs that probe platform, locate the runtime and read its
 * version (DEFAULT_EXEC_TIMEOUT_MS 20 s each), the spawned backend's ready
 * sentinel (DEFAULT_READY_TIMEOUT_MS 45 s in electron/remote-lifecycle.ts) and
 * the port forward (DEFAULT_FORWARD_TIMEOUT_MS 15 s). Their sum is ~135 s, and
 * a *healthy* cold dial lands far inside it — measured 37-64 s against a 2 GB
 * VPS over Windows ssh (no mux: every stage is a fresh ssh process plus a cold
 * `serve` boot).
 *
 * Bounding that chain with RECONNECT_ATTEMPT_TIMEOUT_MS reported
 * "Could not connect to <source>" for switches that were still dialing, and
 * then connected anyway (withTimeout does not cancel the dial), which read as a
 * failure the user had to retry. The per-IPC budget still catches the #93454
 * class from inside the dial; this one only has to outlast a legitimate dial. */
export const SOURCE_SWITCH_DIAL_TIMEOUT_MS = 135_000

/** Rejection raised by withTimeout. The bounded work is NOT cancelled — the
 * caller decides what a straggler that settles later means. */
export class TimeoutError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'TimeoutError'
  }
}

export function isTimeoutError(error: unknown): error is TimeoutError {
  return error instanceof TimeoutError
}

/** Settle with `promise`, or reject with a TimeoutError after `ms`.
 * `onTimeout` runs synchronously before the rejection is published so callers
 * can revoke ownership of work that would otherwise keep running unowned. If
 * that callback throws, its error becomes this promise's rejection.
 * An explicit `pauseWhile` hold pauses the budget without cancelling the work. */
export function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  message: string,
  onTimeout?: (error: TimeoutError) => void,
  pauseWhile?: () => boolean
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const expire = () => {
      const error = new TimeoutError(message)

      try {
        onTimeout?.(error)
      } catch (onTimeoutError) {
        reject(onTimeoutError)

        return
      }

      reject(error)
    }

    // A live updater owns its own wait policy. Charge only backend startup time,
    // then resume the remaining budget when the explicit update hold clears.
    let remaining = ms
    let checkedAt = performance.now()
    let paused = Boolean(pauseWhile?.())

    const tick = () => {
      const now = performance.now()
      const nextPaused = Boolean(pauseWhile?.())

      if (!paused && !nextPaused) { remaining -= now - checkedAt }
      checkedAt = now
      paused = nextPaused

      if (remaining <= 0) { expire();

 return }

      timer = setTimeout(tick, Math.min(1000, remaining))
    }

    let timer = setTimeout(pauseWhile ? tick : expire, pauseWhile ? Math.min(1000, ms) : ms)

    Promise.resolve(promise).then(
      value => {
        clearTimeout(timer)
        resolve(value)
      },
      err => {
        clearTimeout(timer)
        reject(err)
      }
    )
  })
}
