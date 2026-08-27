/**
 * The five screens (Section 4/M7). "That is all — resist adding more."
 *
 * Written for a user who is often 65, walking, tired, one-handed and on 2G.
 * That shapes every decision here: one primary action per screen, 18px base
 * type, 48px touch targets, no jargon, and never a spinner where a fact would
 * do.
 */

import { useCallback, useEffect, useState } from 'preact/hooks'

import type { Shell, Tab } from './App'
import { accessibility, auth, cached, request, tokens } from './lib/api'
import { CACHE_KEYS, passStore, queueStore } from './lib/db'
import type { StoredPass } from './lib/db'
import { applyDisplayPreferences } from './lib/display'
import { formatAge, levelToShow, unknownAdvice } from './lib/freshness'
import { buildQrPayload, envelopeFromPayload, rollingCode, secondsUntilRotation } from './lib/totp'
import type { PassIssued, PassView, ZonePublic } from './lib/types'
import { s, t } from './i18n'
import type { Lang } from './i18n'
import { qrSvg } from './lib/qr'

// ---------------------------------------------------------------------------
// sign in
// ---------------------------------------------------------------------------
/**
 * Sign in with a name.
 *
 * One field, one tap, no waiting for a message that never arrives — there is no
 * SMS gateway behind this deployment, so a one-time code was a step a pilgrim
 * could not finish. Typing the same name again comes back to the same account.
 */
export function SignIn({ lang, onSignedIn }: { lang: Lang; onSignedIn: () => void }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const send = async () => {
    const typed = name.trim()
    if (!typed) {
      setError(t('auth.nameNeeded', lang))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await auth.signIn(typed, lang)
      tokens.set(result)
      onSignedIn()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Could not reach the server.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main class="screen screen--centred">
      <h1 class="title">{t('app.name', lang)}</h1>

      <label class="field">
        <span>{t('auth.yourName', lang)}</span>
        <input
          type="text"
          value={name}
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if ((e as KeyboardEvent).key === 'Enter') void send()
          }}
          autocomplete="name"
          enterkeyhint="go"
        />
      </label>
      <p class="muted">{t('auth.nameHint', lang)}</p>

      {error && <p class="error">{error}</p>}

      <button type="button" class="btn btn--primary" onClick={() => void send()} disabled={busy}>
        {t('auth.signIn', lang)}
      </button>
    </main>
  )
}

// ---------------------------------------------------------------------------
// home
// ---------------------------------------------------------------------------
export function HomeScreen({ shell, onGoto }: { shell: Shell; onGoto: (tab: Tab) => void }) {
  const { lang } = shell
  const [pass, setPass] = useState<StoredPass | null>(null)

  useEffect(() => {
    void passStore.get().then((p) => setPass(p ?? null))
  }, [])

  const view = pass?.view as PassView | undefined

  return (
    <>
      <h1 class="title">{t('app.name', lang)}</h1>

      {/* One primary action, and it is the emergency one. */}
      <SosButton shell={shell} />

      <section class="card">
        <h2>{t('crowd.yourZone', lang)}</h2>
        <CrowdSummary shell={shell} />
      </section>

      <section class="card">
        <h2>{t('nav.pass', lang)}</h2>
        {view ? (
          <>
            <p class="big">{new Date(view.slot_start).toLocaleTimeString(lang === 'mr' ? 'mr-IN' : 'en-IN', { hour: '2-digit', minute: '2-digit' })}</p>
            <p class="muted">
              {t('pass.entry', lang)}:{' '}
              {new Date(view.estimated_entry_at).toLocaleTimeString(lang === 'mr' ? 'mr-IN' : 'en-IN', { hour: '2-digit', minute: '2-digit' })}
            </p>
            {view.was_reslotted && <p class="warn">{t('pass.reslotted', lang)}</p>}
            <button type="button" class="btn" onClick={() => onGoto('pass')}>
              {t('pass.showAtGate', lang)}
            </button>
          </>
        ) : (
          <>
            <p class="muted">{t('pass.none', lang)}</p>
            <button type="button" class="btn" onClick={() => onGoto('pass')}>
              {t('pass.book', lang)}
            </button>
          </>
        )}
      </section>
    </>
  )
}

/**
 * The SOS button.
 *
 * Three taps maximum: press, confirm, done. The confirm step exists because a
 * phone in a pocket presses things — but it is one tap, large, and the default
 * focus.
 *
 * The press enqueues to IndexedDB first and tries the network second, so it
 * works identically online and off. The pilgrim is never shown a spinner: they
 * get a reference number or an explicit "saved, will send" (Section 4/M4).
 */
function SosButton({ shell }: { shell: Shell }) {
  const { lang, essentials } = shell
  const [stage, setStage] = useState<'idle' | 'confirm' | 'done'>('idle')
  const [reference, setReference] = useState<string | null>(null)
  const [queued, setQueued] = useState(false)

  const fire = async () => {
    const position = await currentPosition()
    const action = await shell.enqueue('sos', {
      type: 'medical',
      ...(position ? { location: position } : {}),
    })
    setStage('done')

    // Best effort: if the network is there we get a real reference back
    // immediately. If not, the queued action stands and the message says so.
    try {
      const ack = await request<{ reference: string }>('/sos', {
        method: 'POST',
        body: { type: 'medical', ...(position ? { location: position } : {}) },
      })
      setReference(ack.reference)
      await queueStore.remove(action.id)
      shell.refresh()
    } catch {
      setQueued(true)
    }
  }

  if (stage === 'done') {
    const primary = essentials?.emergency_contacts.find((c) => c.is_primary)
    return (
      <section class="card card--sos-done">
        {reference ? (
          <p class="big">
            {t('sos.reference', lang)} <strong>{reference}</strong>
          </p>
        ) : (
          <p>{queued ? t('sos.queued', lang) : t('sos.sending', lang)}</p>
        )}
        {primary && (
          <>
            <p class="muted">{t('sos.callInstead', lang)}</p>
            <a class="btn btn--call" href={`tel:${primary.number}`}>
              {s(primary.label, primary.label_mr, lang)} · {primary.number}
            </a>
          </>
        )}
        <button type="button" class="btn btn--quiet" onClick={() => setStage('idle')}>
          {t('common.close', lang)}
        </button>
      </section>
    )
  }

  if (stage === 'confirm') {
    return (
      <section class="card card--sos">
        <p class="big">{t('sos.confirm', lang)}</p>
        <button type="button" class="btn btn--sos" onClick={() => void fire()} autofocus>
          {t('sos.confirmYes', lang)}
        </button>
        <button type="button" class="btn btn--quiet" onClick={() => setStage('idle')}>
          {t('sos.cancel', lang)}
        </button>
      </section>
    )
  }

  return (
    <button type="button" class="btn btn--sos btn--huge" onClick={() => setStage('confirm')}>
      {t('sos.button', lang)}
    </button>
  )
}

/** Coarse position, and only if the pilgrim has already granted it. */
function currentPosition(): Promise<[number, number] | null> {
  if (!navigator.geolocation) return Promise.resolve(null)
  return new Promise((resolve) => {
    // Short timeout: an SOS must not wait on a GPS fix. A report with no
    // location beats a location that arrives two minutes late.
    navigator.geolocation.getCurrentPosition(
      (p) => resolve([p.coords.longitude, p.coords.latitude]),
      () => resolve(null),
      { timeout: 5_000, maximumAge: 60_000, enableHighAccuracy: false },
    )
  })
}

function CrowdSummary({ shell }: { shell: Shell }) {
  const { lang, crowd, crowdAgeSeconds } = shell
  if (!crowd) return <p class="muted">{t('common.loading', lang)}</p>

  return (
    <>
      <ul class="zones">
        {crowd.zones.slice(0, 4).map((zone) => (
          <ZoneRow key={zone.zone_code} zone={zone} lang={lang} ageSeconds={crowdAgeSeconds} />
        ))}
      </ul>
      <p class="muted small">
        {t('crowd.lastUpdated', lang)}: {formatAge(crowdAgeSeconds, lang)}
      </p>
      <p class="muted small">{s(crowd.notice, crowd.notice_mr, lang)}</p>
    </>
  )
}

/**
 * One zone.
 *
 * `levelToShow` is what decides whether a colour appears at all. An expired
 * reading gets no colour and the unknown advice instead — never a faded green,
 * which is the failure Section 4/M7 names by name.
 */
function ZoneRow({ zone, lang, ageSeconds }: { zone: ZonePublic; lang: Lang; ageSeconds: number | null }) {
  const level = levelToShow(zone.level, ageSeconds)
  const label = level ? t(`crowd.level.${level}`, lang) : t('crowd.unknown', lang)

  return (
    <li class={`zone zone--${level ?? 'unknown'}`}>
      <span class="zone__name">{s(zone.zone_name, zone.zone_name_mr, lang)}</span>
      <span class="zone__level">{label}</span>
      <span class="zone__advice">{level ? s(zone.advice, zone.advice_mr, lang) : unknownAdvice(lang)}</span>
    </li>
  )
}

// ---------------------------------------------------------------------------
// pass
// ---------------------------------------------------------------------------
export function PassScreen({ shell }: { shell: Shell }) {
  const { lang } = shell
  const [pass, setPass] = useState<StoredPass | null>(null)
  const [code, setCode] = useState<string | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(secondsUntilRotation())

  useEffect(() => {
    void passStore.get().then((p) => setPass(p ?? null))
  }, [])

  // Recompute the rolling code locally every second. This is the whole offline
  // pass: the envelope is cached and the code is derived on-device, so nothing
  // here needs a network.
  useEffect(() => {
    if (!pass?.secret) return
    let cancelled = false
    const tick = async () => {
      const next = await rollingCode(pass.secret)
      if (!cancelled) {
        setCode(next)
        setSecondsLeft(secondsUntilRotation())
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 1_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [pass?.secret])

  if (!pass) return <BookPass shell={shell} onBooked={setPass} />

  const view = pass.view as PassView
  const payload = pass.envelope && code ? buildQrPayload(pass.envelope, code) : null

  return (
    <>
      <h1 class="title">{t('nav.pass', lang)}</h1>

      <section class="card card--qr">
        <p class="muted">{t('pass.showAtGate', lang)}</p>
        {payload ? (
          <div class="qr" dangerouslySetInnerHTML={{ __html: qrSvg(payload) }} aria-label={view.reference} />
        ) : (
          <p class="muted">{t('common.loading', lang)}</p>
        )}
        <p class="mono big">{code ?? '········'}</p>
        <p class="muted small">
          {secondsLeft}s {t('pass.rotates', lang)}
        </p>
        <p class="muted small">{t('pass.offlineOk', lang)}</p>
      </section>

      <section class="card">
        <dl class="detail">
          <dt>{t('pass.slot', lang)}</dt>
          <dd>
            {new Date(view.slot_start).toLocaleString(lang === 'mr' ? 'mr-IN' : 'en-IN', {
              hour: '2-digit',
              minute: '2-digit',
              day: 'numeric',
              month: 'short',
            })}
          </dd>
          <dt>{t('pass.group', lang)}</dt>
          <dd>{view.group_size}</dd>
          <dt>{t('sos.reference', lang)}</dt>
          <dd class="mono">{view.reference}</dd>
        </dl>
        {view.was_reslotted && <p class="warn">{t('pass.reslotted', lang)}</p>}
      </section>
    </>
  )
}

function BookPass({ shell, onBooked }: { shell: Shell; onBooked: (p: StoredPass) => void }) {
  const { lang } = shell
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const book = async () => {
    setBusy(true)
    setError(null)
    try {
      const today = new Date().toISOString().slice(0, 10)
      const grid = await request<{ slots: Array<{ id: string; available: number }> }>(
        `/slots?date=${today}`,
      )
      const slot = grid.slots.find((sl) => sl.available > 0)
      if (!slot) throw new Error('No slot has space today.')

      const issued = await request<PassIssued>('/passes', {
        method: 'POST',
        body: { slot_id: slot.id, group_size: 1 },
      })

      // Fetch the envelope once and keep it. It is signed and carries its own
      // expiry, so caching it is safe — unlike the rolling code, which is
      // recomputed on-device.
      let envelope: string | null = null
      try {
        const qr = await request<{ qr_payload: string }>(`/passes/${issued.id}/qr`)
        envelope = envelopeFromPayload(qr.qr_payload)
      } catch {
        /* the pass still works once a network appears to fetch the envelope */
      }

      const stored: StoredPass = {
        id: issued.id,
        reference: issued.reference,
        secret: issued.qr_secret,
        envelope,
        envelopeFetchedAt: envelope ? new Date().toISOString() : null,
        view: issued,
      }
      await passStore.put(stored)
      onBooked(stored)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Could not book a pass.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h1 class="title">{t('nav.pass', lang)}</h1>
      <section class="card">
        <p>{t('pass.none', lang)}</p>
        {error && <p class="error">{error}</p>}
        <button type="button" class="btn btn--primary" onClick={() => void book()} disabled={busy}>
          {busy ? t('common.loading', lang) : t('pass.book', lang)}
        </button>
      </section>
    </>
  )
}

// ---------------------------------------------------------------------------
// map
// ---------------------------------------------------------------------------
/**
 * Zones and facilities as inline SVG.
 *
 * **No MapLibre.** It is 217 KB gzipped on its own, which is the entire JS
 * budget and then some — and it needs tiles, which means a network. The zone
 * polygons come from our own API as GeoJSON and project to SVG in about thirty
 * lines, works offline from cache, and renders instantly on a 2016 device.
 *
 * What is lost: pan, zoom, and a street basemap. What is kept: which zones are
 * crowded and where the toilets are, which is what the screen is for.
 */
export function MapScreen({ shell }: { shell: Shell }) {
  const { lang, crowd, crowdAgeSeconds, facilities } = shell
  const [zones, setZones] = useState<Array<{ code: string; points: string }>>([])

  useEffect(() => {
    void cached<Array<{ code: string; geometry: { coordinates: number[][][] } | null }>>(
      CACHE_KEYS.zones,
      '/zones',
      { anonymous: true },
    ).then((result) => {
      if (!result) return
      const rings = result.value
        .filter((z) => z.geometry?.coordinates?.[0])
        .map((z) => ({ code: z.code, ring: z.geometry!.coordinates[0]! }))
      if (!rings.length) return

      const lons = rings.flatMap((r) => r.ring.map((p) => p[0]!))
      const lats = rings.flatMap((r) => r.ring.map((p) => p[1]!))
      const [minLon, maxLon] = [Math.min(...lons), Math.max(...lons)]
      const [minLat, maxLat] = [Math.min(...lats), Math.max(...lats)]
      const spanLon = maxLon - minLon || 1
      const spanLat = maxLat - minLat || 1

      setZones(
        rings.map(({ code, ring }) => ({
          code,
          points: ring
            .map((p) => {
              const x = ((p[0]! - minLon) / spanLon) * 100
              // SVG y grows downward; latitude grows upward.
              const y = 100 - ((p[1]! - minLat) / spanLat) * 100
              return `${x.toFixed(2)},${y.toFixed(2)}`
            })
            .join(' '),
        })),
      )
    })
  }, [])

  const levelFor = (code: string) => {
    const zone = crowd?.zones.find((z) => z.zone_code === code)
    return levelToShow(zone?.level ?? null, crowdAgeSeconds)
  }

  return (
    <>
      <h1 class="title">{t('nav.map', lang)}</h1>

      <svg class="map" viewBox="0 0 100 100" role="img" aria-label={t('nav.map', lang)}>
        {zones.map((z) => (
          <polygon key={z.code} points={z.points} class={`map__zone map__zone--${levelFor(z.code) ?? 'unknown'}`} />
        ))}
      </svg>

      <p class="muted small">
        {t('crowd.lastUpdated', lang)}: {formatAge(crowdAgeSeconds, lang)}
      </p>

      <section class="card">
        <h2>{t('map.facilities', lang)}</h2>
        <ul class="facilities">
          {(facilities?.facilities ?? []).map((f) => (
            <li key={f.id} class={f.status === 'operational' ? '' : 'facility--down'}>
              <strong>{t(`map.type.${f.type}` as 'map.type.toilet', lang)}</strong>{' '}
              {s(f.name, f.name_mr, lang)}
              {f.zone_code && <span class="muted"> · {f.zone_code}</span>}
              {f.status !== 'operational' && <span class="warn"> · {t('map.outOfService', lang)}</span>}
            </li>
          ))}
        </ul>
        {facilities && <p class="muted small">{s(facilities.notice, facilities.notice_mr, lang)}</p>}
      </section>
    </>
  )
}

// ---------------------------------------------------------------------------
// alerts
// ---------------------------------------------------------------------------
export function AlertsScreen({ shell }: { shell: Shell }) {
  const { lang, crowd, crowdAgeSeconds, essentials } = shell

  // Advisories a pilgrim may see are the per-zone ones from `/crowd/public`.
  // The operator alert feed needs `alert:view`, which a pilgrim does not have —
  // and should not: an unacknowledged CRITICAL is a control-room fact, not a
  // thing to put in front of somebody standing in the crowd it describes.
  const worrying = (crowd?.zones ?? []).filter((z) => {
    const level = levelToShow(z.level, crowdAgeSeconds)
    return level === 'high' || level === 'critical'
  })

  return (
    <>
      <h1 class="title">{t('nav.alerts', lang)}</h1>

      <section class="card">
        {worrying.length === 0 ? (
          <p class="muted">{t('alerts.none', lang)}</p>
        ) : (
          <ul class="zones">
            {worrying.map((z) => (
              <ZoneRow key={z.zone_code} zone={z} lang={lang} ageSeconds={crowdAgeSeconds} />
            ))}
          </ul>
        )}
        <p class="muted small">
          {t('crowd.lastUpdated', lang)}: {formatAge(crowdAgeSeconds, lang)}
        </p>
      </section>

      <section class="card">
        <h2>{t('alerts.timings', lang)}</h2>
        <dl class="detail">
          {(essentials?.ritual_timings ?? []).map((r) => (
            <>
              <dt key={`${r.time}-t`}>{s(r.name, r.name_mr, lang)}</dt>
              <dd key={`${r.time}-d`} class="mono">
                {r.time}
              </dd>
            </>
          ))}
        </dl>
      </section>

      <HeritageArchive shell={shell} />
    </>
  )
}

/**
 * The Wari heritage archive (Track 1, item 5).
 *
 * Sits under the ritual timings, because to a Warkari it is the same kind of
 * thing: what is sung, when, and why here. Read-only and cached hard - an
 * abhang from the 17th century does not go stale, and the pilgrim most likely
 * to sit and read one is on a bus with no signal.
 *
 * Contributing is a separate, quieter action, and it is not a "post" button.
 * What a pilgrim submits goes to a moderator, and the copy says so: an archive
 * that implies instant publication and then silently holds things for review is
 * worse than one that is honest about the wait.
 */
const HERITAGE_KINDS = ['abhang', 'ovi', 'story', 'ritual', 'place_lore'] as const

type HeritageKind = (typeof HERITAGE_KINDS)[number]

interface HeritageEntry {
  id: string
  kind: HeritageKind
  title_mr: string
  title_en: string | null
  body_mr: string
  attribution: string | null
  source: string | null
  era: string | null
  contributed_by_name: string | null
}

function HeritageArchive({ shell }: { shell: Shell }) {
  const { lang } = shell
  const [entries, setEntries] = useState<HeritageEntry[] | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [contributing, setContributing] = useState(false)

  useEffect(() => {
    void cached<{ items: HeritageEntry[] }>(CACHE_KEYS.heritage, '/heritage?limit=50', {
      anonymous: true,
    }).then((result) => setEntries(result ? result.value.items : []))
  }, [])

  return (
    <section class="card">
      <h2>{t('her.title', lang)}</h2>

      {entries === null && <p class="muted">{t('common.loading', lang)}</p>}
      {entries !== null && entries.length === 0 && <p class="muted">{t('her.empty', lang)}</p>}

      {entries?.map((entry) => (
        <div key={entry.id} class="heritage">
          <button
            type="button"
            class="btn btn--quiet heritage__title"
            onClick={() => setOpen(open === entry.id ? null : entry.id)}
          >
            {s(entry.title_en ?? entry.title_mr, entry.title_mr, lang)}
            {entry.attribution && <span class="muted"> - {entry.attribution}</span>}
          </button>

          {open === entry.id && (
            <>
              {/* Marathi always, whatever the interface language is set to. The
                  text is the artefact; showing a translation in its place would
                  be preserving the wrong thing. */}
              <p class="heritage__body">{entry.body_mr}</p>
              <p class="muted small">
                {[entry.era, entry.source, entry.contributed_by_name].filter(Boolean).join(' - ')}
              </p>
            </>
          )}
        </div>
      ))}

      {contributing ? (
        <HeritageContribute onClose={() => setContributing(false)} lang={lang} />
      ) : (
        <button type="button" class="btn" onClick={() => setContributing(true)}>
          {t('her.contribute', lang)}
        </button>
      )}
    </section>
  )
}

function HeritageContribute({ lang, onClose }: { lang: Lang; onClose: () => void }) {
  const [kind, setKind] = useState<HeritageKind>('story')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [credit, setCredit] = useState('')
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    try {
      await request('/heritage', {
        method: 'POST',
        body: {
          kind,
          title_mr: title.trim(),
          body_mr: body.trim(),
          contributed_by_name: credit.trim() || null,
        },
      })
      setDone(true)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Could not send it.')
    }
  }

  if (done) {
    return (
      <>
        <p>{t('her.submitted', lang)}</p>
        <button type="button" class="btn btn--quiet" onClick={onClose}>
          {t('common.close', lang)}
        </button>
      </>
    )
  }

  return (
    <>
      <label class="field">
        <span>{t('her.what', lang)}</span>
        <select
          value={kind}
          onChange={(e) => setKind((e.target as HTMLSelectElement).value as HeritageKind)}
        >
          {HERITAGE_KINDS.map((key) => (
            <option key={key} value={key}>
              {t(`her.kind.${key}`, lang)}
            </option>
          ))}
        </select>
      </label>

      <label class="field">
        <span>{t('her.titleField', lang)}</span>
        <input value={title} onInput={(e) => setTitle((e.target as HTMLInputElement).value)} />
      </label>

      <label class="field">
        <span>{t('her.bodyField', lang)}</span>
        <textarea
          rows={4}
          value={body}
          onInput={(e) => setBody((e.target as HTMLTextAreaElement).value)}
        />
      </label>

      <label class="field">
        <span>{t('her.credit', lang)}</span>
        <input value={credit} onInput={(e) => setCredit((e.target as HTMLInputElement).value)} />
      </label>
      <p class="muted">{t('her.creditWhy', lang)}</p>

      {error && <p class="error">{error}</p>}

      <button
        type="button"
        class="btn btn--primary"
        onClick={() => void submit()}
        disabled={title.trim().length < 1 || body.trim().length < 2}
      >
        {t('help.submit', lang)}
      </button>
      <button type="button" class="btn btn--quiet" onClick={onClose}>
        {t('common.close', lang)}
      </button>
    </>
  )
}

// ---------------------------------------------------------------------------
// help
// ---------------------------------------------------------------------------
export function HelpScreen({ shell, onToggleLang }: { shell: Shell; onToggleLang: () => void }) {
  const { lang, essentials } = shell

  return (
    <>
      <h1 class="title">{t('nav.help', lang)}</h1>

      <section class="card">
        <h2>{t('help.emergency', lang)}</h2>
        {(essentials?.emergency_contacts ?? []).map((c) => (
          <a
            key={c.number}
            class={`btn btn--call ${c.is_primary ? 'btn--primary' : ''}`}
            href={`tel:${c.number}`}
          >
            {s(c.label, c.label_mr, lang)} · {c.number}
          </a>
        ))}
      </section>

      <AccessibilityCard shell={shell} />

      <MissingPersonForm shell={shell} />

      <LostAndFound shell={shell} />

      <section class="card">
        <button type="button" class="btn" onClick={onToggleLang}>
          {t('help.language', lang)}
        </button>
      </section>
    </>
  )
}

/**
 * Accessibility (Track 1, item 4).
 *
 * Two things in one card, because to the pilgrim they are one thing:
 *
 * **"I need help now"** is the primary action and sits first. It is one tap and
 * it is queued, exactly like the SOS — somebody stuck at a step is very often
 * standing in a dead spot, and the SLA clock the server starts is the moment
 * they pressed, not the moment the signal came back.
 *
 * **"Set what I need"** is the standing declaration. Filling it in makes the
 * button above send the right needs without asking again, and — the part worth
 * saying out loud in the UI — opens the reserved darshan slots. A pilgrim who
 * cannot see why they would fill in a form about their disability will not fill
 * it in, so the card says what it buys them.
 */
const ACC_NEEDS = [
  'wheelchair',
  'walking_support',
  'step_free_route',
  'vision',
  'hearing',
  'speech',
  'cognitive',
  'companion_required',
  'oxygen',
  'stretcher',
] as const

type AccNeed = (typeof ACC_NEEDS)[number]

function AccessibilityCard({ shell }: { shell: Shell }) {
  const { lang } = shell
  const [open, setOpen] = useState(false)
  const [needs, setNeeds] = useState<AccNeed[]>([])
  const [largeText, setLargeText] = useState(false)
  const [highContrast, setHighContrast] = useState(false)
  const [priority, setPriority] = useState(false)
  const [saved, setSaved] = useState(false)
  const [asked, setAsked] = useState<'no' | 'sent' | 'queued'>('no')

  useEffect(() => {
    void accessibility
      .profile()
      .then((profile) => {
        setNeeds(profile.needs.filter((n): n is AccNeed => (ACC_NEEDS as readonly string[]).includes(n)))
        setLargeText(profile.large_text)
        setHighContrast(profile.high_contrast)
        setPriority(profile.priority_booking)
      })
      .catch(() => {
        // Offline, or not signed in yet. The card still works — the local
        // preferences below are applied from localStorage on boot regardless.
      })
  }, [])

  const toggle = (need: AccNeed) =>
    setNeeds((current) =>
      current.includes(need) ? current.filter((n) => n !== need) : [...current, need],
    )

  const save = useCallback(async () => {
    applyDisplayPreferences({ largeText, highContrast })
    try {
      await accessibility.declare({ needs, large_text: largeText, high_contrast: highContrast })
      const profile = await accessibility.profile()
      setPriority(profile.priority_booking)
    } catch {
      // The display preferences are already applied and stored locally; the
      // server copy can wait for a signal.
    }
    setSaved(true)
  }, [needs, largeText, highContrast])

  const askForHelp = useCallback(async () => {
    // Queued first, network second — the same order as the SOS button, and for
    // the same reason: the request must survive the app being closed.
    await shell.enqueue('assistance', { needs, language: lang })
    setAsked('queued')
    try {
      await request('/assistance', { method: 'POST', body: { needs, language: lang } })
      setAsked('sent')
    } catch {
      /* stays queued, and the card says so */
    }
  }, [shell, needs, lang])

  return (
    <section class="card">
      <h2>{t('acc.title', lang)}</h2>

      {asked !== 'no' ? (
        <p>{asked === 'sent' ? t('acc.requested', lang) : t('acc.requestQueued', lang)}</p>
      ) : (
        <button type="button" class="btn btn--primary" onClick={() => void askForHelp()}>
          {t('acc.needHelpNow', lang)}
        </button>
      )}

      {priority && <p class="muted">{t('acc.priority', lang)}</p>}

      {!open ? (
        <button type="button" class="btn" onClick={() => setOpen(true)}>
          {t('acc.declare', lang)}
        </button>
      ) : (
        <>
          {ACC_NEEDS.map((need) => (
            <label key={need} class="field field--check">
              <input
                type="checkbox"
                checked={needs.includes(need)}
                onChange={() => toggle(need)}
              />
              <span>{t(`acc.need.${need}`, lang)}</span>
            </label>
          ))}

          <label class="field field--check">
            <input
              type="checkbox"
              checked={largeText}
              onChange={(e) => setLargeText((e.target as HTMLInputElement).checked)}
            />
            <span>{t('acc.largeText', lang)}</span>
          </label>
          <label class="field field--check">
            <input
              type="checkbox"
              checked={highContrast}
              onChange={(e) => setHighContrast((e.target as HTMLInputElement).checked)}
            />
            <span>{t('acc.highContrast', lang)}</span>
          </label>

          {saved && <p class="muted">{t('acc.saved', lang)}</p>}

          <button type="button" class="btn btn--primary" onClick={() => void save()}>
            {t('help.submit', lang)}
          </button>
          <button type="button" class="btn btn--quiet" onClick={() => setOpen(false)}>
            {t('common.close', lang)}
          </button>
        </>
      )}
    </section>
  )
}

/** The categories, in the order a pilgrim is most likely to need them. */
const LF_CATEGORIES = [
  'bag',
  'phone',
  'documents',
  'money_purse',
  'walking_aid',
  'jewellery',
  'footwear',
  'clothing',
  'medicine',
  'religious_item',
  'other',
] as const

type LfCategory = (typeof LF_CATEGORIES)[number]

interface FoundEntry {
  reference: string
  category: LfCategory
  description: string
  colour: string | null
  zone_name_mr: string | null
  found_on: string
  custody_desk: string | null
  custody_desk_mr: string | null
}

/**
 * Lost and found, pilgrim side.
 *
 * Two halves, and they are asymmetric on purpose. Reporting a loss is queued
 * like an SOS — offline-first, because the moment somebody realises their bag
 * is gone is very often in a dead spot. Browsing what has been handed in is a
 * live read with a cache fallback, and it deliberately shows *less* than the
 * server knows: no identifying marks, no photos, no times. Enough to make
 * somebody walk to the right desk, never enough to describe an item they have
 * never seen.
 *
 * The identifying-mark field carries its own explanation, because a form that
 * asks a 70-year-old for a secret without saying why gets a blank field or a
 * useless one.
 */
function LostAndFound({ shell }: { shell: Shell }) {
  const { lang } = shell
  const [mode, setMode] = useState<'closed' | 'report' | 'browse'>('closed')

  if (mode === 'closed') {
    return (
      <section class="card">
        <h2>{t('lf.title', lang)}</h2>
        <button type="button" class="btn" onClick={() => setMode('report')}>
          {t('lf.reportLost', lang)}
        </button>
        <button type="button" class="btn" onClick={() => setMode('browse')}>
          {t('lf.searchFound', lang)}
        </button>
      </section>
    )
  }

  return mode === 'report' ? (
    <LostItemForm shell={shell} onClose={() => setMode('closed')} />
  ) : (
    <FoundRegister shell={shell} onClose={() => setMode('closed')} />
  )
}

function LostItemForm({ shell, onClose }: { shell: Shell; onClose: () => void }) {
  const { lang } = shell
  const [category, setCategory] = useState<LfCategory>('bag')
  const [description, setDescription] = useState('')
  const [colour, setColour] = useState('')
  const [mark, setMark] = useState('')
  const [done, setDone] = useState(false)

  const submit = useCallback(async () => {
    // Queued, not posted. `bodyToSend` stamps `occurred_at` from the moment the
    // pilgrim pressed this, not the moment the phone found a signal — matching
    // scores on when the thing was lost.
    await shell.enqueue('lost_item', {
      category,
      description: description.trim(),
      colour: colour.trim() || null,
      distinguishing_marks: mark.trim() || null,
      language: lang,
    })
    setDone(true)
  }, [shell, category, description, colour, mark, lang])

  if (done) {
    return (
      <section class="card">
        <p>{t('lf.queued', lang)}</p>
        <button type="button" class="btn btn--quiet" onClick={onClose}>
          {t('common.close', lang)}
        </button>
      </section>
    )
  }

  return (
    <section class="card">
      <h2>{t('lf.reportLost', lang)}</h2>

      <label class="field">
        <span>{t('lf.what', lang)}</span>
        <select
          value={category}
          onChange={(e) => setCategory((e.target as HTMLSelectElement).value as LfCategory)}
        >
          {LF_CATEGORIES.map((key) => (
            <option key={key} value={key}>
              {t(`lf.cat.${key}`, lang)}
            </option>
          ))}
        </select>
      </label>

      <label class="field">
        <span>{t('lf.describe', lang)}</span>
        <input
          value={description}
          onInput={(e) => setDescription((e.target as HTMLInputElement).value)}
        />
      </label>

      <label class="field">
        <span>{t('lf.colour', lang)}</span>
        <input value={colour} onInput={(e) => setColour((e.target as HTMLInputElement).value)} />
      </label>

      <label class="field">
        <span>{t('lf.mark', lang)}</span>
        <input value={mark} onInput={(e) => setMark((e.target as HTMLInputElement).value)} />
      </label>
      {/* Why the secret is worth giving. Without this the field comes back empty
          and the item can only ever be returned by a volunteer eyeballing it. */}
      <p class="muted">{t('lf.markWhy', lang)}</p>

      <button
        type="button"
        class="btn btn--primary"
        onClick={() => void submit()}
        disabled={description.trim().length < 2}
      >
        {t('help.submit', lang)}
      </button>
      <button type="button" class="btn btn--quiet" onClick={onClose}>
        {t('common.close', lang)}
      </button>
    </section>
  )
}

function FoundRegister({ shell, onClose }: { shell: Shell; onClose: () => void }) {
  const { lang } = shell
  const [category, setCategory] = useState<LfCategory | ''>('')
  const [entries, setEntries] = useState<FoundEntry[] | null>(null)
  const [fromCache, setFromCache] = useState(false)

  useEffect(() => {
    const query = category ? `?category=${category}` : ''
    void cached<{ items: FoundEntry[] }>(
      `${CACHE_KEYS.crowd}:lostfound:${category || 'all'}`,
      `/lost-found/search${query}`,
    ).then((result) => {
      setEntries(result ? result.value.items : [])
      setFromCache(result?.fromCache ?? false)
    })
  }, [category])

  return (
    <section class="card">
      <h2>{t('lf.searchFound', lang)}</h2>

      <label class="field">
        <span>{t('lf.what', lang)}</span>
        <select
          value={category}
          onChange={(e) => setCategory((e.target as HTMLSelectElement).value as LfCategory | '')}
        >
          <option value="">{t('lf.cat.other', lang)}…</option>
          {LF_CATEGORIES.map((key) => (
            <option key={key} value={key}>
              {t(`lf.cat.${key}`, lang)}
            </option>
          ))}
        </select>
      </label>

      {entries === null && <p class="muted">{t('common.loading', lang)}</p>}
      {entries !== null && entries.length === 0 && <p class="muted">{t('lf.noneFound', lang)}</p>}

      {entries !== null && entries.length > 0 && (
        <>
          {/* The whole point of the coarse view: it gets somebody to the right
              desk, and the mark they give there is what proves it is theirs. */}
          <p class="muted">{t('lf.askAtDesk', lang)}</p>
          <ul class="list">
            {entries.map((entry) => (
              <li key={entry.reference}>
                <strong>{t(`lf.cat.${entry.category}`, lang)}</strong> · {entry.description}
                {entry.colour && <> · {entry.colour}</>}
                <br />
                <span class="muted">
                  {entry.found_on}
                  {(entry.custody_desk_mr || entry.custody_desk) && (
                    <>
                      {' · '}
                      {t('lf.atDesk', lang)}: {s(entry.custody_desk ?? '', entry.custody_desk_mr ?? '', lang)}
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {fromCache && <p class="muted">{t('offline.banner', lang)}</p>}

      <button type="button" class="btn btn--quiet" onClick={onClose}>
        {t('common.close', lang)}
      </button>
    </section>
  )
}

function MissingPersonForm({ shell }: { shell: Shell }) {
  const { lang } = shell
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [age, setAge] = useState('')
  const [contact, setContact] = useState('')
  const [done, setDone] = useState(false)

  const submit = useCallback(async () => {
    await shell.enqueue('missing_person', {
      name,
      age: age ? Number(age) : null,
      contact_phone: contact,
      language: lang,
    })
    setDone(true)
  }, [shell, name, age, contact, lang])

  if (!open) {
    return (
      <section class="card">
        <button type="button" class="btn" onClick={() => setOpen(true)}>
          {t('help.missingPerson', lang)}
        </button>
      </section>
    )
  }

  if (done) {
    return (
      <section class="card">
        <p>{t('sos.queued', lang)}</p>
        <button type="button" class="btn btn--quiet" onClick={() => setOpen(false)}>
          {t('common.close', lang)}
        </button>
      </section>
    )
  }

  return (
    <section class="card">
      <h2>{t('help.missingPerson', lang)}</h2>
      <label class="field">
        <span>{t('help.name', lang)}</span>
        <input value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} />
      </label>
      <label class="field">
        <span>{t('help.age', lang)}</span>
        <input
          type="number"
          inputMode="numeric"
          value={age}
          onInput={(e) => setAge((e.target as HTMLInputElement).value)}
        />
      </label>
      <label class="field">
        <span>{t('help.contact', lang)}</span>
        <input
          type="tel"
          inputMode="numeric"
          value={contact}
          onInput={(e) => setContact((e.target as HTMLInputElement).value)}
        />
      </label>
      <button
        type="button"
        class="btn btn--primary"
        onClick={() => void submit()}
        disabled={name.trim().length < 1 || contact.trim().length < 6}
      >
        {t('help.submit', lang)}
      </button>
    </section>
  )
}
