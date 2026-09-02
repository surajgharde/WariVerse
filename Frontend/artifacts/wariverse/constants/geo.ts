/**
 * Fixed geography for the Wari.
 *
 * Shared so the map, the location store and the facility queries all agree on
 * where "here" is when the phone cannot say.
 */

/** Shri Vitthal Rukmini Mandir — the destination every pilgrim is walking to. */
export const PANDHARPUR_TEMPLE = {
  latitude: 17.6777,
  longitude: 75.3283,
  label: 'Shri Vitthal Rukmini Mandir, Pandharpur',
} as const;

/**
 * How far around the pilgrim to look for facilities.
 *
 * 10 km rather than the API's 1 km default: the palkhi routes run between
 * villages, and someone an hour's walk out still needs to know which medical
 * post is ahead of them. The backend caps requests at 50 km.
 */
export const FACILITY_RADIUS_M = 10_000;

/** Waiting longer than this for a GPS fix is worse than showing the temple. */
export const LOCATION_TIMEOUT_MS = 12_000;

/**
 * Walking pace used for every ETA, matching the backend's `walking_speed_kmph`.
 * 2.5 km/h, not the usual 5: during the Wari the crowd sets the pace, and a
 * route promising half the real time is worse than no estimate at all.
 */
export const WALKING_SPEED_KMPH = 2.5;

/**
 * Emergency and helpline numbers, shown as tappable badges.
 *
 * Kept here rather than inline so every screen dials the same numbers, and so
 * there is one place to correct them before a Wari.
 */
export const HELPLINES = [
  { label: 'Emergency', number: '112', detail: 'Police, fire, ambulance', urgent: true },
  { label: 'Ambulance', number: '108', detail: 'Free medical transport', urgent: true },
  { label: 'Police', number: '100', detail: 'Pandharpur control', urgent: false },
  { label: 'Wari Helpline', number: '1800-233-1000', detail: 'Mandir Samiti', urgent: false },
] as const;
