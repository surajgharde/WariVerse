/**
 * The colour ramp's safety property.
 *
 * Section 10 asks for smooth interpolation between zone states. The constraint
 * that is *not* in the spec, and matters more than the smoothness, is that
 * interpolation must never make a zone read as a safer band than it is. These
 * tests are that constraint, written down.
 */

import { describe, expect, it } from 'vitest'

import { classify, densityColour, DEFAULT_THRESHOLDS, flowBearing, UNKNOWN_COLOUR, zoneOpacity } from './density'

function channels(colour: string): [number, number, number] {
  const match = colour.match(/rgb\((\d+), (\d+), (\d+)\)/)
  if (!match) throw new Error(`Not an rgb() string: ${colour}`)
  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

/** Rough "how red is it" measure — red up, green down. */
function heat(colour: string): number {
  const [r, g, b] = channels(colour)
  return r - g - b / 2
}

describe('classify', () => {
  it('matches the server bands exactly', () => {
    // Same numbers as `classify_density` in app/models/crowd.py. If these ever
    // drift, the map legend and the alert that fires disagree.
    expect(classify(0)).toBe('safe')
    expect(classify(1.99)).toBe('safe')
    expect(classify(2.0)).toBe('moderate')
    expect(classify(3.49)).toBe('moderate')
    expect(classify(3.5)).toBe('high')
    expect(classify(4.99)).toBe('high')
    expect(classify(5.0)).toBe('critical')
    expect(classify(12)).toBe('critical')
  })

  it('honours server-supplied thresholds', () => {
    const custom = { safe: 1.0, moderate: 2.0, high: 3.0 }
    expect(classify(1.5, custom)).toBe('moderate')
    expect(classify(1.5)).toBe('safe')
  })
})

describe('densityColour', () => {
  it('never renders a reading cooler than the one below it', () => {
    // The whole ramp, walked in small steps. Monotonic heat means no density
    // anywhere on the scale can look calmer than a lower one.
    let previous = -Infinity
    for (let d = 0; d <= 8; d += 0.05) {
      const current = heat(densityColour(d))
      expect(current).toBeGreaterThanOrEqual(previous - 1e-9)
      previous = current
    }
  })

  it('does not blend a reading down into the band below', () => {
    // 3.49 is moderate and 3.5 is high. The moderate colour must not be warmer
    // than the high one, and neither may be confused for safe.
    const nearlyHigh = heat(densityColour(3.49))
    const justHigh = heat(densityColour(3.5))
    const safe = heat(densityColour(0.5))

    expect(justHigh).toBeGreaterThan(nearlyHigh)
    expect(nearlyHigh).toBeGreaterThan(safe)
  })

  it('holds critical flat, with no ramp above it', () => {
    // There is nothing worse to shade towards, and a gradient here would
    // suggest the top of the scale is negotiable.
    expect(densityColour(5.0)).toBe(densityColour(9.9))
    expect(densityColour(5.0)).toBe(densityColour(50))
  })

  it('keeps the unknown colour off the ramp entirely', () => {
    // "We are not measuring this zone" is not a point on a safety scale.
    for (let d = 0; d <= 8; d += 0.25) {
      expect(densityColour(d)).not.toBe(UNKNOWN_COLOUR)
    }
  })
})

describe('zoneOpacity', () => {
  it('drops a stale zone to the same faintness as an unmeasured one', () => {
    // A stale reading and no reading are the same fact to an operator: we do
    // not currently know. They must not be distinguishable by weight.
    expect(zoneOpacity({ isStale: true, hasReading: true })).toBe(
      zoneOpacity({ isStale: false, hasReading: false }),
    )
  })

  it('renders a live zone more strongly than a stale one', () => {
    expect(zoneOpacity({ isStale: false, hasReading: true })).toBeGreaterThan(
      zoneOpacity({ isStale: true, hasReading: true }),
    )
  })
})

describe('flowBearing', () => {
  it('returns null for a crowd that is barely moving', () => {
    // An arrow pointing somewhere at random is a claim about which way people
    // are going. Better to draw nothing.
    expect(flowBearing(0, 0)).toBeNull()
    expect(flowBearing(0.01, 0.01)).toBeNull()
  })

  it('reads north as 0 and east as 90', () => {
    expect(flowBearing(0, 1)).toBeCloseTo(0)
    expect(flowBearing(1, 0)).toBeCloseTo(90)
    expect(flowBearing(0, -1)).toBeCloseTo(180)
    expect(flowBearing(-1, 0)).toBeCloseTo(270)
  })
})

describe('DEFAULT_THRESHOLDS', () => {
  it('are the published crowd-safety figures', () => {
    // These live in code on the server too, deliberately: nobody under pressure
    // should be able to make a critical zone look safe by editing a config row.
    expect(DEFAULT_THRESHOLDS).toEqual({ safe: 2.0, moderate: 3.5, high: 5.0 })
  })
})
