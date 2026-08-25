/**
 * The incident board (Section 4/M4, Phase 5).
 *
 * Ordered by SLA urgency, not recency — the server sorts breached-first then
 * soonest-due, and this component renders that order without re-sorting. An
 * operator working down the list is working down the list of people who have
 * been waiting longest relative to how bad their situation is, which is the
 * only ordering that makes sense when both columns are moving.
 *
 * The SLA countdown ticks locally off `useLive().tick` rather than waiting for
 * a server push. The same reasoning as the alert escalation clock in
 * `AlertFeed`: a critical incident has a three-minute window, and a console
 * that only learns it has breached when the 15-second sweep tells it would show
 * a stale "0:14 left" for a quarter of that window.
 */

import { useCallback, useState } from 'react'

import { ApiError } from '@/api/client'
import type { Incident } from '@/api/types'
import { DispatchDialog } from '@/components/DispatchDialog'
import { useI18n } from '@/i18n'
import { formatAge, formatClock } from '@/lib/format'
import { useLive } from '@/state/live'

export function IncidentBoard() {
  const { incidents } = useLive()
  const { t } = useI18n()
  const [dispatching, setDispatching] = useState<Incident | null>(null)

  return (
    <section className="incidents" aria-label={t('incidents.title')}>
      <header className="incidents__head">
        <h2>{t('incidents.title')}</h2>
        <span className="mono incidents__count">{incidents.length}</span>
      </header>

      {incidents.length === 0 ? (
        <p className="empty">{t('incidents.empty')}</p>
      ) : (
        <div className="incidents__list">
          {incidents.map((incident) => (
            <IncidentCard key={incident.id} incident={incident} onDispatch={() => setDispatching(incident)} />
          ))}
        </div>
      )}

      {dispatching && (
        <DispatchDialog incident={dispatching} onClose={() => setDispatching(null)} />
      )}
    </section>
  )
}

function IncidentCard({ incident, onDispatch }: { incident: Incident; onDispatch: () => void }) {
  const { t, s } = useI18n()
  const { updateIncident, tick } = useLive()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Recomputed from the due timestamp against the wall clock, so the countdown
  // advances on the tick rather than on the next server read.
  const secondsLeft = Math.round((new Date(incident.sla_due_at).getTime() - Date.now()) / 1000)
  void tick

  const breached = incident.sla_breached || (secondsLeft < 0 && incident.first_response_at === null)
  const closable = incident.status === 'resolved'

  const act = useCallback(
    async (body: { status?: Incident['status']; outcome_note?: string }) => {
      setBusy(true)
      setError(null)
      try {
        await updateIncident(incident.id, body)
      } catch (exc) {
        setError(exc instanceof ApiError ? exc.message : 'Could not update this incident.')
      } finally {
        setBusy(false)
      }
    },
    [incident.id, updateIncident],
  )

  const classes = [
    'incident',
    `incident--${incident.severity}`,
    breached ? 'incident--breached' : '',
    incident.source === 'pilgrim_sos' ? 'incident--sos' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <article className={classes}>
      <header className="incident__head">
        <span className="incident__ref mono">{incident.reference}</span>
        <span className="incident__severity">{incident.severity}</span>
        {incident.zone_code && <span className="incident__zone mono">{incident.zone_code}</span>}
        <span className="incident__status">{incident.status.replace(/_/g, ' ')}</span>
      </header>

      <p className="incident__type">{incident.type.replace(/_/g, ' ')}</p>
      {incident.description && <p className="incident__desc">{incident.description}</p>}

      {/* The SLA, as a countdown that goes negative rather than clamping. "2m
          over" and "0s left" are different facts and the second one hides the
          first. */}
      <p className={`incident__sla ${breached ? 'incident__sla--breached' : ''}`}>
        {incident.first_response_at ? (
          <>
            {t('incidents.responded')} <span className="mono">{formatClock(incident.first_response_at)}</span>
          </>
        ) : breached ? (
          <>
            {t('incidents.slaOver')} <span className="mono">{formatAge(Math.abs(secondsLeft))}</span>
          </>
        ) : (
          <>
            {t('incidents.slaLeft')} <span className="mono">{formatCountdown(secondsLeft)}</span>
          </>
        )}
      </p>

      {/* An SOS that sat in an offline queue is not a new emergency. The delay
          is the single most important fact about a late-arriving report. */}
      {incident.delayed_by_seconds !== null && incident.delayed_by_seconds > 30 && (
        <p className="incident__delayed" title={`Pressed at ${formatClock(incident.client_reported_at, true)} IST`}>
          {t('incidents.queuedOffline')} {formatAge(incident.delayed_by_seconds)}
        </p>
      )}

      {incident.assigned_call_sign && (
        <p className="incident__unit">
          {t('incidents.unit')} <span className="mono">{incident.assigned_call_sign}</span>
        </p>
      )}

      {incident.has_audio_note && <p className="incident__audio">{t('incidents.audioNote')}</p>}

      {error && <p className="incident__error">{error}</p>}

      <footer className="incident__actions">
        {incident.status !== 'resolved' && incident.status !== 'closed' && (
          <button type="button" className="btn btn--primary" onClick={onDispatch} disabled={busy}>
            {incident.assigned_call_sign ? t('incidents.reassign') : t('alerts.dispatch')}
          </button>
        )}

        {incident.status === 'dispatched' && (
          <button type="button" className="btn" onClick={() => void act({ status: 'on_scene' })} disabled={busy}>
            {t('incidents.onScene')}
          </button>
        )}

        {incident.status !== 'resolved' && incident.status !== 'closed' && (
          <button type="button" className="btn" onClick={() => void act({ status: 'resolved' })} disabled={busy}>
            {t('incidents.resolve')}
          </button>
        )}

        {closable && <CloseIncident onClose={(note) => act({ status: 'closed', outcome_note: note })} busy={busy} />}
      </footer>

      <p className="incident__meta">
        {s(incident.source.replace(/_/g, ' '), incident.source.replace(/_/g, ' '))} ·{' '}
        {formatAge(incident.seconds_open)}
      </p>
    </article>
  )
}

/**
 * Closing needs a note saying what was done — the server rejects the transition
 * without one. Asking for it inline rather than letting the request fail keeps
 * the requirement visible as a field to fill in, not as an error to decode.
 */
function CloseIncident({ onClose, busy }: { onClose: (note: string) => void; busy: boolean }) {
  const { t } = useI18n()
  const [note, setNote] = useState('')

  return (
    <span className="incident__close">
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder={t('incidents.outcomePlaceholder')}
        aria-label={t('incidents.outcomePlaceholder')}
      />
      <button
        type="button"
        className="btn"
        onClick={() => onClose(note)}
        disabled={busy || note.trim().length < 3}
        title={note.trim().length < 3 ? 'Closing needs a note saying what was actually done.' : undefined}
      >
        {t('incidents.close')}
      </button>
    </span>
  )
}

/** mm:ss while the clock is still running. */
function formatCountdown(seconds: number): string {
  const s = Math.max(0, seconds)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
