/**
 * Wire types, mirroring the Core API's Pydantic schemas.
 *
 * Hand-written rather than generated. Phase 4 consumes eleven endpoints out of
 * a much larger surface, and a generated client would drag in every schema the
 * console never touches. When the surface grows past what is comfortable here,
 * generate into `packages/api-client` and delete this file — the shapes below
 * are deliberately named to match the server's so that swap is mechanical.
 *
 * One convention throughout, inherited from the server: **`null` means "not
 * measured", and is never interchangeable with `0`.** Every optional number in
 * this file carries that meaning, and the components that render them are
 * expected to branch on it rather than default it away with `?? 0`.
 */

export type DensityLevel = 'safe' | 'moderate' | 'high' | 'critical'
export type AlertSeverity = 'info' | 'warning' | 'critical'
export type AlertStatus = 'open' | 'acknowledged' | 'escalated' | 'resolved' | 'expired'
export type CameraStatus = 'online' | 'degraded' | 'offline'
export type CrowdSource = 'live' | 'video' | 'sim'

/** Server error envelope. Every failure, including validation, arrives shaped like this. */
export interface ErrorDetail {
  code: string
  message: string
  message_mr: string
  details: Record<string, unknown>
  trace_id: string | null
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

// ---------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------
export interface UserProfile {
  id: string
  name: string
  role: string
  language: string
  permissions: string[]
  mfa_enrolled: boolean
  phone_masked: string | null
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: UserProfile
}

/**
 * Administrator and System Admin get this instead of tokens — sign-in is not
 * finished until `/auth/mfa/verify` is called with a code.
 */
export interface MfaChallenge {
  mfa_required: true
  mfa_token: string
  expires_in: number
}

export type LoginResult = TokenResponse | MfaChallenge

export function isMfaChallenge(result: LoginResult): result is MfaChallenge {
  return 'mfa_required' in result && result.mfa_required
}

// ---------------------------------------------------------------------------
// zones and crowd
// ---------------------------------------------------------------------------
export interface Zone {
  id: string
  code: string
  name: string
  name_mr: string
  zone_type: string
  area_m2: number
  capacity_persons: number
  is_active: boolean
  parent_zone_id: string | null
  /** GeoJSON Polygon, ready for MapLibre without a conversion step. */
  geometry: GeoJSON.Polygon | null
  camera_count: number
  calibrated_camera_count: number
}

export interface Flow {
  speed_ms: number
  /** Compass point, or null when the crowd is barely moving. */
  direction: string | null
  dx: number
  dy: number
}

export interface ZoneStatus {
  zone_id: string
  zone_code: string
  zone_name: string
  zone_name_mr: string
  person_count: number
  density: number
  level: DensityLevel
  occupancy_pct: number | null
  flow: Flow
  stagnation_index: number
  counterflow_ratio: number
  confidence: number
  source: string
  camera_count: number
  observed_at: string
  age_seconds: number
  is_stale: boolean
  area_m2: number
  notes: string[]
}

export interface CrowdLive {
  zones: ZoneStatus[]
  /** Zone codes with no recent reading. Render as unknown, never as safe. */
  unknown_zones: string[]
  source: string
  generated_at: string
}

// ---------------------------------------------------------------------------
// alerts
// ---------------------------------------------------------------------------
export interface Alert {
  id: string
  type: string
  severity: AlertSeverity
  status: AlertStatus
  zone_id: string | null
  zone_code: string | null
  zone_name_mr: string | null
  trigger_metric: string
  trigger_value: number
  threshold_value: number | null
  confidence: number
  observed_at: string
  recommended_action: string | null
  recommended_action_mr: string | null
  /** Which numbered rule produced the action — the answer to "why is it telling me this". */
  rule_id: string | null
  escalation_level: number
  acknowledged_by: string | null
  acknowledged_at: string | null
  escalated_at: string | null
  resolved_at: string | null
  created_at: string
  seconds_open: number
}

export interface AlertRule {
  id: string
  alert_type: string
  severity: string
  metric: string
  threshold: number
  action: string
  action_mr: string
}

// ---------------------------------------------------------------------------
// cameras
// ---------------------------------------------------------------------------
export interface Camera {
  id: string
  zone_id: string
  zone_code: string | null
  name: string
  status: CameraStatus
  is_calibrated: boolean
  calibrated_at: string | null
  last_heartbeat_at: string | null
  seconds_since_heartbeat: number | null
  is_tripwire_enabled: boolean
  has_stream: boolean
}

// ---------------------------------------------------------------------------
// command centre
// ---------------------------------------------------------------------------
export type KpiState = 'ok' | 'watch' | 'breach' | 'unknown'
export type KpiUnit = 'persons' | 'minutes' | 'per_hour' | 'count' | 'ratio'

export interface Kpi {
  key: string
  label: string
  label_mr: string
  /** `null` means we are not measuring this. Render a dash and the note — never a zero. */
  value: number | null
  unit: KpiUnit
  target: number | null
  as_of: string | null
  age_seconds: number | null
  is_stale: boolean
  source: string
  confidence: number
  state: KpiState
  detail: Record<string, unknown>
  note: string | null
  note_mr: string | null
}

export interface KpiStrip {
  kpis: Kpi[]
  generated_at: string
  stale_count: number
  unknown_count: number
}

export type ChangeKind =
  | 'zone_level'
  | 'alert_raised'
  | 'alert_acknowledged'
  | 'alert_escalated'
  | 'alert_resolved'
  | 'camera_status'

export interface ChangeItem {
  at: string
  kind: ChangeKind
  severity: AlertSeverity
  summary: string
  summary_mr: string
  zone_code: string | null
  ref_type: string | null
  ref_id: string | null
  from_level: DensityLevel | null
  to_level: DensityLevel | null
}

export interface ChangeDigest {
  since: string
  until: string
  items: ChangeItem[]
  /** True when the window held more than `limit` changes — surface it. */
  truncated: boolean
  generated_at: string
}

export interface ReplayZoneState {
  zone_id: string
  zone_code: string
  density: number
  level: DensityLevel
  person_count: number
  stagnation_index: number
  counterflow_ratio: number
  confidence: number
  sample_count: number
}

export interface ReplayFrame {
  at: string
  zones: ReplayZoneState[]
  /** Zones with no reading this minute. Grey them; do not hold the last colour. */
  unknown_zones: string[]
  open_alerts: number
  critical_alerts: number
}

export interface ReplayWindow {
  since: string
  until: string
  step_seconds: number
  frames: ReplayFrame[]
  /** Stable legend — the set must not change frame to frame. */
  zone_codes: string[]
  generated_at: string
  note: string
  note_mr: string
}

// ---------------------------------------------------------------------------
// incidents (Phase 5, Section 4/M4)
// ---------------------------------------------------------------------------
export type IncidentType =
  | 'medical'
  | 'missing_person'
  | 'crowd_crush_risk'
  | 'fire'
  | 'structural'
  | 'lost_item'
  | 'facility_failure'
  | 'security'
  | 'other'

export type IncidentSeverity = 'critical' | 'high' | 'normal' | 'low'

export type IncidentStatus =
  | 'reported'
  | 'triaged'
  | 'dispatched'
  | 'on_scene'
  | 'resolved'
  | 'closed'

export interface IncidentEvent {
  id: string
  action: string
  note: string | null
  actor_id: string | null
  meta: Record<string, unknown>
  created_at: string
}

export interface Incident {
  id: string
  reference: string
  type: IncidentType
  severity: IncidentSeverity
  status: IncidentStatus
  source: string
  zone_id: string | null
  zone_code: string | null
  zone_name_mr: string | null
  /** [lon, lat]; null when the report carried only a zone. */
  location: [number, number] | null
  description: string | null
  has_audio_note: boolean
  sla_due_at: string
  sla_breached: boolean
  /** Goes negative once the clock has run out — render "2m over", not "0". */
  seconds_to_sla: number
  first_response_at: string | null
  assigned_responder_id: string | null
  assigned_call_sign: string | null
  /** Set when the report was queued offline; a 20-minute-old SOS is not new. */
  client_reported_at: string | null
  delayed_by_seconds: number | null
  alert_id: string | null
  resolved_at: string | null
  closed_at: string | null
  outcome_note: string | null
  created_at: string
  seconds_open: number
  timeline: IncidentEvent[]
}

export interface Suggestion {
  responder_id: string
  call_sign: string
  unit_type: string
  /** Straight-line metres; null when either end has no known position. */
  distance_m: number | null
  /** Walking time through a crowd. A floor, not a forecast. */
  eta_seconds: number | null
  type_rank: number
  caveats: string[]
}

export interface DispatchOptions {
  incident_id: string
  suggestions: Suggestion[]
  /** So an empty suggestion list is never read as "no units exist". */
  available_units: number
  note: string
  note_mr: string
  generated_at: string
}

export interface Responder {
  id: string
  call_sign: string
  unit_type: string
  status: string
  location: [number, number] | null
  last_ping_at: string | null
  seconds_since_ping: number | null
  assigned_incident_id: string | null
  assigned_incident_reference: string | null
}

export interface MissingPerson {
  id: string
  incident_id: string | null
  incident_reference: string | null
  name: string
  age: number | null
  description: string | null
  has_photo: boolean
  last_seen_zone_id: string | null
  last_seen_zone_code: string | null
  last_seen_at: string | null
  language: string
  status: string
  reported_at: string
  resolved_at: string | null
  purge_after: string | null
  open_for_seconds: number
}

// ---------------------------------------------------------------------------
// breach ledger (Phase 6, Section 4/M5)
// ---------------------------------------------------------------------------
export type ReviewStatus = 'pending' | 'verified' | 'false_positive' | 'authorised'

export interface ClipView {
  actor_id: string
  purpose: string
  ip: string | null
  accessed_at: string
}

export interface Breach {
  id: string
  /** Position in the hash chain. Gaps are themselves evidence. */
  sequence: number
  tripwire_id: string
  tripwire_name: string | null
  camera_id: string
  gate_id: string | null
  gate_code: string | null
  gate_name_mr: string | null
  occurred_at: string
  direction: string
  crossing_count: number
  confidence: number
  /** Whether a clip exists — never where it is. Fetching it is a separate act. */
  has_clip: boolean
  clip_sha256: string | null
  /** "We checked and found no pass" vs "nobody checked" — different claims. */
  pass_scan_checked: boolean
  review_status: ReviewStatus
  reviewed_by: string | null
  review_reason: string | null
  reviewed_at: string | null
  redacted_at: string | null
  redaction_reason: string | null
  chain_hash: string
  prev_hash: string
  purge_after: string
  created_at: string
  clip_views: ClipView[]
}

export interface ChainBreak {
  sequence: number
  breach_id: string | null
  problem: string
  expected: string | null
  found: string | null
}

export interface ChainReport {
  events_checked: number
  intact: boolean
  breaks: ChainBreak[]
  first_sequence: number | null
  last_sequence: number | null
  /** Worth writing down outside this database — see the server's note. */
  head_hash: string | null
  verified_at: string
  note: string
  note_mr: string
}

export interface ClipResponse {
  breach_id: string
  sequence: number
  clip_uri: string
  clip_sha256: string | null
  notice: string
  notice_mr: string
  logged_at: string
}

export interface ConsoleConfig {
  alert_escalate_seconds: number
  alert_page_seconds: number
  stale_reading_seconds: number
  crowd_window_seconds: number
  crowd_source: CrowdSource
  density_thresholds: Record<string, number>
  live_alert_counts: Record<string, number>
  server_time: string
}

// ---------------------------------------------------------------------------
// websocket
// ---------------------------------------------------------------------------
export type ServerEvent =
  | { type: 'hello'; at: string; heartbeat_seconds: number; data: { zones: WsZone[] } }
  | { type: 'heartbeat'; at: string }
  | { type: 'pong'; at: string }
  | { type: 'density.updated'; at: string; trace_id: string | null; data: WsZone }
  | { type: 'alert.raised'; at: string; trace_id: string | null; data: WsAlertRef }
  | { type: 'alert.updated'; at: string; trace_id: string | null; data: WsAlertRef }
  | { type: 'camera.status_changed'; at: string; trace_id: string | null; data: WsCameraRef }
  | { type: 'incident.raised'; at: string; trace_id: string | null; data: WsIncidentRef }
  | { type: 'incident.updated'; at: string; trace_id: string | null; data: WsIncidentRef }
  | { type: 'incident.sla_breached'; at: string; trace_id: string | null; data: WsIncidentRef }

/** The socket's zone payload — `ZoneSnapshot.to_json()` on the server. */
export interface WsZone {
  zone_id: string
  zone_code: string
  zone_name: string
  zone_name_mr: string
  person_count: number
  density: number
  level: DensityLevel
  flow_dx: number
  flow_dy: number
  stagnation_index: number
  counterflow_ratio: number
  confidence: number
  source: string
  camera_count: number
  observed_at: string
  area_m2: number
  capacity_persons: number
  notes: string[]
}

export interface WsAlertRef {
  alert_id: string
  status?: string
  severity?: string
  zone_id?: string
  acknowledged_by?: string
  resolved_by?: string
}

export interface WsCameraRef {
  camera_id: string
  zone_id?: string
  status?: CameraStatus
  name?: string
}

/**
 * The socket's incident payload.
 *
 * Note what is absent: the reporter. The command centre needs to know an
 * incident exists, where, how bad and how long it has had — not who pressed
 * the button. The server refuses to put it on the wire; this type refuses to
 * have somewhere to put it.
 */
export interface WsIncidentRef {
  incident_id: string
  reference: string
  type: IncidentType
  severity: IncidentSeverity
  status: IncidentStatus
  zone_id: string | null
  zone_code: string | null
  zone_name_mr: string | null
  sla_due_at: string
  sla_breached: boolean
  created_at: string
  overdue_seconds?: number
  missing_person_id?: string
}
