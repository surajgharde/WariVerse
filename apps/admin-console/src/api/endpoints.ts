/** Every Core API call the console makes, in one file. */

import { query, request } from './client'
import type {
  Alert,
  AlertRule,
  Camera,
  ChangeDigest,
  ConsoleConfig,
  CrowdLive,
  KpiStrip,
  LoginResult,
  Page,
  ReplayWindow,
  TokenResponse,
  UserProfile,
  Zone,
} from './types'

// ---------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------
export const auth = {
  /** Staff sign-in. Administrators get an MFA challenge instead of tokens. */
  login: (phone: string, password: string) =>
    request<LoginResult>('/auth/login', {
      method: 'POST',
      body: { phone, password },
      anonymous: true,
    }),

  verifyMfa: (mfaToken: string, code: string) =>
    request<TokenResponse>('/auth/mfa/verify', {
      method: 'POST',
      body: { mfa_token: mfaToken, code },
      anonymous: true,
    }),

  me: () => request<UserProfile>('/auth/me'),

  logout: (refreshToken: string | null) =>
    request<{ ok: boolean }>('/auth/logout', {
      method: 'POST',
      body: { refresh_token: refreshToken },
    }),
}

// ---------------------------------------------------------------------------
// command centre
// ---------------------------------------------------------------------------
export const command = {
  config: () => request<ConsoleConfig>('/command/config'),
  kpis: () => request<KpiStrip>('/command/kpis'),
  changes: (minutes = 15) => request<ChangeDigest>(`/command/changes${query({ minutes })}`),
  replay: (minutes = 60, zones?: string[]) =>
    request<ReplayWindow>(
      `/command/replay${query({ minutes })}${(zones ?? []).map((z) => `&zones=${encodeURIComponent(z)}`).join('')}`,
    ),
}

// ---------------------------------------------------------------------------
// crowd
// ---------------------------------------------------------------------------
export const crowd = {
  zones: () => request<Zone[]>('/zones'),
  live: () => request<CrowdLive>('/crowd/live'),
  cameras: () => request<Camera[]>('/cameras'),
}

// ---------------------------------------------------------------------------
// alerts
// ---------------------------------------------------------------------------
export const alerts = {
  list: (limit = 100) => request<Page<Alert>>(`/alerts${query({ live_only: true, limit })}`),

  /**
   * Acknowledging stops the escalation clock, so it goes over HTTP where it is
   * audited — never over the socket. An acknowledgement with no request id is
   * one nobody can reconstruct six months later.
   */
  acknowledge: (id: string, note?: string) =>
    request<Alert>(`/alerts/${id}/ack`, { method: 'POST', body: { note: note ?? null } }),

  resolve: (id: string, resolution: string) =>
    request<Alert>(`/alerts/${id}/resolve`, { method: 'POST', body: { resolution } }),

  /** The rule table — the answer to "why is it telling me to close the gate". */
  rules: () => request<AlertRule[]>('/alerts/rules'),
}
