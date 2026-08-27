/**
 * Heritage moderation (Track 1, item 5).
 *
 * The queue of contributions waiting to go into the archive, oldest first —
 * a submission that has waited a week is the one to read next.
 *
 * Two things this screen insists on, both inherited from the server rather than
 * invented here:
 *
 * **Declining needs a reason.** The decline button does nothing until a note is
 * written. A rejection with no reason is unappealable and unlearnable: the
 * contributor cannot fix it, and the next moderator cannot tell whether the
 * same call would be made again.
 *
 * **Nothing is deleted.** There is no delete control on this screen and no
 * endpoint behind one. Someone's grandmother's version of an ovi being wrong
 * for this archive is not a reason to destroy the only copy anybody typed out.
 *
 * The Marathi body is rendered in full and unwrapped — `white-space: pre-wrap`,
 * because an abhang's line breaks are part of the text. A moderator judging
 * verse needs to see it as verse.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { heritage } from '@/api/endpoints'
import type { HeritageItem } from '@/api/types'
import { useI18n } from '@/i18n'
import { formatAge } from '@/lib/format'

export function HeritageReview() {
  const { t } = useI18n()
  const [items, setItems] = useState<HeritageItem[]>([])
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const page = await heritage.queue()
      setItems(page.items)
      setError(null)
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'Could not load the queue.')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  return (
    <section className="heritage" aria-label={t('her.title')}>
      <header className="heritage__head">
        <h2>{t('her.title')}</h2>
        <span className="mono">{items.length}</span>
      </header>

      {error && (
        <p className="empty" role="alert">
          {error}
        </p>
      )}

      {items.length === 0 ? (
        <p className="empty">{t('her.empty')}</p>
      ) : (
        <div className="heritage__list">
          {items.map((item) => (
            <ContributionCard key={item.id} item={item} onChanged={reload} />
          ))}
        </div>
      )}
    </section>
  )
}

function ContributionCard({
  item,
  onChanged,
}: {
  item: HeritageItem
  onChanged: () => Promise<void>
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [declining, setDeclining] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const decide = useCallback(
    async (publish: boolean) => {
      setBusy(true)
      setError(null)
      try {
        await heritage.review(item.id, publish, publish ? undefined : note.trim())
        await onChanged()
      } catch (exc) {
        setError(exc instanceof ApiError ? exc.message : 'That did not go through.')
      } finally {
        setBusy(false)
      }
    },
    [item.id, note, onChanged],
  )

  const age = item.created_at
    ? (Date.now() - new Date(item.created_at).getTime()) / 1000
    : null

  return (
    <article className="contribution">
      <header className="contribution__head">
        <strong>{item.title_mr}</strong>
        <span className="mono">{t(`her.kind.${item.kind}`)}</span>
      </header>

      <p className="muted">
        {[item.attribution, item.era, item.source, item.contributed_by_name]
          .filter(Boolean)
          .join(' · ')}
        {age !== null && <> · {formatAge(age)}</>}
      </p>

      {/* Verse as verse. Reflowing an abhang into a paragraph is the digital
          equivalent of retyping it wrong. */}
      <p className="contribution__body">{item.body_mr}</p>

      {error && (
        <p className="empty" role="alert">
          {error}
        </p>
      )}

      <div className="contribution__actions">
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy}
          onClick={() => void decide(true)}
        >
          {t('her.publish')}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => setDeclining((v) => !v)}>
          {t('her.decline')}
        </button>
      </div>

      {declining && (
        <div className="contribution__decline">
          <label className="field">
            <span>{t('her.whyDecline')}</span>
            <input value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          {/* Disabled until there is a reason. The server refuses too — this is
              the courtesy, not the guard. */}
          <button
            type="button"
            className="btn"
            disabled={busy || note.trim().length < 3}
            onClick={() => void decide(false)}
          >
            {t('her.confirmDecline')}
          </button>
        </div>
      )}
    </article>
  )
}
