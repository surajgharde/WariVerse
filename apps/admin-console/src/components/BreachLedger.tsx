/**
 * The tamper-evident ledger view (Section 4/M5, Phase 6).
 *
 * Three things this screen does that a plain review queue would not:
 *
 * **The chain banner is above the records, not beside them.** If the ledger does
 * not verify, nothing below it is worth acting on — so the banner is the first
 * thing read and the review buttons are disabled underneath it. An operator who
 * marks a record "verified" on a chain that is broken has certified something
 * about a record that may not be the one they were shown.
 *
 * **A record is a detection until a human says otherwise.** `pending` is
 * rendered as a question, not as a finding. Nothing on this screen presents an
 * AI output as a conclusion.
 *
 * **The clip is behind a password and a stated purpose.** No thumbnail, no
 * autoplay, no preloaded URL. The URI never reaches this component until
 * somebody has re-authenticated and said why they are looking.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { breaches as breachApi } from '@/api/endpoints'
import type { Breach, ChainReport, ReviewStatus } from '@/api/types'
import { useI18n } from '@/i18n'
import { formatClock } from '@/lib/format'

export function BreachLedger() {
  const { t, s } = useI18n()
  const [records, setRecords] = useState<Breach[]>([])
  const [chain, setChain] = useState<ChainReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [page, report] = await Promise.all([breachApi.list(), breachApi.verify()])
      setRecords(page.items)
      setChain(report)
      setError(null)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not read the ledger.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (loading) return <p className="empty">{t('common.loading')}</p>
  if (error) return <p className="empty">{error}</p>

  const broken = chain !== null && !chain.intact

  return (
    <section className="ledger" aria-label={t('breach.title')}>
      <header className="ledger__head">
        <h2>{t('breach.title')}</h2>
        <span className="mono ledger__count">{records.length}</span>
      </header>

      {chain && <ChainBanner report={chain} onRecheck={load} />}

      {records.length === 0 ? (
        <p className="empty">{t('breach.empty')}</p>
      ) : (
        <div className="ledger__list">
          {records.map((record) => (
            <BreachCard
              key={record.id}
              record={record}
              chainBroken={broken}
              onReviewed={(updated) =>
                setRecords((current) => current.map((r) => (r.id === updated.id ? updated : r)))
              }
            />
          ))}
        </div>
      )}

      {chain && <p className="ledger__note">{s(chain.note, chain.note_mr)}</p>}
    </section>
  )
}

function ChainBanner({ report, onRecheck }: { report: ChainReport; onRecheck: () => void }) {
  const { t } = useI18n()

  return (
    <div className={`chain ${report.intact ? 'chain--intact' : 'chain--broken'}`} role="status">
      <div className="chain__state">
        <strong>{report.intact ? t('breach.chainIntact') : t('breach.chainBroken')}</strong>
        <span className="mono chain__meta">
          {report.events_checked} {t('breach.recordsChecked')}
          {report.head_hash && (
            <>
              {' · '}
              {t('breach.head')} <span title={report.head_hash}>{report.head_hash.slice(0, 12)}…</span>
            </>
          )}
          {' · '}
          {formatClock(report.verified_at, true)} IST
        </span>
      </div>

      {/* Every break, not just the first. "Broken at 412" leaves an operator
          unable to tell one bad row from the point where everything after it
          was rewritten. */}
      {!report.intact && (
        <ul className="chain__breaks">
          {report.breaks.map((b, i) => (
            <li key={`${b.sequence}-${i}`}>
              <span className="mono">#{b.sequence}</span> {b.problem}
            </li>
          ))}
        </ul>
      )}

      <button type="button" className="btn" onClick={onRecheck}>
        {t('breach.recheck')}
      </button>
    </div>
  )
}

function BreachCard({
  record,
  chainBroken,
  onReviewed,
}: {
  record: Breach
  chainBroken: boolean
  onReviewed: (updated: Breach) => void
}) {
  const { t } = useI18n()
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [clipOpen, setClipOpen] = useState(false)

  const pending = record.review_status === 'pending'
  // `authorised` and `false_positive` both need a written reason. The server
  // enforces it; asking here keeps the requirement visible as a field rather
  // than arriving as an error.
  const needsReason = (status: ReviewStatus) => status === 'authorised' || status === 'false_positive'

  const decide = async (status: ReviewStatus) => {
    if (needsReason(status) && reason.trim().length < 3) {
      setError(t('breach.reasonNeeded'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      onReviewed(await breachApi.review(record.id, status, reason || undefined))
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not record that review.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className={`breach breach--${record.review_status} ${record.redacted_at ? 'breach--redacted' : ''}`}>
      <header className="breach__head">
        <span className="breach__seq mono">#{record.sequence}</span>
        {record.gate_code && <span className="breach__gate mono">{record.gate_code}</span>}
        <span className="breach__when mono">{formatClock(record.occurred_at, true)}</span>
        <span className={`badge badge--review-${record.review_status}`}>
          {t(`breach.status.${record.review_status}` as 'breach.status.pending')}
        </span>
      </header>

      {/* The claim, worded exactly as the module is allowed to word it. */}
      <p className="breach__claim">{t('breach.claim')}</p>

      <p className="breach__meta">
        {t('breach.direction')} <span className="mono">{record.direction}</span>
        {' · '}
        {t('alerts.confidence')} <span className="mono">{Math.round(record.confidence * 100)}%</span>
        {' · '}
        {record.pass_scan_checked ? t('breach.passChecked') : t('breach.passNotChecked')}
      </p>

      <p className="breach__hash mono" title={`${record.prev_hash} → ${record.chain_hash}`}>
        {record.prev_hash.slice(0, 8)}… → {record.chain_hash.slice(0, 8)}…
      </p>

      {record.redacted_at && (
        <p className="breach__redacted">
          {t('breach.redacted')} {formatClock(record.redacted_at, true)} — {record.redaction_reason}
        </p>
      )}

      {record.reviewed_at && !pending && (
        <p className="breach__reviewed">
          {t('breach.reviewedAt')} {formatClock(record.reviewed_at, true)}
          {record.review_reason && <> — {record.review_reason}</>}
        </p>
      )}

      {error && <p className="breach__error">{error}</p>}

      {pending && (
        <>
          <input
            className="breach__reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t('breach.reasonPlaceholder')}
            aria-label={t('breach.reasonPlaceholder')}
            disabled={chainBroken}
          />
          <footer className="breach__actions">
            {(['verified', 'false_positive', 'authorised'] as const).map((status) => (
              <button
                key={status}
                type="button"
                className={`btn ${status === 'verified' ? 'btn--primary' : ''}`}
                onClick={() => void decide(status)}
                disabled={busy || chainBroken}
                title={chainBroken ? t('breach.chainBlocks') : undefined}
              >
                {t(`breach.mark.${status}` as 'breach.mark.verified')}
              </button>
            ))}
          </footer>
        </>
      )}

      {record.has_clip && (
        <>
          <button type="button" className="btn btn--ghost breach__clip-btn" onClick={() => setClipOpen(true)}>
            {t('breach.viewClip')}
          </button>
          {clipOpen && <ClipDialog record={record} onClose={() => setClipOpen(false)} />}
        </>
      )}

      {record.clip_views.length > 0 && (
        <p className="breach__views">
          {t('breach.viewedBy')} <span className="mono">{record.clip_views.length}</span>
        </p>
      )}
    </article>
  )
}

/**
 * Re-authentication and a stated purpose before any evidence moves.
 *
 * The URI arrives in the response to this form and is held in component state
 * only — never put in the address bar, never stored. Closing the dialog
 * discards it.
 */
function ClipDialog({ record, onClose }: { record: Breach; onClose: () => void }) {
  const { t, s } = useI18n()
  const [password, setPassword] = useState('')
  const [purpose, setPurpose] = useState('')
  const [result, setResult] = useState<{ uri: string; notice: string; noticeMr: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const clip = await breachApi.clip(record.id, password, purpose)
      setResult({ uri: clip.clip_uri, notice: clip.notice, noticeMr: clip.notice_mr })
      setPassword('')
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not open that clip.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dialog__backdrop" role="presentation" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={t('breach.viewClip')}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="dialog__head">
          <h2>{t('breach.viewClip')}</h2>
          <span className="mono dialog__ref">#{record.sequence}</span>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            {t('common.close')}
          </button>
        </header>

        {result ? (
          <>
            <p className="breach__notice">{s(result.notice, result.noticeMr)}</p>
            <p className="mono breach__uri">{result.uri}</p>
          </>
        ) : (
          <>
            <p className="breach__notice">{t('breach.clipPrompt')}</p>

            <label className="field">
              <span>{t('auth.password')}</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                autoFocus
              />
            </label>

            <label className="field">
              <span>{t('breach.purpose')}</span>
              <input value={purpose} onChange={(e) => setPurpose(e.target.value)} />
            </label>

            {error && <p className="dialog__error">{error}</p>}

            <footer className="dialog__actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => void submit()}
                disabled={busy || !password || purpose.trim().length < 3}
              >
                {busy ? t('auth.working') : t('breach.openClip')}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  )
}
