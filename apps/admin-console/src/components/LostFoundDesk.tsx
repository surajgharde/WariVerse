/**
 * The lost-and-found desk (Track 1, item 2).
 *
 * Two columns, because the desk has two piles: reports of things gone, and
 * things sitting on the shelf. The interesting column is neither — it is the
 * suggestions between them.
 *
 * A suggestion renders its *reasons*, not its score. "Same zone, 40 minutes
 * apart, both blue" is something a volunteer can check against the two objects
 * in front of them; "87%" is something they can only defer to. The number is
 * shown small and last, and the accept button is never pre-selected at any
 * value — the server has no auto-match path and neither does this screen.
 *
 * Handover is a separate, deliberately heavier action: it takes a claimant name
 * and a written note, because an item that left the desk with nothing against
 * it is indistinguishable afterwards from an item stolen off it.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { lostFound } from '@/api/endpoints'
import type { LostFoundItem, LostFoundMatch } from '@/api/types'
import { useI18n } from '@/i18n'
import { formatAge } from '@/lib/format'

export function LostFoundDesk() {
  const { t } = useI18n()
  const [lost, setLost] = useState<LostFoundItem[]>([])
  const [found, setFound] = useState<LostFoundItem[]>([])
  const [selected, setSelected] = useState<LostFoundItem | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const [lostPage, foundPage] = await Promise.all([
        lostFound.list('lost'),
        lostFound.list('found'),
      ])
      setLost(lostPage.items)
      setFound(foundPage.items)
      setError(null)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not load the register.')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  return (
    <section className="lostfound" aria-label={t('lf.title')}>
      <header className="lostfound__head">
        <h2>{t('lf.title')}</h2>
        <span className="mono">
          {lost.length} / {found.length}
        </span>
      </header>

      {error && (
        <p className="empty" role="alert">
          {error}
        </p>
      )}

      <div className="lostfound__columns">
        <Column
          title={t('lf.lostColumn')}
          items={lost}
          selected={selected}
          onSelect={setSelected}
        />
        <Column
          title={t('lf.foundColumn')}
          items={found}
          selected={selected}
          onSelect={setSelected}
        />
      </div>

      {selected && (
        <RecordDetail
          record={selected}
          onClose={() => setSelected(null)}
          onChanged={async () => {
            await reload()
            setSelected(null)
          }}
        />
      )}
    </section>
  )
}

function Column({
  title,
  items,
  selected,
  onSelect,
}: {
  title: string
  items: LostFoundItem[]
  selected: LostFoundItem | null
  onSelect: (item: LostFoundItem) => void
}) {
  const { t } = useI18n()

  return (
    <div className="lostfound__column">
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="empty">{t('lf.empty')}</p>
      ) : (
        <ul className="lostfound__list">
          {items.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className={`lostfound__row ${selected?.id === item.id ? 'lostfound__row--on' : ''}`}
                onClick={() => onSelect(item)}
              >
                <strong>{t(`lf.cat.${item.category}`)}</strong>
                <span>{item.description}</span>
                <span className="mono lostfound__meta">
                  {item.reference}
                  {item.zone_code && <> · {item.zone_code}</>}
                  {/* Oldest-first is the server's order. Elapsed time is the
                      only thing that ranks records with no severity. */}
                  {' · '}
                  {formatAge(item.open_for_seconds)}
                  {item.suggestions.some((s) => s.decision === 'pending') && (
                    <> · {t('lf.hasSuggestions')}</>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function RecordDetail({
  record,
  onClose,
  onChanged,
}: {
  record: LostFoundItem
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [claimant, setClaimant] = useState(record.claimed_by_name ?? '')
  const [note, setNote] = useState('')

  const pending = record.suggestions.filter((s) => s.decision === 'pending')

  const decide = useCallback(
    async (match: LostFoundMatch, accept: boolean) => {
      const counterpartId = record.kind === 'lost' ? match.found_item_id : match.lost_item_id
      setBusy(true)
      setError(null)
      try {
        await lostFound.decide(record.id, counterpartId, record.kind, accept)
        await onChanged()
      } catch (exc) {
        setError(exc instanceof ApiError ? exc.message : 'That did not go through.')
      } finally {
        setBusy(false)
      }
    },
    [record, onChanged],
  )

  const handover = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      await lostFound.handover(record.id, claimant.trim(), note.trim())
      await onChanged()
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'That did not go through.')
    } finally {
      setBusy(false)
    }
  }, [record, claimant, note, onChanged])

  return (
    <div className="lostfound__detail" role="dialog" aria-label={record.reference}>
      <header>
        <h3>
          {t(`lf.cat.${record.category}`)} · <span className="mono">{record.reference}</span>
        </h3>
        <button type="button" className="btn btn--quiet" onClick={onClose}>
          {t('common.close')}
        </button>
      </header>

      <p>{record.description}</p>
      <p className="muted mono">
        {record.status}
        {record.colour && <> · {record.colour}</>}
        {record.custody_desk && <> · {record.custody_desk}</>}
      </p>

      {/* The identifying mark is shown to the desk and to nobody else — it is
          never in a list response and never in the pilgrim-facing search. */}
      {record.distinguishing_marks && (
        <p className="lostfound__mark">
          <span className="muted">{t('lf.mark')}:</span> {record.distinguishing_marks}
        </p>
      )}

      {error && (
        <p className="empty" role="alert">
          {error}
        </p>
      )}

      {pending.length > 0 && (
        <section className="lostfound__suggestions">
          <h4>{t('lf.suggestions')}</h4>
          {pending.map((match) => (
            <div key={match.id} className="lostfound__suggestion">
              <p>
                <strong>{match.counterpart?.description ?? '—'}</strong>
                {match.counterpart?.zone_code && <> · {match.counterpart.zone_code}</>}
                {match.counterpart?.custody_desk && <> · {match.counterpart.custody_desk}</>}
              </p>
              {/* Reasons first and in words; the number last and small. A
                  volunteer can check "same zone, 40 minutes apart" against the
                  objects in front of them. They cannot check a percentage. */}
              <p className="muted">
                <Reasons reasons={match.reasons} />
                <span className="mono"> · {match.score}</span>
                {match.is_strong && <> · {t('lf.strong')}</>}
              </p>
              <div className="lostfound__actions">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={busy}
                  onClick={() => void decide(match, true)}
                >
                  {t('lf.accept')}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={() => void decide(match, false)}
                >
                  {t('lf.reject')}
                </button>
              </div>
            </div>
          ))}
        </section>
      )}

      {record.kind === 'found' && record.status !== 'returned' && (
        <section className="lostfound__handover">
          <h4>{t('lf.handover')}</h4>
          <label className="field">
            <span>{t('lf.claimant')}</span>
            <input value={claimant} onChange={(e) => setClaimant(e.target.value)} />
          </label>
          <label className="field">
            <span>{t('lf.handoverNote')}</span>
            <input value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          {/* Both fields required by the server too — this is a courtesy, not
              the guard. See `POST /lost-found/{id}/handover`. */}
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy || claimant.trim().length < 1 || note.trim().length < 3}
            onClick={() => void handover()}
          >
            {t('lf.confirmHandover')}
          </button>
        </section>
      )}
    </div>
  )
}

/** The scorer's arithmetic, in words a volunteer can check. */
function Reasons({ reasons }: { reasons: Record<string, unknown> }) {
  const { t } = useI18n()
  const parts: string[] = []

  if (reasons.same_zone === true) parts.push(t('lf.reasonSameZone'))
  if (reasons.same_zone === false) parts.push(t('lf.reasonOtherZone'))
  if (typeof reasons.hours_apart === 'number') {
    parts.push(`${reasons.hours_apart}${t('lf.reasonHours')}`)
  }
  if (typeof reasons.colour === 'string') parts.push(String(reasons.colour))
  if (Array.isArray(reasons.shared_words) && reasons.shared_words.length > 0) {
    parts.push((reasons.shared_words as string[]).join(', '))
  }

  return <>{parts.join(' · ')}</>
}
