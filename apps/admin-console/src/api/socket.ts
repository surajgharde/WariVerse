/**
 * The live event socket (`/api/v1/ws/crowd`).
 *
 * Section 9's requirements for a client are explicit: handle reconnect, run a
 * heartbeat, back off automatically. What follows is those three, plus the one
 * the spec implies and does not say — **a socket that has gone quiet must be
 * treated as dead, not as a quiet temple.**
 *
 * That is the whole reason `staleAfterMs` exists. A TCP connection through a
 * proxy can stay open long after it has stopped delivering anything. The
 * browser will not tell us; `readyState` stays OPEN. So the client watches the
 * clock instead: the server promises a frame every `heartbeat_seconds`, and if
 * two of those pass in silence we tear the socket down and reconnect rather
 * than leave an operator looking at a frozen map that appears live.
 *
 * Backoff is exponential with jitter. Without the jitter, two hundred operator
 * consoles knocked offline by the same network blip would reconnect in
 * lockstep and do to the API what the blip did not.
 */

import type { ServerEvent } from './types'

export type SocketState = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface SocketHandlers {
  onEvent: (event: ServerEvent) => void
  onState: (state: SocketState, detail?: { attempt?: number; nextRetryMs?: number }) => void
  /** Called when the server rejects the token, so the shell can re-authenticate. */
  onAuthFailure: () => void
}

const BASE_RETRY_MS = 1_000
const MAX_RETRY_MS = 30_000
/** The server's default; overridden by the `hello` frame it sends on connect. */
const DEFAULT_HEARTBEAT_S = 20

function backoffMs(attempt: number): number {
  const capped = Math.min(BASE_RETRY_MS * 2 ** attempt, MAX_RETRY_MS)
  // Full jitter: spread a fleet-wide reconnect across the whole window.
  return Math.round(capped * (0.5 + Math.random() * 0.5))
}

function socketUrl(token: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  // The token rides in the query string because a browser cannot set a header
  // on a WebSocket. See the note at the top of `ws.py` — this needs TLS in
  // front of it, and a proxy configured not to log query strings for /ws/.
  return `${scheme}://${window.location.host}/api/v1/ws/crowd?token=${encodeURIComponent(token)}`
}

export class CrowdSocket {
  private ws: WebSocket | null = null
  private attempt = 0
  private heartbeatSeconds = DEFAULT_HEARTBEAT_S
  private retryTimer: number | null = null
  private watchdog: number | null = null
  private lastFrameAt = 0
  private stopped = false

  constructor(
    private readonly getToken: () => string | null,
    private readonly handlers: SocketHandlers,
  ) {}

  start(): void {
    this.stopped = false
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.clearTimers()
    // 1000 = normal closure. Anything else and the server logs a disconnect it
    // has to wonder about.
    this.ws?.close(1000, 'console closed')
    this.ws = null
    this.handlers.onState('closed')
  }

  private clearTimers(): void {
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer)
    if (this.watchdog !== null) window.clearInterval(this.watchdog)
    this.retryTimer = null
    this.watchdog = null
  }

  private connect(): void {
    const token = this.getToken()
    if (!token) {
      this.handlers.onAuthFailure()
      return
    }

    this.handlers.onState(this.attempt === 0 ? 'connecting' : 'reconnecting', {
      attempt: this.attempt,
    })

    const ws = new WebSocket(socketUrl(token))
    this.ws = ws

    ws.onopen = () => {
      this.attempt = 0
      this.lastFrameAt = Date.now()
      this.handlers.onState('open')
      this.startWatchdog()
    }

    ws.onmessage = (message) => {
      this.lastFrameAt = Date.now()
      let event: ServerEvent
      try {
        event = JSON.parse(message.data as string) as ServerEvent
      } catch {
        // A frame we cannot parse is a bug on one side or the other, but it is
        // not a reason to drop a working socket.
        return
      }
      if (event.type === 'hello') {
        this.heartbeatSeconds = event.heartbeat_seconds || DEFAULT_HEARTBEAT_S
      }
      this.handlers.onEvent(event)
    }

    ws.onclose = (event) => {
      this.clearTimers()
      if (this.stopped) return

      // 1008 is the policy violation the server sends when the token is bad or
      // the role lost its permission. Retrying that in a loop would hammer the
      // API with a credential that is never going to work.
      if (event.code === 1008) {
        this.handlers.onState('closed')
        this.handlers.onAuthFailure()
        return
      }
      this.scheduleReconnect()
    }

    ws.onerror = () => {
      // `onclose` always follows; scheduling here too would double the backoff.
    }
  }

  /**
   * Silence detection. The server sends a heartbeat every `heartbeat_seconds`;
   * missing two of them means the socket is open and dead, which looks exactly
   * like a calm evening to anyone watching the map.
   */
  private startWatchdog(): void {
    if (this.watchdog !== null) window.clearInterval(this.watchdog)
    this.watchdog = window.setInterval(() => {
      const silentMs = Date.now() - this.lastFrameAt
      if (silentMs > this.heartbeatSeconds * 2 * 1000) {
        this.ws?.close(4000, 'no heartbeat')
      } else if (silentMs > this.heartbeatSeconds * 1000) {
        // Prod it before giving up — the server answers `{"type":"ping"}` with
        // a pong, which is a cheaper way to learn the truth than a reconnect.
        this.send({ type: 'ping' })
      }
    }, 5_000)
  }

  private send(payload: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload))
    }
  }

  private scheduleReconnect(): void {
    const delay = backoffMs(this.attempt)
    this.attempt += 1
    this.handlers.onState('reconnecting', { attempt: this.attempt, nextRetryMs: delay })
    this.retryTimer = window.setTimeout(() => this.connect(), delay)
  }
}

/** Exported for the unit test; the backoff curve is worth pinning down. */
export const _internals = { backoffMs, BASE_RETRY_MS, MAX_RETRY_MS }
