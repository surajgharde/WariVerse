/**
 * The rule the spec calls non-negotiable.
 *
 * *"Never render stale crowd data as if it were live — that is how people walk
 * into a crush."*
 *
 * Every test here is a version of: at what age does a colour stop being an
 * honest thing to paint? The one that matters most is
 * `levelToShow` returning `null`, because `null` is what forces a caller to
 * render the unknown state instead of a comforting faded green.
 */

import { describe, expect, it } from 'vitest'

import {
  AGEING_AFTER_SECONDS,
  ageSecondsSince,
  EXPIRED_AFTER_SECONDS,
  formatAge,
  freshnessOf,
  levelToShow,
  unknownAdvice,
} from './freshness'

describe('freshnessOf', () => {
  it('treats a fresh reading as live', () => {
    expect(freshnessOf(0)).toBe('live')
    expect(freshnessOf(89)).toBe('live')
    expect(freshnessOf(AGEING_AFTER_SECONDS)).toBe('live')
  })

  it('treats a reading past the window as ageing, not dead', () => {
    // A phone in a pocket on 2G spends most of its life here. Discarding these
    // would leave the pilgrim with nothing at all, which is worse than a
    // labelled old reading.
    expect(freshnessOf(AGEING_AFTER_SECONDS + 1)).toBe('ageing')
    expect(freshnessOf(10 * 60)).toBe('ageing')
    expect(freshnessOf(EXPIRED_AFTER_SECONDS - 1)).toBe('ageing')
  })

  it('expires a reading old enough for the crowd to have changed completely', () => {
    expect(freshnessOf(EXPIRED_AFTER_SECONDS)).toBe('expired')
    expect(freshnessOf(60 * 60)).toBe('expired')
  })
})

describe('levelToShow — the crush rule', () => {
  it('shows a colour while the reading is live', () => {
    expect(levelToShow('safe', 10)).toBe('safe')
    expect(levelToShow('critical', 10)).toBe('critical')
  })

  it('still shows a colour while it is merely ageing', () => {
    // Paired with the age on screen. A ten-minute-old "critical" is still worth
    // showing — it is the last thing anybody knew, and it was bad.
    expect(levelToShow('critical', 10 * 60)).toBe('critical')
  })

  it('shows NO colour once the reading has expired', () => {
    // The assertion this whole file exists for. A faded green for a
    // half-hour-old reading is the failure the spec names by name.
    expect(levelToShow('safe', EXPIRED_AFTER_SECONDS)).toBeNull()
    expect(levelToShow('critical', 60 * 60)).toBeNull()
  })

  it('shows no colour when there is no timestamp to judge by', () => {
    // An age we cannot compute is not an age of zero.
    expect(levelToShow('safe', null)).toBeNull()
  })

  it('shows no colour when the server itself said unknown', () => {
    expect(levelToShow(null, 5)).toBeNull()
  })
})

describe('ageSecondsSince', () => {
  it('measures against the given instant', () => {
    const now = new Date('2026-07-12T14:30:00Z')
    expect(ageSecondsSince('2026-07-12T14:29:00Z', now)).toBe(60)
    expect(ageSecondsSince('2026-07-12T14:00:00Z', now)).toBe(1800)
  })

  it('never returns a negative age from a clock that runs slow', () => {
    // A cheap Android's clock drifts. A future timestamp must read as "just
    // now", not as a negative age that would sort or format absurdly.
    const now = new Date('2026-07-12T14:30:00Z')
    expect(ageSecondsSince('2026-07-12T14:31:00Z', now)).toBe(0)
  })

  it('returns null rather than NaN for something unparseable', () => {
    expect(ageSecondsSince(null)).toBeNull()
    expect(ageSecondsSince('not a date')).toBeNull()
  })
})

describe('formatAge', () => {
  it('reads the way the banner in the spec reads', () => {
    // "last updated 14 min ago"
    expect(formatAge(14 * 60, 'en')).toBe('14 min ago')
    expect(formatAge(14 * 60, 'mr')).toBe('14 मिनिटांपूर्वी')
  })

  it('defaults to Marathi', () => {
    expect(formatAge(0)).toBe('आत्ताच')
  })

  it('is deliberately coarse', () => {
    // "13 minutes 42 seconds" reads as false confidence about a number that is
    // an estimate anyway.
    expect(formatAge(59, 'en')).toBe('just now')
    expect(formatAge(90, 'en')).toBe('1 min ago')
    expect(formatAge(3600, 'en')).toBe('1h ago')
    expect(formatAge(3900, 'en')).toBe('1h 5m ago')
  })

  it('says "no reading" rather than inventing an age', () => {
    expect(formatAge(null, 'en')).toBe('no reading')
    expect(formatAge(null, 'mr')).toBe('माहिती नाही')
  })
})

describe('unknownAdvice', () => {
  it('never uses the word safe, in either language', () => {
    // The most important string in the app. An unknown area must not read like
    // a clear one — that is precisely how somebody walks into a crush.
    expect(unknownAdvice('en').toLowerCase()).not.toContain('safe')
    expect(unknownAdvice('en').toLowerCase()).not.toContain('clear to')
    expect(unknownAdvice('mr')).not.toContain('सुरक्षित')
  })

  it('tells the pilgrim what to actually do', () => {
    expect(unknownAdvice('en')).toContain('volunteers')
    expect(unknownAdvice('mr')).toContain('स्वयंसेवक')
  })
})
