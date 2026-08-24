/**
 * Density -> colour.
 *
 * Section 10 asks for "smooth colour interpolation between zone states". That
 * is a real request and this file honours it, but with one hard constraint the
 * spec implies and does not spell out: **interpolation must never make a zone
 * read as a safer band than it is.**
 *
 * So the ramp is anchored at the published band boundaries (2.0 / 3.5 / 5.0
 * p/m², the same numbers `classify_density` uses server-side) and interpolates
 * *within* a band, not across the boundary into it. A zone at 4.9 p/m² is a
 * saffron that has almost reached sindoor; at 5.0 it is sindoor outright, with
 * no blend that would let it look like 4.5 for a frame.
 *
 * The unknown colour is not on this ramp at all. It is a flat dead grey,
 * because "we are not measuring this zone" is not a point on a safety scale.
 */

import type { DensityLevel } from '@/api/types'

export const LEVELS: DensityLevel[] = ['safe', 'moderate', 'high', 'critical']

/** The published boundaries. Kept in sync with the server via `/command/config`. */
export const DEFAULT_THRESHOLDS = { safe: 2.0, moderate: 3.5, high: 5.0 }

type Rgb = readonly [number, number, number]

/** Section 10's palette, as RGB so we can interpolate without a colour library. */
const BAND_COLOURS: Record<DensityLevel, Rgb> = {
  safe: [46, 125, 91], // --tulsi
  moderate: [224, 161, 6], // --amber
  high: [232, 98, 43], // --saffron
  critical: [196, 43, 28], // --sindoor
}

export const UNKNOWN_COLOUR = '#565b66'

export function levelColour(level: DensityLevel): string {
  return rgb(BAND_COLOURS[level])
}

function rgb([r, g, b]: Rgb): string {
  return `rgb(${r}, ${g}, ${b})`
}

function mix(from: Rgb, to: Rgb, t: number): Rgb {
  const clamped = Math.max(0, Math.min(1, t))
  return [
    Math.round(from[0] + (to[0] - from[0]) * clamped),
    Math.round(from[1] + (to[1] - from[1]) * clamped),
    Math.round(from[2] + (to[2] - from[2]) * clamped),
  ]
}

export function classify(
  density: number,
  thresholds: { safe: number; moderate: number; high: number } = DEFAULT_THRESHOLDS,
): DensityLevel {
  if (density < thresholds.safe) return 'safe'
  if (density < thresholds.moderate) return 'moderate'
  if (density < thresholds.high) return 'high'
  return 'critical'
}

/**
 * Colour for a density reading, interpolated within its band.
 *
 * The band is decided first and the blend happens inside it, so the returned
 * colour always belongs to the band `classify()` would return. A reading one
 * hair under a boundary looks like it is about to cross; it never looks like it
 * already has, and — the part that matters — it never looks like the band
 * below.
 */
export function densityColour(
  density: number,
  thresholds: { safe: number; moderate: number; high: number } = DEFAULT_THRESHOLDS,
): string {
  const level = classify(density, thresholds)

  switch (level) {
    case 'safe': {
      // Fade in from the band's own colour rather than from nothing, so an
      // empty zone and a nearly-moderate one are visibly different.
      const t = density / thresholds.safe
      return rgb(mix(BAND_COLOURS.safe, BAND_COLOURS.moderate, t * 0.35))
    }
    case 'moderate': {
      const t = (density - thresholds.safe) / (thresholds.moderate - thresholds.safe)
      return rgb(mix(BAND_COLOURS.moderate, BAND_COLOURS.high, t * 0.5))
    }
    case 'high': {
      const t = (density - thresholds.moderate) / (thresholds.high - thresholds.moderate)
      return rgb(mix(BAND_COLOURS.high, BAND_COLOURS.critical, t * 0.6))
    }
    case 'critical':
      // No ramp above critical. There is nothing worse to shade towards, and a
      // gradient here would suggest the top of the scale is negotiable.
      return rgb(BAND_COLOURS.critical)
  }
}

/**
 * Fill opacity for a zone polygon.
 *
 * Stale and unknown zones render at a low, flat opacity with the dead grey —
 * an operator's eye should read them as "no information", not as a pale
 * version of safe.
 */
export function zoneOpacity(state: { isStale: boolean; hasReading: boolean }): number {
  if (!state.hasReading || state.isStale) return 0.18
  return 0.55
}

/** Compass bearing (degrees, 0 = north) from a flow vector, or null when static. */
export function flowBearing(dx: number, dy: number): number | null {
  const speed = Math.hypot(dx, dy)
  if (speed < 0.05) return null
  return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360
}
