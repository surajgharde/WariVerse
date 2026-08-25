/**
 * Choosing a unit to send (Section 4/M4).
 *
 * The server ranks; a human picks. That is the whole shape of this dialog, and
 * the reason a few obvious conveniences are deliberately absent:
 *
 * * **Nothing is pre-selected.** A default selection under time pressure is an
 *   auto-dispatch with an extra click, and Section 4/M4 says no auto-dispatch.
 *   The operator must choose.
 * * **Caveats are shown, not hidden behind a tooltip.** "position is 8 minutes
 *   old" is the fact that decides whether the nearest unit is really nearest.
 * * **Units outside the ranking are still reachable.** `available_units` is
 *   shown next to the list so an empty ranking reads as "nothing within 2 km",
 *   never as "no units exist" — and the operator knows to use the radio.
 */

import { useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { incidents as incidentsApi } from '@/api/endpoints'
import type { DispatchOptions, Incident, Suggestion } from '@/api/types'
import { useI18n } from '@/i18n'
import { formatDuration } from '@/lib/format'
import { useLive } from '@/state/live'

export function DispatchDialog({ incident, onClose }: { incident: Incident; onClose: () => void }) {
  const { t, s } = useI18n()
  const { dispatchUnit } = useLive()

  const [options, setOptions] = useState<DispatchOptions | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    incidentsApi
      .dispatchOptions(incident.id)
      .then((data) => {
        if (!cancelled) setOptions(data)
      })
      .catch((exc: unknown) => {
        if (!cancelled) setError(exc instanceof ApiError ? exc.message : 'Could not load units.')
      })
    return () => {
      cancelled = true
    }
  }, [incident.id])

  // Escape closes. A dialog that traps an operator mid-emergency is a bug.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const send = async () => {
    if (!selected) return
    setBusy(true)
    setError(null)
    try {
      await dispatchUnit(incident.id, selected, note || undefined)
      onClose()
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not dispatch that unit.')
      setBusy(false)
    }
  }

  return (
    <div className="dialog__backdrop" role="presentation" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={t('dispatch.title')}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="dialog__head">
          <h2>{t('dispatch.title')}</h2>
          <span className="mono dialog__ref">{incident.reference}</span>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            {t('common.close')}
          </button>
        </header>

        {error && <p className="dialog__error">{error}</p>}

        {!options ? (
          <p className="empty">{t('common.loading')}</p>
        ) : options.suggestions.length === 0 ? (
          <p className="empty">
            {/* Distinguishes "nothing near" from "nothing at all" — the first is
                a radio call, the second is a staffing problem. */}
            {options.available_units > 0
              ? t('dispatch.noneNearby').replace('{n}', String(options.available_units))
              : t('dispatch.noneAtAll')}
          </p>
        ) : (
          <ul className="dispatch__list">
            {options.suggestions.map((unit) => (
              <UnitRow
                key={unit.responder_id}
                unit={unit}
                selected={selected === unit.responder_id}
                onSelect={() => setSelected(unit.responder_id)}
              />
            ))}
          </ul>
        )}

        {options && (
          <>
            <label className="field">
              <span>{t('dispatch.note')}</span>
              <input value={note} onChange={(e) => setNote(e.target.value)} />
            </label>

            <footer className="dialog__actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => void send()}
                disabled={busy || !selected}
                title={!selected ? 'Choose a unit first — nothing is dispatched automatically.' : undefined}
              >
                {busy ? t('auth.working') : t('dispatch.send')}
              </button>
              <span className="dispatch__available mono">
                {options.available_units} {t('dispatch.available')}
              </span>
            </footer>

            {/* The server's own caveat about what these numbers are, travelling
                with the data so it cannot drift from what the endpoint does. */}
            <p className="dispatch__note">{s(options.note, options.note_mr)}</p>
          </>
        )}
      </div>
    </div>
  )
}

function UnitRow({
  unit,
  selected,
  onSelect,
}: {
  unit: Suggestion
  selected: boolean
  onSelect: () => void
}) {
  const { t } = useI18n()

  return (
    <li>
      <button
        type="button"
        className={`dispatch__unit ${selected ? 'dispatch__unit--selected' : ''}`}
        onClick={onSelect}
        aria-pressed={selected}
      >
        <span className="dispatch__callsign mono">{unit.call_sign}</span>
        <span className="dispatch__type">{unit.unit_type.replace(/_/g, ' ')}</span>

        <span className="dispatch__distance mono">
          {unit.distance_m === null ? '—' : `${Math.round(unit.distance_m)} m`}
        </span>

        <span className="dispatch__eta mono" title="Walking through a crowd at 0.7 m/s. A floor, not a forecast.">
          {unit.eta_seconds === null ? '—' : `~${formatDuration(unit.eta_seconds / 60)}`}
        </span>

        {unit.caveats.length > 0 && (
          <span className="dispatch__caveats">
            {unit.caveats.map((c) => (
              <em key={c}>{c}</em>
            ))}
          </span>
        )}

        {unit.type_rank === 0 && <span className="dispatch__best">{t('dispatch.bestFit')}</span>}
      </button>
    </li>
  )
}
