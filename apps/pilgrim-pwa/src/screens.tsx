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
import { auth, cached, request, tokens } from './lib/api'
import { CACHE_KEYS, passStore, queueStore } from './lib/db'
import type { StoredPass } from './lib/db'
import { formatAge, levelToShow, unknownAdvice } from './lib/freshness'
import { buildQrPayload, envelopeFromPayload, rollingCode, secondsUntilRotation } from './lib/totp'
import type { PassIssued, PassView, ZonePublic } from './lib/types'
import { s, t } from './i18n'
import type { Lang } from './i18n'
import { qrSvg } from './lib/qr'

// ---------------------------------------------------------------------------
// sign in
// ---------------------------------------------------------------------------
export function SignIn({ lang, onSignedIn }: { lang: Lang; onSignedIn: () => void }) {
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [stage, setStage] = useState<'phone' | 'code'>('phone')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const send = async () => {
    setBusy(true)
    setError(null)
    try {
      if (stage === 'phone') {
        await auth.requestOtp(phone)
        setStage('code')
      } else {
        const result = await auth.verifyOtp(phone, code, name || undefined)
        tokens.set(result)
        onSignedIn()
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Could not reach the server.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main class="screen screen--centred">
      <h1 class="title">{t('app.name', lang)}</h1>

      {stage === 'phone' ? (
        <label class="field">
          <span>{t('auth.phone', lang)}</span>
          <input
            type="tel"
            inputMode="numeric"
            value={phone}
            onInput={(e) => setPhone((e.target as HTMLInputElement).value)}
            autocomplete="tel"
          />
        </label>
      ) : (
        <>
          <label class="field">
            <span>{t('auth.code', lang)}</span>
            <input
              type="text"
              inputMode="numeric"
              value={code}
              onInput={(e) => setCode((e.target as HTMLInputElement).value)}
              autocomplete="one-time-code"
            />
          </label>
          <label class="field">
            <span>{t('auth.yourName', lang)}</span>
            <input value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} />
          </label>
        </>
      )}

      {error && <p class="error">{error}</p>}

      <button type="button" class="btn btn--primary" onClick={() => void send()} disabled={busy}>
        {stage === 'phone' ? t('auth.signIn', lang) : t('auth.verify', lang)}
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

      <MissingPersonForm shell={shell} />

      <section class="card">
        <button type="button" class="btn" onClick={onToggleLang}>
          {t('help.language', lang)}
        </button>
      </section>
    </>
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
