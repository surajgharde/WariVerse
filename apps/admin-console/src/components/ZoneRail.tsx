/**
 * The left rail: every zone, always, in a fixed order.
 *
 * Fixed order matters more than it looks. A rail sorted by density would put
 * the worst zone at the top, which sounds helpful and is not: the operator
 * learns where each zone sits in the list and reads it by position. A list
 * whose rows move under a surge is a list that has to be re-read at exactly
 * the moment there is no time to re-read it. Severity ordering belongs in the
 * alert feed, which is built for it; this rail is a roster.
 *
 * Zones with no reading are still listed, greyed, saying so. Dropping them
 * would make an unmeasured zone indistinguishable from a zone that does not
 * exist.
 */

import type { Zone, ZoneStatus } from '@/api/types'
import { useI18n } from '@/i18n'
import { densityColour, UNKNOWN_COLOUR } from '@/lib/density'
import { asOfTitle, formatAge, formatCount, formatDensity } from '@/lib/format'
import { useLive } from '@/state/live'

export function ZoneRail({
  selected,
  onSelect,
}: {
  selected: string | null
  onSelect: (zoneId: string | null) => void
}) {
  const { zones, statusFor, config } = useLive()
  const { t } = useI18n()

  return (
    <aside className="rail rail--left" aria-label={t('zones.title')}>
      <header className="rail__head">
        <h2>{t('zones.title')}</h2>
        <span className="rail__count">{zones.length}</span>
      </header>

      <div className="rail__body">
        {zones.map((zone) => (
          <ZoneRow
            key={zone.id}
            zone={zone}
            status={statusFor(zone.id)}
            thresholds={config?.density_thresholds}
            selected={selected === zone.id}
            onSelect={() => onSelect(selected === zone.id ? null : zone.id)}
          />
        ))}
      </div>
    </aside>
  )
}

function ZoneRow({
  zone,
  status,
  thresholds,
  selected,
  onSelect,
}: {
  zone: Zone
  status: ZoneStatus | null
  thresholds: Record<string, number> | undefined
  selected: boolean
  onSelect: () => void
}) {
  const { t, lang } = useI18n()

  const known = status !== null && !status.is_stale
  const bands = {
    safe: thresholds?.safe ?? 2.0,
    moderate: thresholds?.moderate ?? 3.5,
    high: thresholds?.high ?? 5.0,
  }
  const colour = known ? densityColour(status.density, bands) : UNKNOWN_COLOUR
  const name = lang === 'mr' ? zone.name_mr : zone.name

  return (
    <button
      type="button"
      className={`zone-row ${selected ? 'zone-row--selected' : ''} ${known ? '' : 'zone-row--unknown'}`}
      onClick={onSelect}
      title={
        status
          ? asOfTitle(status.observed_at, status.source, status.age_seconds)
          : 'This zone has never reported. Treat it as unknown, not as clear.'
      }
      aria-pressed={selected}
    >
      <span className="zone-row__bar" style={{ background: colour }} aria-hidden="true" />

      <span className="zone-row__main">
        <span className="zone-row__name">
          <span className="zone-row__code mono">{zone.code}</span> {name}
        </span>

        {known ? (
          <span className="zone-row__metrics mono">
            {formatDensity(status.density)} p/m² · {formatCount(status.person_count)} {t('zones.people')}
            {status.occupancy_pct !== null && <> · {formatDensity(status.occupancy_pct)}%</>}
          </span>
        ) : (
          <span className="zone-row__metrics zone-row__metrics--unknown">
            {t('zones.unknown')}
            {status?.is_stale && <> · {formatAge(status.age_seconds)}</>}
          </span>
        )}
      </span>

      {known && (
        <span className="zone-row__flow mono" title={`${t('zones.flow')} ${status.flow.speed_ms.toFixed(2)} m/s`}>
          {status.flow.direction ?? t('zones.static')}
        </span>
      )}

      {selected && status && (
        <span className="zone-row__detail">
          <Detail label={t('zones.stagnation')} value={status.stagnation_index} />
          <Detail label={t('zones.counterflow')} value={status.counterflow_ratio} />
          <Detail label={t('alerts.confidence')} value={status.confidence} />
          <span className="zone-row__cameras">
            {t('zones.cameras')}: <span className="mono">{zone.calibrated_camera_count}/{zone.camera_count}</span>{' '}
            {zone.camera_count > zone.calibrated_camera_count && (
              <em title="An uncalibrated camera contributes a density derived from no measured ground plane.">
                {t('cameras.uncalibrated')}
              </em>
            )}
          </span>
          {status.notes.map((note) => (
            <span key={note} className="zone-row__note">
              {note}
            </span>
          ))}
        </span>
      )}
    </button>
  )
}

function Detail({ label, value }: { label: string; value: number }) {
  return (
    <span className="zone-row__stat">
      {label} <span className="mono">{value.toFixed(2)}</span>
    </span>
  )
}
