/**
 * Reconnect backoff.
 *
 * The property that matters is not the exact curve, it is the jitter. Two
 * hundred operator consoles knocked offline by one network blip must not
 * reconnect in lockstep — doing so would let a blip the API survived be
 * followed by a thundering herd it does not.
 */

import { describe, expect, it } from 'vitest'

import { _internals } from './socket'

const { backoffMs, BASE_RETRY_MS, MAX_RETRY_MS } = _internals

describe('backoffMs', () => {
  it('grows with each attempt', () => {
    const first = median(0)
    const second = median(1)
    const third = median(2)
    expect(second).toBeGreaterThan(first)
    expect(third).toBeGreaterThan(second)
  })

  it('never exceeds the cap, however many attempts have failed', () => {
    for (const attempt of [10, 20, 50, 1000]) {
      expect(backoffMs(attempt)).toBeLessThanOrEqual(MAX_RETRY_MS)
    }
  })

  it('never retries faster than half the base delay', () => {
    // Full jitter halves the delay at worst. A console that retried instantly
    // on every close would be a denial-of-service against its own API.
    for (let i = 0; i < 200; i += 1) {
      expect(backoffMs(0)).toBeGreaterThanOrEqual(BASE_RETRY_MS / 2)
    }
  })

  it('spreads a fleet-wide reconnect rather than synchronising it', () => {
    // Two hundred consoles, same attempt number. If the delays were
    // deterministic every one of them would land in the same millisecond.
    const delays = new Set(Array.from({ length: 200 }, () => backoffMs(4)))
    expect(delays.size).toBeGreaterThan(50)
  })
})

function median(attempt: number): number {
  const samples = Array.from({ length: 201 }, () => backoffMs(attempt)).sort((a, b) => a - b)
  return samples[100]!
}
