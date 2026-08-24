/**
 * The formatters' one rule: `null` renders as a dash, never as zero.
 *
 * This is the client half of the server's "a number we are not measuring is
 * None, never 0". A formatter that quietly does `?? 0` would undo every bit of
 * care taken in `command_service.py`, and it would do it in a one-line diff
 * that looks like a null-safety fix.
 */

import { describe, expect, it } from 'vitest'

import { asOfTitle, formatAge, formatCount, formatDensity, formatDuration, formatKpi, NO_VALUE } from './format'

describe('null never becomes zero', () => {
  it('holds for every formatter', () => {
    expect(formatCount(null)).toBe(NO_VALUE)
    expect(formatDensity(null)).toBe(NO_VALUE)
    expect(formatDuration(null)).toBe(NO_VALUE)
    expect(formatAge(null)).toBe(NO_VALUE)
    expect(formatKpi(null, 'persons')).toBe(NO_VALUE)
    expect(formatKpi(null, 'minutes')).toBe(NO_VALUE)
    expect(formatKpi(null, 'count')).toBe(NO_VALUE)
    expect(formatKpi(null, 'per_hour')).toBe(NO_VALUE)
    expect(formatKpi(null, 'ratio')).toBe(NO_VALUE)
  })

  it('still renders a real zero as zero', () => {
    // The other half of the rule. A zone reporting nobody is a measurement.
    expect(formatCount(0)).toBe('0')
    expect(formatDensity(0)).toBe('0')
    expect(formatKpi(0, 'persons')).toBe('0')
    expect(formatDuration(0)).toBe('0m')
  })
})

describe('formatDuration', () => {
  it('reads the way an operator says it out loud', () => {
    expect(formatDuration(45)).toBe('45m')
    expect(formatDuration(60)).toBe('1h')
    expect(formatDuration(260)).toBe('4h 20m')
    expect(formatDuration(1440)).toBe('24h')
  })

  it('never renders a negative wait', () => {
    expect(formatDuration(-5)).toBe('0m')
  })
})

describe('formatAge', () => {
  it('keeps second precision across the whole staleness window', () => {
    // The stale threshold is 90s. 61s and 89s must not look the same — one is
    // comfortable and the other is about to grey out.
    expect(formatAge(9)).toBe('9s ago')
    expect(formatAge(61)).toBe('61s ago')
    expect(formatAge(89)).toBe('89s ago')
    expect(formatAge(91)).toBe('91s ago')
    expect(formatAge(120)).toBe('2m ago')
    expect(formatAge(3_900)).toBe('1h 5m ago')
  })
})

describe('asOfTitle', () => {
  it('says "not measured" rather than inventing a timestamp', () => {
    const title = asOfTitle(null, 'unavailable', null)
    expect(title).toContain('Not measured')
    expect(title).toContain('unavailable')
  })

  it('carries the source, because provenance is the point', () => {
    const title = asOfTitle('2026-08-24T09:15:00+00:00', 'sim', 12)
    expect(title).toContain('sim')
    expect(title).toContain('12s ago')
  })
})

describe('formatKpi', () => {
  it('formats minutes as a duration, not as a bare number', () => {
    expect(formatKpi(260, 'minutes')).toBe('4h 20m')
  })

  it('formats a ratio as a percentage', () => {
    expect(formatKpi(0.85, 'ratio')).toBe('85%')
  })
})
