/**
 * Number and time formatting for a control room.
 *
 * The rule that shapes this file: **`null` renders as an em dash, never as
 * zero.** Every formatter here takes `number | null` and every one of them
 * returns `—` for null. That is not defensive coding, it is the product
 * requirement — an operator must be able to tell "nothing is happening" from
 * "we cannot see".
 */

import type { KpiUnit } from '@/api/types'

/** The dash we render for an unmeasured value. Figure dash, not hyphen — it
 * sits on the digit baseline and lines up in a tabular column. */
export const NO_VALUE = '—'

const integer = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })
const oneDp = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 })

export function formatCount(value: number | null): string {
  return value === null ? NO_VALUE : integer.format(value)
}

export function formatDensity(value: number | null): string {
  return value === null ? NO_VALUE : oneDp.format(value)
}

/** Minutes as an operator says them out loud: "4h 20m", not "260". */
export function formatDuration(minutes: number | null): string {
  if (minutes === null) return NO_VALUE
  const total = Math.max(0, Math.round(minutes))
  if (total < 60) return `${total}m`
  const hours = Math.floor(total / 60)
  const rest = total % 60
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`
}

export function formatKpi(value: number | null, unit: KpiUnit): string {
  if (value === null) return NO_VALUE
  switch (unit) {
    case 'minutes':
      return formatDuration(value)
    case 'persons':
    case 'per_hour':
    case 'count':
      return formatCount(value)
    case 'ratio':
      return `${oneDp.format(value * 100)}%`
  }
}

/**
 * Age in seconds as a glanceable string.
 *
 * Seconds are kept up to two minutes rather than the usual one. The staleness
 * threshold is ninety seconds, so an 89-second-old reading is a number an
 * operator is actively deciding about — rendering it as "1m ago" rounds away
 * the exact resolution that decision needs, and makes 61s and 89s look
 * identical when only one of them is about to grey out.
 */
export function formatAge(seconds: number | null): string {
  if (seconds === null) return NO_VALUE
  const s = Math.max(0, Math.round(seconds))
  if (s < 120) return `${s}s ago`
  const minutes = Math.floor(s / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m ago`
}

/** Wall-clock time, 24-hour, in the temple's timezone rather than the browser's.
 *
 * A console operated in Pandharpur and reviewed from anywhere else must show
 * the same clock in both places, or two people reading the same incident log
 * will disagree about when it happened.
 */
const clock = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'Asia/Kolkata',
})

const clockWithSeconds = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZone: 'Asia/Kolkata',
})

export function formatClock(iso: string | null, withSeconds = false): string {
  if (!iso) return NO_VALUE
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return NO_VALUE
  return (withSeconds ? clockWithSeconds : clock).format(date)
}

/** The full "as of" string shown on hover — Section 4/M3 requires one on every number. */
export function asOfTitle(iso: string | null, source: string, ageSeconds: number | null): string {
  if (!iso) return `Not measured · source: ${source}`
  return `As of ${formatClock(iso, true)} IST · ${formatAge(ageSeconds)} · source: ${source}`
}

export function secondsSince(iso: string): number {
  return Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
}
