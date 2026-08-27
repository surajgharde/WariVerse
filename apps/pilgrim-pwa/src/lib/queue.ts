/**
 * Actions taken with no network (Section 4/M7).
 *
 * *"Actions taken offline (SOS, reports) queue in IndexedDB and sync on
 * reconnect with a visible pending state."*
 *
 * This module is the *policy* — what to send, in what order, how often to
 * retry, when to give up. The IndexedDB plumbing is in `db.ts`, kept apart so
 * the rules below can be tested without a browser, because the rules are the
 * part that has to be right.
 *
 * The decisions that shape it:
 *
 * **An SOS never expires and is never dropped.** Every other queued action has
 * a point past which replaying it is worse than forgetting it — a crowd report
 * from three hours ago is noise. An SOS is not: if it is still queued, nobody
 * has come, and the person may still be in trouble. `isExpired` returns false
 * for SOS at any age, and `MAX_ATTEMPTS` does not apply to it either.
 *
 * **The client's own timestamp travels with it.** `client_reported_at` is what
 * lets the control room see "this was pressed 20 minutes ago" rather than
 * treating a late arrival as a fresh emergency. The server uses it to start the
 * SLA clock at the right moment — see `incident_service.create`.
 *
 * **Order is oldest-first, but SOS jumps the queue.** A reconnecting phone with
 * an SOS and four crowd reports should send the SOS first, because the sync may
 * only get one request through before the signal goes again.
 */

export type QueuedKind = 'sos' | 'missing_person' | 'crowd_report'

export interface QueuedAction {
  /** Local id. Not a server id — this thing has never been to a server. */
  id: string
  kind: QueuedKind
  /** The request body, exactly as it will be POSTed. */
  body: Record<string, unknown>
  /** When the pilgrim actually did this, not when we managed to send it. */
  queuedAt: string
  attempts: number
  lastAttemptAt: string | null
  lastError: string | null
}

/** Where each kind of queued action gets sent. */
export const ENDPOINTS: Record<QueuedKind, string> = {
  sos: '/sos',
  missing_person: '/missing-persons',
  crowd_report: '/incidents',
}

/**
 * Give-up threshold — for everything except an SOS.
 *
 * Eight attempts with the backoff below spans roughly half an hour of trying.
 */
export const MAX_ATTEMPTS = 8

/**
 * Past this, replaying a non-emergency report does more harm than good.
 *
 * Six hours. A crowd report from this morning tells the control room about a
 * crowd that has dispersed, and acting on it wastes a responder.
 */
export const STALE_AFTER_SECONDS = 6 * 60 * 60

/** SOS is exempt from both of the above. See the module docstring. */
export function isEmergency(kind: QueuedKind): boolean {
  return kind === 'sos'
}

export function isExpired(action: QueuedAction, now: Date = new Date()): boolean {
  if (isEmergency(action.kind)) return false
  const age = (now.getTime() - new Date(action.queuedAt).getTime()) / 1000
  return age > STALE_AFTER_SECONDS
}

export function isExhausted(action: QueuedAction): boolean {
  if (isEmergency(action.kind)) return false
  return action.attempts >= MAX_ATTEMPTS
}

/** Anything that should still be attempted. */
export function isSendable(action: QueuedAction, now: Date = new Date()): boolean {
  return !isExpired(action, now) && !isExhausted(action)
}

/**
 * Seconds to wait before attempt number `attempts`.
 *
 * Exponential with a five-minute ceiling. No jitter, deliberately — unlike the
 * console's fleet of reconnecting sockets, this is one phone, and predictable
 * retry timing is easier to explain to somebody watching a "sending…" label.
 */
export function backoffSeconds(attempts: number): number {
  if (attempts <= 0) return 0
  return Math.min(2 ** attempts, 300)
}

export function isDue(action: QueuedAction, now: Date = new Date()): boolean {
  if (action.attempts === 0 || !action.lastAttemptAt) return true
  const since = (now.getTime() - new Date(action.lastAttemptAt).getTime()) / 1000
  return since >= backoffSeconds(action.attempts)
}

/**
 * The order to drain the queue in: emergencies first, then oldest first.
 *
 * A reconnecting phone may get one request through before the signal drops
 * again. That request should be the SOS.
 */
export function sendOrder(actions: QueuedAction[]): QueuedAction[] {
  return [...actions].sort((a, b) => {
    const emergency = Number(isEmergency(b.kind)) - Number(isEmergency(a.kind))
    if (emergency !== 0) return emergency
    return new Date(a.queuedAt).getTime() - new Date(b.queuedAt).getTime()
  })
}

/** What to send now: due, sendable, in priority order. */
export function dueNow(actions: QueuedAction[], now: Date = new Date()): QueuedAction[] {
  return sendOrder(actions.filter((a) => isSendable(a, now) && isDue(a, now)))
}

/**
 * Attach the client's own timestamp to the body on the way out.
 *
 * Without this the control room reads a twenty-minute-old emergency as a fresh
 * one, and the SLA clock starts at the wrong moment. Only set for kinds whose
 * server schema accepts it — sending an unknown field would be rejected whole.
 */
export function bodyToSend(action: QueuedAction): Record<string, unknown> {
  if (action.kind === 'sos') {
    return { ...action.body, client_reported_at: action.queuedAt }
  }
  return action.body
}

/**
 * Whether a failed attempt is worth repeating.
 *
 * A 4xx means the server understood and refused — retrying sends the identical
 * rejected body forever. A 5xx or a network error means it never arrived.
 *
 * 429 is the exception: it means "later", not "no". And on the SOS path the
 * server is built never to hard-block anyway (`incident_service.raise_sos`), so
 * a 429 there would be something else entirely and is still worth retrying.
 */
export function shouldRetry(status: number | null): boolean {
  if (status === null) return true // network error — it never got there
  if (status === 429) return true
  return status >= 500
}

export function describeStatus(status: number | null, lang: 'mr' | 'en' = 'mr'): string {
  if (status === null) {
    return lang === 'mr' ? 'नेटवर्क नाही — पाठवण्याच्या रांगेत' : 'No network — waiting to send'
  }
  if (shouldRetry(status)) {
    return lang === 'mr' ? 'पुन्हा प्रयत्न करत आहे' : 'Retrying'
  }
  return lang === 'mr' ? 'पाठवता आले नाही' : 'Could not be sent'
}
