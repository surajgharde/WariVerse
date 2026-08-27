/** Every Core API call the console makes, in one file. */

import { query, request } from './client'
import type {
  Alert,
  AlertRule,
  AssistanceRequest,
  AssistanceStatus,
  Breach,
  Camera,
  ChainReport,
  ClipResponse,
  ChangeDigest,
  ConsoleConfig,
  CrowdLive,
  DispatchOptions,
  HeritageItem,
  Incident,
  IncidentSeverity,
  IncidentStatus,
  IncidentType,
  ItemCategory,
  KpiStrip,
  LoginResult,
  LostFoundItem,
  LostFoundKind,
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

/**
 * Lost and found property — the desk's side (Track 1, item 2).
 *
 * Note what is absent: there is no "auto-match" call. `decide` is the only way a
 * pairing is ever made, and it takes a human's session token. The server ranks
 * candidates and stores them; it never accepts one on its own.
 */
/**
 * Assistance requests (Track 1, item 4).
 *
 * There is no `profiles` call here and there is not going to be one: a declared
 * disability is health data, the server exposes it only to its owner, and a
 * console that could list them would be the list worth stealing.
 */
/**
 * Heritage moderation (Track 1, item 5).
 *
 * There is no `delete` here, and there is no endpoint behind one. A rejected
 * contribution keeps its text and gains a reason; destroying the only copy
 * anybody typed out is not a moderation outcome this system offers.
 */
export const heritage = {
  queue: (limit = 50) => request<Page<HeritageItem>>(`/heritage/review/queue${query({ limit })}`),

  review: (id: string, publish: boolean, note?: string) =>
    request<HeritageItem>(`/heritage/${id}/review`, {
      method: 'POST',
      body: { publish, note: note ?? null },
    }),

  /** A typo, or the source somebody forgot. Does not unpublish. */
  correct: (id: string, body: Partial<Pick<HeritageItem, 'title_mr' | 'body_mr' | 'attribution' | 'source' | 'era'>>) =>
    request<HeritageItem>(`/heritage/${id}`, { method: 'PATCH', body }),
}

export const assistance = {
  list: (limit = 100) =>
    request<Page<AssistanceRequest>>(`/assistance${query({ open_only: true, limit })}`),

  update: (
    id: string,
    body: { claim?: boolean; status?: AssistanceStatus; outcome_note?: string },
  ) => request<AssistanceRequest>(`/assistance/${id}`, { method: 'PATCH', body }),

  /** Record what a volunteer found when they walked up to a facility. */
  surveyFacility: (
    facilityId: string,
    flags: Partial<
      Record<'step_free' | 'ramp' | 'accessible_toilet' | 'seating' | 'staffed' | 'handrail', boolean>
    >,
  ) =>
    request<{ facility_id: string; accessibility: Record<string, boolean> }>(
      `/accessibility/facilities/${facilityId}`,
      { method: 'PATCH', body: flags },
    ),
}

export const lostFound = {
  list: (kind?: LostFoundKind, limit = 100) =>
    request<Page<LostFoundItem>>(`/lost-found${query({ kind, open_only: true, limit })}`),

  get: (id: string) => request<LostFoundItem>(`/lost-found/${id}`),

  registerFound: (body: {
    category: ItemCategory
    description: string
    colour?: string | null
    distinguishing_marks?: string | null
    zone_id?: string | null
    custody_facility_id?: string | null
  }) => request<LostFoundItem>('/lost-found/found', { method: 'POST', body }),

  /** Accept or reject a suggested pairing. Always a person, never a threshold. */
  decide: (id: string, counterpartId: string, kind: LostFoundKind, accept: boolean, note?: string) =>
    request<LostFoundItem>(`/lost-found/${id}/match`, {
      method: 'POST',
      body: {
        // The counterpart is whichever side this record is not.
        [kind === 'lost' ? 'found_item_id' : 'lost_item_id']: counterpartId,
        accept,
        note: note ?? null,
      },
    }),

  /** The moment an object leaves the shelf. Both names are mandatory. */
  handover: (id: string, claimantName: string, note: string, claimantPhone?: string) =>
    request<LostFoundItem>(`/lost-found/${id}/handover`, {
      method: 'POST',
      body: { claimant_name: claimantName, note, claimant_phone: claimantPhone ?? null },
    }),
}
