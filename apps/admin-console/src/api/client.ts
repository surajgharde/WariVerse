/**
 * The HTTP layer.
 *
 * Three things this does that a bare `fetch` wrapper would not:
 *
 * 1. **Unwraps the server's error envelope** into a typed `ApiError`, so every
 *    call site has `code`, `message_mr` and `trace_id` available. An operator
 *    who reports "it said something went wrong" is unhelpable; one who reports
 *    a trace id is one grep away from an answer.
 * 2. **Refreshes the access token once, and shares the attempt.** Access tokens
 *    live fifteen minutes and the console makes several concurrent requests, so
 *    a naive implementation fires N refreshes at once — and since the server
 *    rotates refresh tokens and revokes the whole family on reuse, the second
 *    one would log the operator out. `refreshing` below is that fix.
 * 3. **Refuses to swallow a failed refresh.** When re-auth genuinely fails the
 *    session is over, and the console says so rather than rendering a screen of
 *    empty rails that looks like a quiet temple.
 */

import type { ErrorDetail, TokenResponse } from './types'

const BASE = '/api/v1'

/** Where tokens live between reloads.
 *
 * `sessionStorage`, not `localStorage`: a shared control-room workstation must
 * not leave an authenticated session behind for whoever sits down next. Closing
 * the tab ends it. This is a weaker guarantee than an httpOnly cookie and is
 * chosen knowingly — a cookie brings CSRF into a system that currently has
 * none, and the WebSocket cannot send a header anyway (see `ws.py`).
 */
const ACCESS_KEY = 'wariverse.access'
const REFRESH_KEY = 'wariverse.refresh'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly messageMr: string
  readonly details: Record<string, unknown>
  readonly traceId: string | null

  constructor(status: number, detail: ErrorDetail) {
    super(detail.message)
    this.name = 'ApiError'
    this.status = status
    this.code = detail.code
    this.messageMr = detail.message_mr
    this.details = detail.details
    this.traceId = detail.trace_id
  }
}

/** Thrown when the session cannot be recovered. The shell renders the sign-in screen. */
export class SessionExpired extends Error {
  constructor() {
    super('Session expired')
    this.name = 'SessionExpired'
  }
}

export const tokens = {
  access: (): string | null => sessionStorage.getItem(ACCESS_KEY),
  refresh: (): string | null => sessionStorage.getItem(REFRESH_KEY),
  set(pair: { access_token: string; refresh_token: string }): void {
    sessionStorage.setItem(ACCESS_KEY, pair.access_token)
    sessionStorage.setItem(REFRESH_KEY, pair.refresh_token)
  },
  clear(): void {
    sessionStorage.removeItem(ACCESS_KEY)
    sessionStorage.removeItem(REFRESH_KEY)
  },
}

/** In-flight refresh, shared by every caller that hits a 401 at the same moment. */
let refreshing: Promise<string> | null = null

async function refreshAccessToken(): Promise<string> {
  const token = tokens.refresh()
  if (!token) throw new SessionExpired()

  const response = await fetch(`${BASE}/auth/refresh`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ refresh_token: token }),
  })

  if (!response.ok) {
    // The refresh token is spent, revoked, or its family was killed because
    // someone replayed a superseded one. Either way this session is over.
    tokens.clear()
    throw new SessionExpired()
  }

  const pair = (await response.json()) as TokenResponse
  tokens.set(pair)
  return pair.access_token
}

async function sharedRefresh(): Promise<string> {
  refreshing ??= refreshAccessToken().finally(() => {
    refreshing = null
  })
  return refreshing
}

interface RequestOptions {
  method?: string
  body?: unknown
  /** Set for sign-in calls, which must not attempt a refresh on 401. */
  anonymous?: boolean
  signal?: AbortSignal
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: ErrorDetail }
    if (body.error) return new ApiError(response.status, body.error)
  } catch {
    // Fall through: a proxy returning HTML for a 502 is not the server's envelope.
  }
  return new ApiError(response.status, {
    code: `HTTP_${response.status}`,
    message: response.statusText || 'Request failed',
    message_mr: 'विनंती अयशस्वी झाली',
    details: {},
    trace_id: null,
  })
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, anonymous = false, signal } = options

  const send = async (accessToken: string | null): Promise<Response> => {
    const headers: Record<string, string> = {}
    if (body !== undefined) headers['content-type'] = 'application/json'
    if (accessToken) headers['authorization'] = `Bearer ${accessToken}`
    return fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  }

  let response = await send(anonymous ? null : tokens.access())

  if (response.status === 401 && !anonymous) {
    const fresh = await sharedRefresh()
    response = await send(fresh)
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** Build a query string, dropping undefined and null so callers can pass optionals directly. */
export function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue
    search.append(key, String(value))
  }
  const out = search.toString()
  return out ? `?${out}` : ''
}
