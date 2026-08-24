/**
 * The camera roster.
 *
 * No video. The Core API deliberately withholds `stream_url` — an RTSP endpoint
 * with credentials in it is a way into the temple's camera network, not a
 * display field (see `cameras.py`). What an operator needs from this screen is
 * which cameras are contributing to the density figures and which are not, and
 * that is what it shows.
 *
 * Calibration state is given equal weight to online state, because an
 * uncalibrated camera is the more dangerous of the two failures: an offline
 * camera reports nothing and drops the zone's confidence, while an
 * uncalibrated one reports confidently from no measured ground plane.
 */

import type { Camera } from '@/api/types'
import { useI18n } from '@/i18n'
import { formatAge, formatClock } from '@/lib/format'
import { useLive } from '@/state/live'

export function CameraGrid() {
  const { cameras } = useLive()
  const { t } = useI18n()

  if (cameras.length === 0) {
    return <p className="empty">{t('cameras.empty')}</p>
  }

  const online = cameras.filter((c) => c.status === 'online').length
  const uncalibrated = cameras.filter((c) => !c.is_calibrated).length

  return (
    <section className="cameras" aria-label={t('cameras.title')}>
      <header className="cameras__head">
        <span className="mono cameras__tally">
          {online}/{cameras.length} {t('cameras.online')}
        </span>
        {uncalibrated > 0 && (
          <span className="cameras__warn">
            {uncalibrated} {t('cameras.uncalibrated')} — their density figures are estimates
          </span>
        )}
      </header>

      <div className="cameras__grid">
        {cameras.map((camera) => (
          <CameraTile key={camera.id} camera={camera} />
        ))}
      </div>
    </section>
  )
}

function CameraTile({ camera }: { camera: Camera }) {
  const { t } = useI18n()

  return (
    <article className={`camera camera--${camera.status}`}>
      <header className="camera__head">
        <span className="camera__name mono">{camera.name}</span>
        <span className={`badge badge--${camera.status}`}>{t(`cameras.${camera.status}` as const)}</span>
      </header>

      <p className="camera__zone">{camera.zone_code ?? '—'}</p>

      <p className="camera__meta">
        {t('cameras.lastSeen')}:{' '}
        <span className="mono" title={formatClock(camera.last_heartbeat_at, true)}>
          {camera.seconds_since_heartbeat === null
            ? 'never'
            : formatAge(camera.seconds_since_heartbeat)}
        </span>
      </p>

      {!camera.is_calibrated && (
        <p className="camera__uncalibrated" title="Without a homography the density figure is fiction.">
          {t('cameras.uncalibrated')}
        </p>
      )}

      {camera.is_tripwire_enabled && <p className="camera__tripwire">tripwire</p>}
    </article>
  )
}
