/**
 * The network layer, written for a network that is usually not there.
 *
 * Every read goes through `cached()`: try the network with a short timeout,
 * fall back to whatever IndexedDB holds, and **always tell the caller how old
 * the answer is**. There is no code path that returns data without its age,
 * because the screens are required to render that age and a function that can
 * omit it is a function that eventually will.
 *
 * The timeout is deliberately short. On 2G a request can hang for a minute;
 * a pilgrim looking for the nearest toilet would rather have yesterday's list
 * now than today's list eventually.
 */

import { cacheStore } from './db'
import type { CachedAt } from './db'

const BASE = '/api/v1'

/** Long enough for a slow 2G round trip, short enough not to feel broken. */
const TIMEOUT_MS = 8_000

const ACCESS_KEY = 'wariverse.pilgrim.access'
const REFRESH_KEY = 'wariverse.pilgrim.refresh'

/**
 * localStorage here, unlike the admin console's sessionStorage.
 *
 * Opposite threat models. The console runs on a shared control-room
 * workstation, where a session outliving its operator is the risk. This runs on
 * a personal phone in a pocket, where being signed out mid-Wari with no network
 * to sign back in with is the risk. Persistence is the safer default here.
 */
export const tokens = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  set(pair: { access_token: string; refresh_token: string }) {
    localStorage.setItem(ACCESS_KEY, pair.access_token)
    localStorage.setItem(REFRESH_KEY, pair.refresh_token)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly messageMr: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export interface RequestOptions {
  method?: string
  body?: unknown
  anonymous?: boolean
  timeoutMs?: number
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, anonymous = false, timeoutMs = TIMEOUT_MS } = options

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const headers: Record<string, string> = {}
    if (body !== undefined) headers['content-type'] = 'application/json'
    const token = anonymous ? null : tokens.access()
    if (token) headers.authorization = `Bearer ${token}`

    const response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })

    if (!response.ok) {
      let code = `HTTP_${response.status}`
      let message = response.statusText
      let messageMr = 'विनंती अयशस्वी झाली'
      try {
        const envelope = (await response.json()) as {
          error?: { code: string; message: string; message_mr: string }
        }
        if (envelope.error) {
          code = envelope.error.code
          message = envelope.error.message
          messageMr = envelope.error.message_mr
        }
      } catch {
        /* not our envelope — a proxy error page, most likely */
      }
      throw new ApiError(response.status, code, message, messageMr)
    }

    return response.status === 204 ? (undefined as T) : ((await response.json()) as T)
  } finally {
    clearTimeout(timer)
  }
}

/** What every read returns: the value, its age, and where it came from. */
export interface Fresh<T> {
  value: T
  /** ISO time the data was *fetched*, not generated. */
  fetchedAt: string
  /** True when the network failed and this came out of IndexedDB. */
  fromCache: boolean
}

/**
 * Read with a cache fallback.
 *
 * Network first, because a live answer is better when one is available. Cache
 * second, because an old answer is better than a spinner. `null` only when
 * there is neither — and the screens render that as "we do not know", never as
 * an empty list that looks like "there is nothing there".
 */
export async function cached<T>(
  key: string,
  path: string,
  options: RequestOptions = {},
): Promise<Fresh<T> | null> {
  try {
    const value = await request<T>(path, options)
    void cacheStore.put(key, value)
    return { value, fetchedAt: new Date().toISOString(), fromCache: false }
  } catch {
    const stored = (await cacheStore.get<T>(key).catch(() => undefined)) as CachedAt<T> | undefined
    if (!stored) return null
    return { value: stored.value, fetchedAt: stored.fetchedAt, fromCache: true }
  }
}

export const auth = {
  requestOtp: (phone: string) =>
    request<{ sent: boolean; expires_in: number; debug_code?: string }>('/auth/otp/request', {
      method: 'POST',
      body: { phone },
      anonymous: true,
    }),

  verifyOtp: (phone: string, code: string, name?: string) =>
    request<{ access_token: string; refresh_token: string; user: { id: string; name: string } }>(
      '/auth/otp/verify',
      { method: 'POST', body: { phone, code, name, language: 'mr' }, anonymous: true },
    ),
}
