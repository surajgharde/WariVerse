/**
 * How old is this, and may I show it as a colour? (Section 4/M7)
 *
 * The rule, quoted because it is the reason this file exists rather than a
 * ternary at each call site: *"Offline UI shows a clear 'last updated 14 min
 * ago' banner. Never render stale crowd data as if it were live — that is how
 * people walk into a crush."*
 *
 * Three bands, and the middle one is the interesting one:
 *
 * * **live** — within the server's own staleness window. Show the colour.
 * * **ageing** — past that window but recent enough to be worth something.
 *   Show the colour *and* the age, prominently. This is where a cached reading
 *   spends most of its life on a bad network.
 * * **expired** — old enough that a colour would be a lie. Show no colour at
 *   all; show the age and the advice to treat the area as unknown.
 *
 * The expiry cut-off is deliberately not "the server's stale threshold". Ninety
 * seconds is the right line for a control-room screen that is refreshed every
 * ten; it is far too aggressive for a phone in a pocket on 2G, where a
 * fifteen-minute-old reading may be the only one anybody has. So the app keeps
 * showing it, with its age, until it reaches an age at which the crowd could
 * plausibly have changed completely.
 */

import type { DensityLevel } from './types'

/** Past this, a reading is shown with its age rather than as a bare colour. */
export const AGEING_AFTER_SECONDS = 90

/**
 * Past this, no colour is shown at all.
 *
 * Twenty minutes. A Wari crowd can go from moving to stalled inside that, so
 * beyond it the honest answer is "we do not know", not a faded green.
 */
export const EXPIRED_AFTER_SECONDS = 20 * 60

export type Freshness = 'live' | 'ageing' | 'expired'

export function freshnessOf(ageSeconds: number): Freshness {
  if (ageSeconds <= AGEING_AFTER_SECONDS) return 'live'
  if (ageSeconds < EXPIRED_AFTER_SECONDS) return 'ageing'
  return 'expired'
}

export function ageSecondsSince(observedAt: string | null, now: Date = new Date()): number | null {
  if (!observedAt) return null
  const then = new Date(observedAt).getTime()
  if (Number.isNaN(then)) return null
  return Math.max(0, (now.getTime() - then) / 1000)
}

/**
 * The colour to paint a zone, or `null` for "paint nothing".
 *
 * `null` is the whole point of this function. Returning a faded green for an
 * expired reading would be the exact failure the spec names — so an expired
 * reading, or one with no timestamp at all, gets no colour and the caller is
 * forced to render the unknown state instead.
 */
export function levelToShow(
  level: DensityLevel | null,
  ageSeconds: number | null,
): DensityLevel | null {
  if (level === null || ageSeconds === null) return null
  return freshnessOf(ageSeconds) === 'expired' ? null : level
}

/**
 * "14 मिनिटांपूर्वी" — the banner text.
 *
 * Marathi first because this app is Marathi-first, and because the person
 * reading it is often 65 and tired. Deliberately coarse: nobody needs
 * "13 minutes 42 seconds", and precision here reads as false confidence.
 */
export function formatAge(ageSeconds: number | null, lang: 'mr' | 'en' = 'mr'): string {
  if (ageSeconds === null) {
    return lang === 'mr' ? 'माहिती नाही' : 'no reading'
  }

  const seconds = Math.round(ageSeconds)
  if (seconds < 60) {
    return lang === 'mr' ? 'आत्ताच' : 'just now'
  }

  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    return lang === 'mr' ? `${minutes} मिनिटांपूर्वी` : `${minutes} min ago`
  }

  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (lang === 'mr') {
    return rest ? `${hours} तास ${rest} मिनिटांपूर्वी` : `${hours} तासांपूर्वी`
  }
  return rest ? `${hours}h ${rest}m ago` : `${hours}h ago`
}

/**
 * What to tell somebody about an area we cannot currently see.
 *
 * Never the word "safe", and never anything that could be read as permission
 * to walk in. The server says the same thing on `/crowd/public`; it is repeated
 * here because the offline case is exactly when the server cannot.
 */
export function unknownAdvice(lang: 'mr' | 'en' = 'mr'): string {
  return lang === 'mr'
    ? 'या भागाची सध्याची माहिती नाही. ते मोकळे आहे असे समजू नका; स्वयंसेवकांच्या सूचना पाळा.'
    : 'No current reading for this area. Do not assume it is clear — follow the volunteers.'
}
