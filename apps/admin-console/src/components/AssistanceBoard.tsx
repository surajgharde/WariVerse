/**
 * The assistance board (Track 1, item 4).
 *
 * Requests for a wheelchair, an arm to lean on, a step-free route. Ordered by
 * who has waited past their promise first — the server sorts on `sla_due_at`
 * and this renders that order without re-sorting.
 *
 * Two deliberate differences from the incident board next door:
 *
 * **No severity.** These requests are not graded against each other. A
 * wheelchair twenty minutes late is the same failure whoever asked for it, so
 * elapsed time against a single 15-minute promise is the only ranking.
 *
 * **A "nobody came" button.** Closing as `unmet` is a first-class action with a
 * mandatory note, not a thing you do by letting a row rot. The count that
 * matters after a Wari is how many asks went unanswered, and a board with no
 * way to say so produces a flawless report and a false one.
 *
 * What is not here: the pilgrim's own notes about their body. The server does
 * not send them and this screen has nowhere to put them. A volunteer is told
 * "wheelchair", which is what they act on.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { assistance } from '@/api/endpoints'
import type { AssistanceRequest } from '@/api/types'
import { useI18n } from '@/i18n'
import { formatAge } from '@/lib/format'

export function AssistanceBoard() {
  const { t } = useI18n()
  const [requests, setRequests] = useState<AssistanceRequest[]>([])
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const page = await assistance.list()
      setRequests(page.items)
      setError(null)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not load the board.')
    }
  }, [])

  useEffect(() => {
    void reload()
    // Polled rather than pushed. These are 15-minute promises, not 3-minute
    // ones — a socket subscription would be machinery for a clock that ticks
    // slowly enough to read.
    const timer = window.setInterval(() => void reload(), 20_000)
    return () => window.clearInterval(timer)
  }, [reload])

  const breached = requests.filter((r) => r.sla_breached).length

  return (
    <section className="assistance" aria-label={t('assist.title')}>
      <header className="assistance__head">
        <h2>{t('assist.title')}</h2>
        <span className="mono">{requests.length}</span>
        {breached > 0 && (
          <span className="assistance__late">
            {breached} {t('assist.late')}
          </span>
        )}
      </header>

      {error && (
        <p className="empty" role="alert">
          {error}
        </p>
      )}

      {requests.length === 0 ? (
        <p className="empty">{t('assist.empty')}</p>
      ) : (
        <div className="assistance__list">
          {requests.map((request) => (
            <RequestCard key={request.id} request={request} onChanged={reload} />
          ))}
        </div>
      )}
    </section>
  )
}

function RequestCard({
  request,
  onChanged,
}: {
  request: AssistanceRequest
  onChanged: () => Promise<void>
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [closing, setClosing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const act = useCallback(
    async (body: Parameters<typeof assistance.update>[1]) => {
      setBusy(true)
      setError(null)
      try {
        await assistance.update(request.id, body)
        await onChanged()
      } catch (exc) {
        setError(exc instanceof ApiError ? exc.message : 'That did not go through.')
      } finally {
        setBusy(false)
      }
    },
    [request.id, onChanged],
  )

  return (
    <article className={`assist ${request.sla_breached ? 'assist--late' : ''}`}>
      <header className="assist__head">
        {/* The needs are the headline: it is what the volunteer has to carry. */}
        <strong>{request.needs.map((need) => t(`assist.need.${need}`)).join(' · ')}</strong>
        <span className="mono">{request.reference}</span>
      </header>

      <p className="muted">
        {request.zone_code ?? t('assist.noZone')}
        {' · '}
        {formatAge(request.waiting_seconds)}
        {request.on_behalf_of && <> · {request.on_behalf_of}</>}
      </p>

      {request.note && <p>{request.note}</p>}

      {error && (
        <p className="empty" role="alert">
          {error}
        </p>
      )}

      <div className="assist__actions">
        {request.status === 'open' && (
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            onClick={() => void act({ claim: true })}
          >
            {t('assist.claim')}
          </button>
        )}
        {request.status === 'assigned' && (
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy}
            onClick={() => void act({ status: 'met' })}
          >
            {t('assist.met')}
          </button>
        )}
        <button type="button" className="btn" disabled={busy} onClick={() => setClosing((v) => !v)}>
          {t('assist.unmet')}
        </button>
      </div>

      {closing && (
        <div className="assist__unmet">
          {/* Mandatory, and enforced by the server too. "Nobody came" is the
              outcome most worth being able to count afterwards. */}
          <label className="field">
            <span>{t('assist.whyUnmet')}</span>
            <input value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <button
            type="button"
            className="btn"
            disabled={busy || note.trim().length < 3}
            onClick={() => void act({ status: 'unmet', outcome_note: note.trim() })}
          >
            {t('assist.confirmUnmet')}
          </button>
        </div>
      )}
    </article>
  )
}
