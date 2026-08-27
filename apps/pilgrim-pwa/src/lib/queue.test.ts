/**
 * The offline queue's rules.
 *
 * The one that matters: **an SOS is never dropped.** Everything else in this
 * file is ordinary retry bookkeeping; that one is a safety property. If a
 * queued SOS is still in the queue then nobody has come, and the person may
 * still be in trouble — so no age and no attempt count may discard it.
 */

import { describe, expect, it } from 'vitest'

import {
  backoffSeconds,
  bodyToSend,
  dueNow,
  isDue,
  isEmergency,
  isExhausted,
  isExpired,
  isSendable,
  MAX_ATTEMPTS,
  sendOrder,
  shouldRetry,
  STALE_AFTER_SECONDS,
} from './queue'
import type { QueuedAction, QueuedKind } from './queue'

const NOW = new Date('2026-07-12T14:00:00Z')

function action(kind: QueuedKind, overrides: Partial<QueuedAction> = {}): QueuedAction {
  return {
    id: 'local-1',
    kind,
    body: {},
    queuedAt: NOW.toISOString(),
    attempts: 0,
    lastAttemptAt: null,
    lastError: null,
    ...overrides,
  }
}

function agedBy(kind: QueuedKind, seconds: number): QueuedAction {
  return action(kind, { queuedAt: new Date(NOW.getTime() - seconds * 1000).toISOString() })
}

// ---------------------------------------------------------------------------
// an SOS is never dropped
// ---------------------------------------------------------------------------
describe('an SOS is never dropped', () => {
  it('does not expire, however old', () => {
    // A crowd report from three hours ago is noise. An SOS from three hours ago
    // means nobody came.
    expect(isExpired(agedBy('sos', STALE_AFTER_SECONDS * 10), NOW)).toBe(false)
    expect(isExpired(agedBy('sos', 60 * 60 * 24), NOW)).toBe(false)
  })

  it('is never exhausted by attempts', () => {
    expect(isExhausted(action('sos', { attempts: MAX_ATTEMPTS * 100 }))).toBe(false)
  })

  it('stays sendable in conditions that would retire anything else', () => {
    const old = agedBy('sos', STALE_AFTER_SECONDS * 5)
    old.attempts = MAX_ATTEMPTS * 5
    expect(isSendable(old, NOW)).toBe(true)
  })

  it('is the one kind flagged as an emergency', () => {
    expect(isEmergency('sos')).toBe(true)
    expect(isEmergency('missing_person')).toBe(false)
    expect(isEmergency('crowd_report')).toBe(false)
  })
})

describe('everything else does retire', () => {
  it('expires once replaying it would mislead', () => {
    expect(isExpired(agedBy('crowd_report', STALE_AFTER_SECONDS - 1), NOW)).toBe(false)
    expect(isExpired(agedBy('crowd_report', STALE_AFTER_SECONDS + 1), NOW)).toBe(true)
  })

  it('gives up after the attempt ceiling', () => {
    expect(isExhausted(action('crowd_report', { attempts: MAX_ATTEMPTS - 1 }))).toBe(false)
    expect(isExhausted(action('crowd_report', { attempts: MAX_ATTEMPTS }))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// ordering
// ---------------------------------------------------------------------------
describe('sendOrder', () => {
  it('puts the SOS first even when it is the newest thing queued', () => {
    // A reconnecting phone may get exactly one request through before the
    // signal goes again. It should be the SOS.
    const queue = [
      agedBy('crowd_report', 600),
      agedBy('missing_person', 300),
      agedBy('sos', 5),
    ]
    expect(sendOrder(queue).map((a) => a.kind)).toEqual(['sos', 'crowd_report', 'missing_person'])
  })

  it('is oldest-first among equals', () => {
    const queue = [agedBy('crowd_report', 10), agedBy('crowd_report', 900)]
    const ordered = sendOrder(queue)
    expect(new Date(ordered[0]!.queuedAt).getTime()).toBeLessThan(
      new Date(ordered[1]!.queuedAt).getTime(),
    )
  })

  it('does not mutate the queue it was given', () => {
    const queue = [agedBy('crowd_report', 10), agedBy('sos', 900)]
    const snapshot = queue.map((a) => a.kind)
    sendOrder(queue)
    expect(queue.map((a) => a.kind)).toEqual(snapshot)
  })
})

// ---------------------------------------------------------------------------
// backoff
// ---------------------------------------------------------------------------
describe('backoff', () => {
  it('sends a brand-new action immediately', () => {
    expect(backoffSeconds(0)).toBe(0)
    expect(isDue(action('sos'), NOW)).toBe(true)
  })

  it('grows, then caps at five minutes', () => {
    expect(backoffSeconds(1)).toBe(2)
    expect(backoffSeconds(4)).toBe(16)
    expect(backoffSeconds(8)).toBe(256)
    expect(backoffSeconds(20)).toBe(300)
    expect(backoffSeconds(1000)).toBe(300)
  })

  it('holds an action back until its wait has elapsed', () => {
    const justTried = action('sos', {
      attempts: 4, // 16 seconds
      lastAttemptAt: new Date(NOW.getTime() - 5_000).toISOString(),
    })
    expect(isDue(justTried, NOW)).toBe(false)

    const waited = action('sos', {
      attempts: 4,
      lastAttemptAt: new Date(NOW.getTime() - 20_000).toISOString(),
    })
    expect(isDue(waited, NOW)).toBe(true)
  })
})

describe('dueNow', () => {
  it('returns only what is worth sending, in priority order', () => {
    const queue = [
      agedBy('crowd_report', STALE_AFTER_SECONDS + 60), // expired
      action('crowd_report', { attempts: MAX_ATTEMPTS }), // exhausted
      agedBy('crowd_report', 120), // fine
      agedBy('sos', 30), // fine, and first
    ]
    expect(dueNow(queue, NOW).map((a) => a.kind)).toEqual(['sos', 'crowd_report'])
  })
})

// ---------------------------------------------------------------------------
// the outgoing body
// ---------------------------------------------------------------------------
describe('bodyToSend', () => {
  it('attaches the client timestamp to an SOS', () => {
    // Without this the control room reads a twenty-minute-old emergency as a
    // fresh one, and the SLA clock starts at the wrong moment.
    const queued = agedBy('sos', 1200)
    expect(bodyToSend(queued).client_reported_at).toBe(queued.queuedAt)
  })

  it('leaves other kinds alone', () => {
    // Their server schemas have no such field, and an unknown field would get
    // the whole request rejected.
    const report = action('crowd_report', { body: { type: 'other' } })
    expect(bodyToSend(report)).toEqual({ type: 'other' })
    expect('client_reported_at' in bodyToSend(report)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// retry policy
// ---------------------------------------------------------------------------
describe('shouldRetry', () => {
  it('retries when the request never arrived', () => {
    expect(shouldRetry(null)).toBe(true)
  })

  it('retries a server fault', () => {
    expect(shouldRetry(500)).toBe(true)
    expect(shouldRetry(503)).toBe(true)
  })

  it('does not retry something the server understood and refused', () => {
    // Retrying sends the identical rejected body forever.
    expect(shouldRetry(400)).toBe(false)
    expect(shouldRetry(401)).toBe(false)
    expect(shouldRetry(422)).toBe(false)
  })

  it('retries a 429, which means later rather than no', () => {
    expect(shouldRetry(429)).toBe(true)
  })
})
