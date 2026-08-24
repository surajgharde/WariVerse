/**
 * The console shell.
 *
 * Section 4/M3 specifies the layout: a persistent left rail, a tabbed centre,
 * a prioritised alert feed on the right, and six KPIs across the top. That is
 * what this is, with one deliberate deviation, flagged rather than silently
 * taken:
 *
 * > The spec puts the live map in the *left rail* and offers the map again as
 * > one of the centre tabs. Rendering the same map twice costs the centre — the
 * > largest area on the screen — to duplicate something already visible, and
 * > Section 10 is explicit that the density map is the product. So the map is
 * > the centre's default tab at full size, and the left rail carries the zone
 * > roster: every zone, fixed order, with its reading, staleness and flow. The
 * > rail answers "what is the state of zone C" without a click, which is what
 * > the rail-map was for; the centre gives the map the room the spec's own
 * > design direction asks for.
 *
 * Incident pins and the Palkhi marker are absent because incidents (Phase 5)
 * and Palkhi tracking (Phase 9) do not exist yet. They are map layers, not
 * redesigns — `ZoneMap` gains two sources when those phases land.
 */

import { useCallback, useState } from 'react'

import type { ReplayFrame } from '@/api/types'
import { AlertFeed } from '@/components/AlertFeed'
import { CameraGrid } from '@/components/CameraGrid'
import { ChangeStrip } from '@/components/ChangeStrip'
import { KpiStrip } from '@/components/KpiStrip'
import { ReplayScrubber } from '@/components/ReplayScrubber'
import { SignIn } from '@/components/SignIn'
import { ZoneMap } from '@/components/ZoneMap'
import { ZoneRail } from '@/components/ZoneRail'
import { useI18n } from '@/i18n'
import { AuthProvider, useAuth } from '@/state/auth'
import { LiveProvider, useLive } from '@/state/live'

type Tab = 'map' | 'cameras' | 'replay'

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  )
}

function Gate() {
  const { status, forbidden, signOut } = useAuth()
  const { t } = useI18n()

  // An expired session must land back on the sign-in screen, not on an empty
  // console. A console rendering blank rails looks exactly like a quiet temple.
  const onAuthFailure = useCallback(() => {
    void signOut()
  }, [signOut])

  if (status === 'checking') {
    return <main className="boot">{t('common.loading')}</main>
  }

  if (status !== 'authenticated') return <SignIn />

  if (forbidden) {
    return (
      <main className="boot boot--error">
        <p>{t('error.forbidden')}</p>
        <button type="button" className="btn" onClick={() => void signOut()}>
          {t('auth.signOut')}
        </button>
      </main>
    )
  }

  return (
    <LiveProvider onAuthFailure={onAuthFailure}>
      <Console />
    </LiveProvider>
  )
}

function Console() {
  const [tab, setTab] = useState<Tab>('map')
  const [selectedZone, setSelectedZone] = useState<string | null>(null)
  const [frame, setFrame] = useState<ReplayFrame | null>(null)
  const { loading, error, refresh } = useLive()
  const { t } = useI18n()

  if (error) {
    return (
      <main className="boot boot--error">
        <h1>{t('error.title')}</h1>
        <p>{error}</p>
        <button type="button" className="btn btn--primary" onClick={refresh}>
          {t('error.retry')}
        </button>
      </main>
    )
  }

  if (loading) return <main className="boot">{t('common.loading')}</main>

  return (
    <div className="console">
      <TopBar />
      <KpiStrip />
      <ChangeStrip />

      <div className="console__body">
        <ZoneRail selected={selectedZone} onSelect={setSelectedZone} />

        <main className="centre">
          <nav className="tabs" role="tablist">
            {(['map', 'cameras', 'replay'] as const).map((key) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                className={`tab ${tab === key ? 'tab--active' : ''}`}
                onClick={() => setTab(key)}
              >
                {t(`nav.${key}` as const)}
              </button>
            ))}
          </nav>

          <div className="centre__body">
            {tab === 'cameras' ? (
              <CameraGrid />
            ) : (
              <>
                {/* One map instance across both tabs. Remounting MapLibre on a
                    tab switch would re-download the style and flash the screen
                    black — in a control room a black map reads as an outage. */}
                <ZoneMap frame={tab === 'replay' ? frame : null} />
                {tab === 'replay' && <ReplayScrubber onFrame={setFrame} />}
              </>
            )}
          </div>
        </main>

        <AlertFeed />
      </div>
    </div>
  )
}

function TopBar() {
  const { user, signOut } = useAuth()
  const { config, socket, kpis } = useLive()
  const { t, toggle } = useI18n()

  const sourceKey =
    config?.crowd_source === 'live' ? 'source.live' : config?.crowd_source === 'video' ? 'source.video' : 'source.sim'

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <strong>{t('app.title')}</strong>
        <span className="topbar__subtitle">{t('app.subtitle')}</span>
      </div>

      {/* An operator must always know whether they are watching the temple or a
          simulation. This badge is loud on purpose when it is not live. */}
      <span className={`badge badge--source badge--source-${config?.crowd_source ?? 'sim'}`}>
        {t(sourceKey as 'source.sim')}
      </span>

      <ConnectionBadge state={socket} />

      {kpis && kpis.stale_count > 0 && (
        <span
          className="badge badge--stale"
          title="Several numbers have gone cold at once. That pattern means a pipeline died, not a quiet temple."
        >
          {kpis.stale_count} stale
        </span>
      )}

      <div className="topbar__spacer" />

      <button type="button" className="btn btn--ghost" onClick={toggle}>
        {t('common.language')}
      </button>

      <span className="topbar__user">
        {user?.name} <span className="topbar__role mono">{user?.role}</span>
      </span>

      <button type="button" className="btn btn--ghost" onClick={() => void signOut()}>
        {t('auth.signOut')}
      </button>
    </header>
  )
}

function ConnectionBadge({ state }: { state: 'connecting' | 'open' | 'reconnecting' | 'closed' }) {
  const { t } = useI18n()

  const label =
    state === 'open'
      ? t('conn.live')
      : state === 'connecting'
        ? t('conn.connecting')
        : state === 'reconnecting'
          ? t('conn.reconnecting')
          : t('conn.closed')

  return (
    <span
      className={`badge badge--conn badge--conn-${state}`}
      title={state === 'open' ? undefined : t('conn.degraded')}
    >
      ● {label}
    </span>
  )
}
