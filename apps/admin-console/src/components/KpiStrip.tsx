/**
 * The six numbers across the top (Section 4/M3).
 *
 * Every card here obeys the same three rules, and they are the reason this
 * component is more than a flexbox of numbers:
 *
 * 1. A `null` value renders as an em dash with the server's explanation, never
 *    as zero.
 * 2. Every number carries its "as of" on hover, and a stale one (>90s) greys
 *    out and gets a badge. An operator must never act on a number they think
 *    is live but isn't.
 * 3. The state colour comes from the server, not from a threshold in this
 *    file — the lines that decide "this is fine" live next to the alert rules
 *    where somebody reviews them.
 */

import { useState } from 'react'

import type { Kpi } from '@/api/types'
import { useI18n } from '@/i18n'
import { asOfTitle, formatAge, formatCount, formatKpi } from '@/lib/format'
import { useLive } from '@/state/live'

export function KpiStrip() {
  const { kpis } = useLive()
  const { t } = useI18n()

  if (!kpis) {
    return (
      <div className="kpi-strip kpi-strip--loading" aria-busy="true">
        {t('common.loading')}
      </div>
    )
  }

  return (
    <div className="kpi-strip" role="group" aria-label="Key performance indicators">
      {kpis.kpis.map((kpi) => (
        <KpiCard key={kpi.key} kpi={kpi} />
      ))}
    </div>
  )
}

function KpiCard({ kpi }: { kpi: Kpi }) {
  const { t, s, lang } = useI18n()
  const [expanded, setExpanded] = useState(false)

  const unmeasured = kpi.value === null
  const note = s(kpi.note, kpi.note_mr)
  const label = lang === 'mr' ? kpi.label_mr : kpi.label

  const classes = [
    'kpi',
    `kpi--${kpi.state}`,
    kpi.is_stale ? 'kpi--stale' : '',
    unmeasured ? 'kpi--unmeasured' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <button
      type="button"
      className={classes}
      title={asOfTitle(kpi.as_of, kpi.source, kpi.age_seconds)}
      onClick={() => setExpanded((open) => !open)}
      aria-expanded={expanded}
    >
      <span className="kpi__label">{label}</span>

      <span className="kpi__value">
        <span className="kpi__number">{formatKpi(kpi.value, kpi.unit)}</span>
        {kpi.target !== null && !unmeasured && (
          <span className="kpi__target">
            {' / '}
            {formatKpi(kpi.target, kpi.unit)}
          </span>
        )}
      </span>

      <span className="kpi__foot">
        {kpi.is_stale && <span className="badge badge--stale">{t('kpi.stale')}</span>}
        {unmeasured ? (
          <span className="kpi__unknown">{t('kpi.unknown')}</span>
        ) : (
          <span className="kpi__age">{formatAge(kpi.age_seconds)}</span>
        )}
      </span>

      {/* The note is the whole point of an unmeasured card — it says *why*, and
          "why" is what tells an operator whether to worry. Shown without
          expanding when there is no number to look at. */}
      {note && (unmeasured || expanded) && <span className="kpi__note">{note}</span>}

      {expanded && Object.keys(kpi.detail).length > 0 && (
        <dl className="kpi__detail">
          {Object.entries(kpi.detail).map(([key, value]) => (
            <div key={key} className="kpi__detail-row">
              <dt>{key.replace(/_/g, ' ')}</dt>
              <dd>{renderDetail(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </button>
  )
}

function renderDetail(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—'
  if (typeof value === 'number') return formatCount(value)
  return String(value)
}
