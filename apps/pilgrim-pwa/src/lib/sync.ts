/**
 * Draining the offline queue.
 *
 * The policy lives in `queue.ts` and is tested there; this is the part that
 * actually talks to the network. Kept apart so the rules can be exercised
 * without a browser, a server, or a fake for either.
 *
 * One attempt per action per drain, on purpose. Hammering a flaky 2G link with
 * four retries in a row wastes the window that a returning signal opens; better
 * to send one, and let the next drain thirty seconds later send the next.
 */

import { request, ApiError } from './api'
import { queueStore } from './db'
import { bodyToSend, dueNow, ENDPOINTS, isSendable, shouldRetry } from './queue'
import type { QueuedAction } from './queue'

export interface DrainResult {
  sent: number
  failed: number
  remaining: number
}

export async function drainQueue(now: Date = new Date()): Promise<DrainResult> {
  const all = await queueStore.all().catch(() => [] as QueuedAction[])
  let sent = 0
  let failed = 0

  for (const action of dueNow(all, now)) {
    try {
      await request(ENDPOINTS[action.kind], {
        method: 'POST',
        body: bodyToSend(action),
      })
      await queueStore.remove(action.id)
      sent += 1
    } catch (exc) {
      failed += 1
      const status = exc instanceof ApiError ? exc.status : null

      if (!shouldRetry(status)) {
        // The server understood and refused. Retrying sends the identical
        // rejected body forever — except for an SOS, which is kept regardless
        // so a human can see it was attempted rather than have it vanish.
        if (action.kind !== 'sos') {
          await queueStore.remove(action.id)
          continue
        }
      }

      await queueStore.put({
        ...action,
        attempts: action.attempts + 1,
        lastAttemptAt: now.toISOString(),
        lastError: exc instanceof Error ? exc.message : 'unknown',
      })
    }
  }

  const left = await queueStore.all().catch(() => [] as QueuedAction[])
  return { sent, failed, remaining: left.filter((a) => isSendable(a, now)).length }
}
