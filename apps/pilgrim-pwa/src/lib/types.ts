/** Wire types the pilgrim app consumes. A deliberately small subset. */

export type DensityLevel = 'safe' | 'moderate' | 'high' | 'critical'

/** `/crowd/public` — level and advice only. No head counts, ever (Section 12). */
export interface ZonePublic {
  zone_code: string
  zone_name: string
  zone_name_mr: string
  /** `null` means unknown, which is not the same as safe. */
  level: DensityLevel | null
  advice: string
  advice_mr: string
  observed_at: string | null
  age_seconds: number | null
  is_stale: boolean
}

export interface CrowdPublic {
  zones: ZonePublic[]
  generated_at: string
  notice: string
  notice_mr: string
}

export interface Facility {
  id: string
  type: string
  name: string
  name_mr: string
  /** [lon, lat] */
  location: [number, number]
  zone_id: string | null
  zone_code: string | null
  status: string
  capacity: number | null
  notes_mr: string | null
}

export interface FacilityList {
  facilities: Facility[]
  generated_at: string
  cache_seconds: number
  notice: string
  notice_mr: string
}

export interface EmergencyContact {
  label: string
  label_mr: string
  number: string
  is_primary: boolean
}

export interface RitualTiming {
  name: string
  name_mr: string
  time: string
  note_mr: string | null
}

export interface PilgrimEssentials {
  emergency_contacts: EmergencyContact[]
  ritual_timings: RitualTiming[]
  control_room_sms: string | null
  stale_reading_seconds: number
  generated_at: string
  cache_seconds: number
  offline_notice: string
  offline_notice_mr: string
}

export interface PassView {
  id: string
  reference: string
  status: string
  group_size: number
  holder_name: string
  slot_date: string
  slot_start: string
  slot_end: string
  gate_code: string | null
  issued_at: string
  scanned_at: string | null
  /** Honest wait: whichever is later, the slot opening or clearing the queue. */
  estimated_entry_at: string
  queue_ahead: number
  reslot_count: number
  was_reslotted: boolean
  allow_early_reslot: boolean
  as_of: string
}

/** Booking response — carries the QR seed, returned exactly once. */
export interface PassIssued extends PassView {
  qr_secret: string
}

export interface Zone {
  id: string
  code: string
  name: string
  name_mr: string
  zone_type: string
  geometry: { type: string; coordinates: number[][][] } | null
}
