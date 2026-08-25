/** Every Core API call the console makes, in one file. */

import { query, request } from './client'
import type {
  Alert,
  AlertRule,
  Breach,
  Camera,
  ChainReport,
  ClipResponse,
  ChangeDigest,
  ConsoleConfig,
  CrowdLive,
  DispatchOptions,
  Incident,
  IncidentSeverity,
  IncidentStatus,
  IncidentType,
  KpiStrip,
  LoginResult,
  MissingPerson,
  Page,
  ReplayWindow,
  Responder,
  ReviewStatus,
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

  /**
   * Development sign-in: a phone, no password, no TOTP.
   *
   * The server route is dead unless `ENVIRONMENT=development` and
   * `DEV_LOGIN_ENABLED=true`, and refuses to let the app boot in production
   * with the flag on. The console only ever calls this from a button that
   * `import.meta.env.DEV` gates, so it is absent from a production bundle
   * entirely rather than merely hidden.
   */
  devLogin: (phone: string) =>
    request<TokenResponse>('/auth/dev-login', {
      method: 'POST',
      body: { phone },
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

// ---------------------------------------------------------------------------
// incidents (Phase 5)
// ---------------------------------------------------------------------------
export const incidents = {
  list: (limit = 100) => request<Page<Incident>>(`/incidents${query({ open_only: true, limit })}`),

  get: (id: string) => request<Incident>(`/incidents/${id}`),

  /**
   * Open an incident from an alert.
   *
   * The alert's zone becomes the incident's zone, so the responder ranking has
   * somewhere to measure from. Severity is the operator's call, not the
   * alert's — an alert says a threshold was crossed; an incident says a person
   * needs help, and those are not the same judgement.
   */
  create: (body: {
    type: IncidentType
    severity: IncidentSeverity
    zone_id?: string | null
    location?: [number, number] | null
    description?: string | null
    source?: string
  }) => request<Incident>('/incidents', { method: 'POST', body }),

  update: (
    id: string,
    body: { status?: IncidentStatus; severity?: IncidentSeverity; note?: string; outcome_note?: string },
  ) => request<Incident>(`/incidents/${id}`, { method: 'PATCH', body }),

  /** Ranked units for a human to choose from. Suggestions only — never a decision. */
  dispatchOptions: (id: string) => request<DispatchOptions>(`/incidents/${id}/dispatch-options`),

  dispatch: (id: string, responderId: string, note?: string, overrideReason?: string) =>
    request<Incident>(`/incidents/${id}/dispatch`, {
      method: 'POST',
      body: { responder_id: responderId, note: note ?? null, override_reason: overrideReason ?? null },
    }),
}

export const breaches = {
  list: (limit = 100) => request<Page<Breach>>(`/breaches${query({ limit })}`),

  get: (id: string) => request<Breach>(`/breaches/${id}`),

  /**
   * A human's decision. Until this runs the record is a detection, not a
   * finding — Section 4/M5: "AI output alone is never a finding."
   */
  review: (id: string, status: ReviewStatus, reason?: string) =>
    request<Breach>(`/breaches/${id}/review`, {
      method: 'POST',
      body: { status, reason: reason ?? null },
    }),

  /**
   * Play back an evidence clip.
   *
   * A POST, not a GET, and it carries a password and a stated purpose. Both are
   * required by the server; sending them from here rather than opening a URL
   * means the view is re-authenticated and logged before any bytes move.
   */
  clip: (id: string, password: string, purpose: string) =>
    request<ClipResponse>(`/breaches/${id}/clip`, {
      method: 'POST',
      body: { password, purpose },
    }),

  /** Recompute every hash in the ledger. */
  verify: () => request<ChainReport>('/breaches/verify'),
}

export const responders = {
  list: () => request<Responder[]>('/responders'),
}

export const missingPersons = {
  list: (limit = 100) => request<Page<MissingPerson>>(`/missing-persons${query({ open_only: true, limit })}`),

  update: (id: string, status: string, note?: string) =>
    request<MissingPerson>(`/missing-persons/${id}`, {
      method: 'PATCH',
      body: { status, note: note ?? null },
    }),
}
