/**
 * The right rail (Section 4/M3).
 *
 * Worst first, then newest first — an operator scrolling a busy feed should hit
 * the CRITICAL from four minutes ago before the INFO from four seconds ago.
 * The server already orders it that way; this component does not re-sort, it
 * renders what it was given.
 *
 * The escalation clock is the part worth reading carefully. An unacknowledged
 * critical alert escalates visually after `alert_escalate_seconds` and pages
 * the next role after `alert_page_seconds` — and **both numbers come from the
 * server**, because they are operator-tunable. A console counting to 60 while
 * the server escalates at 90 would turn a card red before anything happened,
 * and an operator who notices that once stops believing the colour.
 *
 * The paging itself happens server-side, in the scheduler's escalation job,
 * where it can be audited. What this component does is *show* the state the
 * server has already reached. It never initiates an escalation, because an
 * escalation that exists only in a browser tab is one that vanishes when the
 * tab is closed.
 */

import { useEffect, useMemo, useState } from 'react'

import { ApiError } from '@/api/client'
import type { Alert, Incident } from '@/api/types'
import { DispatchDialog } from '@/components/DispatchDialog'
import { useI18n } from '@/i18n'
import { formatAge, formatClock, formatDensity } from '@/lib/format'
import { useLive } from '@/state/live'

export function AlertFeed() {
  const { alerts, statuses, zones, config } = useLive()
  const { t } = useI18n()

  const anyUnknown = zones.length > Object.keys(statuses).length

  return (
    <aside className="rail rail--right" aria-label={t('alerts.title')}>
      <header className="rail__head">
        <h2>{t('alerts.title')}</h2>
        <span className="rail__count">{alerts.length}</span>
      </header>

      <div className="rail__body">
        {alerts.length === 0 ? (
          <p className="empty">
            {/* "No alerts" and "no alerts, and half the temple is dark" are
                different sentences, and only one of them means all clear. */}
            {anyUnknown ? t('alerts.emptyStale') : t('alerts.empty')}
          </p>
        ) : (
          alerts.map((alert) => (
            <AlertCard
              key={alert.id}
              alert={alert}
              escalateSeconds={config?.alert_escalate_seconds ?? 60}
              pageSeconds={config?.alert_page_seconds ?? 180}
            />
          ))
        )}
      </div>
    </aside>
  )
}

function AlertCard({
  alert,
  escalateSeconds,
  pageSeconds,
}: {
  alert: Alert
  escalateSeconds: number
  pageSeconds: number
}) {
  const { t, s } = useI18n()
  const { acknowledge, escalateToIncident, tick } = useLive()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [arrived, setArrived] = useState(true)
  const [dispatching, setDispatching] = useState<Incident | null>(null)

  // Section 10: alerts pulse once on arrival, then hold. Nothing in a control
  // room animates forever — a pulse that never stops is a pulse nobody sees.
  useEffect(() => {
    const timer = window.setTimeout(() => setArrived(false), 1_200)
    return () => window.clearTimeout(timer)
  }, [])

  const openSeconds = useMemo(
    () => Math.max(0, (Date.now() - new Date(alert.created_at).getTime()) / 1000),
    // `tick` re-evaluates this once a second so the escalation state advances
    // without waiting for a server event.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [alert.created_at, tick],
  )

  const unacknowledged = alert.status === 'open'
  const critical = alert.severity === 'critical'
  const escalating = unacknowledged && critical && openSeconds >= escalateSeconds
  const paging = unacknowledged && critical && openSeconds >= pageSeconds

  /**
   * Dispatch from an alert: open an incident, then choose a unit.
   *
   * Two steps rather than one, because an alert and an incident are different
   * claims. An alert says a threshold was crossed; an incident says somebody
   * needs help and starts an SLA clock against a named severity. Turning the
   * first into the second is a judgement, and it is recorded as one — the
   * incident's description carries the alert that prompted it.
   */
  const onDispatch = async () => {
    setBusy(true)
    setError(null)
    try {
      const incident = await escalateToIncident(alert, alert.severity === 'critical' ? 'critical' : 'high')
      setDispatching(incident)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not open an incident.')
    } finally {
      setBusy(false)
    }
  }

  const onAcknowledge = async () => {
    setBusy(true)
    setError(null)
    try {
      await acknowledge(alert.id)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not acknowledge.')
    } finally {
      setBusy(false)
    }
  }

  const classes = [
    'alert',
    `alert--${alert.severity}`,
    arrived ? 'alert--arrived' : '',
    escalating ? 'alert--escalating' : '',
    paging ? 'alert--paging' : '',
    alert.status === 'acknowledged' ? 'alert--acknowledged' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <article className={classes} aria-live={critical ? 'assertive' : 'off'}>
      <header className="alert__head">
        <span className="alert__severity">{alert.severity}</span>
        {alert.zone_code && <span className="alert__zone">{alert.zone_code}</span>}
        <span className="alert__age" title={`Raised ${formatClock(alert.created_at, true)} IST`}>
          {formatAge(openSeconds)}
        </span>
      </header>

      <p className="alert__type">{alert.type.replace(/_/g, ' ')}</p>

      {/* The metric that triggered it, next to the threshold it crossed. An
          operator asked to close a gate deserves to see the arithmetic. */}
      <p className="alert__trigger">
        <span className="mono">{alert.trigger_metric}</span>{' '}
        <span className="mono alert__trigger-value">{formatDensity(alert.trigger_value)}</span>
        {alert.threshold_value !== null && (
          <>
            {' ≥ '}
            <span className="mono alert__trigger-threshold">{formatDensity(alert.threshold_value)}</span>
          </>
        )}
      </p>

      <p className="alert__confidence">
        {t('alerts.confidence')} <span className="mono">{Math.round(alert.confidence * 100)}%</span>
        {alert.rule_id && (
          <>
            {' · '}
            {t('alerts.rule')} <span className="mono">{alert.rule_id}</span>
          </>
        )}
      </p>

      {/* Section 0 rule 3: never presented as certainty, always attributed to a
          rule the operator can go and read. */}
      {(alert.recommended_action || alert.recommended_action_mr) && (
        <p className="alert__action">{s(alert.recommended_action, alert.recommended_action_mr)}</p>
      )}

      {(escalating || paging) && (
        <p className="alert__escalation">
          {paging ? t('alerts.paging') : t('alerts.escalated')}
          {alert.escalation_level > 0 && <span className="mono"> L{alert.escalation_level}</span>}
        </p>
      )}

      {alert.status === 'acknowledged' && alert.acknowledged_at && (
        <p className="alert__acknowledged">
          {t('alerts.acknowledgedBy')} · {formatClock(alert.acknowledged_at, true)}
        </p>
      )}

      {error && <p className="alert__error">{error}</p>}

      <footer className="alert__actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={onAcknowledge}
          disabled={busy || alert.status !== 'open'}
        >
          {t('alerts.acknowledge')}
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => void onDispatch()}
          disabled={busy || alert.status === 'resolved'}
          title="Opens an incident from this alert, then lets you choose a unit."
        >
          {t('alerts.dispatch')}
        </button>
      </footer>

      {dispatching && <DispatchDialog incident={dispatching} onClose={() => setDispatching(null)} />}
    </article>
  )
}
