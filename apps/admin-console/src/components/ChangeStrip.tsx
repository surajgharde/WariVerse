/**
 * "What changed in the last 15 minutes" (Section 4/M3, last rule).
 *
 * For the operator coming back from a walkabout. They need catch-up, not a full
 * re-read, and the difference between those two is what this strip is.
 *
 * `truncated` is surfaced rather than swallowed. A digest that quietly drops
 * half a surge is worse than one that admits it ran out of room — the operator
 * who sees "list truncated" goes and looks at the alert history; the one who
 * sees a tidy list of eight lines assumes that was all of it.
 */

import { useI18n } from '@/i18n'
import { formatClock } from '@/lib/format'
import { useLive } from '@/state/live'
import type { ChangeItem } from '@/api/types'

const KIND_MARK: Record<ChangeItem['kind'], string> = {
  zone_level: '◆',
  alert_raised: '▲',
  alert_escalated: '▲▲',
  alert_acknowledged: '✓',
  alert_resolved: '✓✓',
  camera_status: '◉',
}

export function ChangeStrip() {
  const { digest } = useLive()
  const { t, s } = useI18n()

  if (!digest) return null

  return (
    <section className="changes" aria-label={t('changes.title')}>
      <h2 className="changes__title">{t('changes.title')}</h2>

      <div className="changes__scroll">
        {digest.items.length === 0 ? (
          <span className="changes__empty">{t('changes.empty')}</span>
        ) : (
          digest.items.map((item, index) => (
            <span
              key={`${item.ref_id ?? item.zone_code ?? 'x'}-${item.at}-${item.kind}-${index}`}
              className={`change change--${item.severity}`}
              title={`${formatClock(item.at, true)} IST`}
            >
              <span className="change__mark" aria-hidden="true">
                {KIND_MARK[item.kind]}
              </span>
              <span className="change__time mono">{formatClock(item.at)}</span>
              <span className="change__text">{s(item.summary, item.summary_mr)}</span>
            </span>
          ))
        )}

        {digest.truncated && <span className="change change--truncated">{t('changes.truncated')}</span>}
      </div>
    </section>
  )
}
