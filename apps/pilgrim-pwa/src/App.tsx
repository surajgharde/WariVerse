/**
 * The pilgrim app shell (Section 4/M7).
 *
 * Five screens, and the spec says "that is all — resist adding more". They are
 * in one file because five screens with no router is genuinely smaller than
 * five files plus a routing library, and every kilobyte here is measured
 * against a 200 KB budget on a 2G connection.
 *
 * Two things the shell owns and the screens do not:
 *
 * **The offline banner.** Rendered once, above everything, whenever the network
 * is gone or the queue is non-empty. The spec requires it and it must not be
 * something an individual screen can forget.
 *
 * **The SOS button.** Present on Home, three taps maximum, and it works
 * offline — the press writes to IndexedDB before it tries the network, so the
 * request survives the app being closed.
 */

import { useCallback, useEffect, useState } from 'preact/hooks'

import { cached, tokens } from './lib/api'
import { CACHE_KEYS, localId, queueStore } from './lib/db'
import { ageSecondsSince, formatAge } from './lib/freshness'
import { dueNow } from './lib/queue'
import type { QueuedAction, QueuedKind } from './lib/queue'
import type { CrowdPublic, FacilityList, PilgrimEssentials } from './lib/types'
import { currentLang, setLang, t } from './i18n'
import type { Lang } from './i18n'
import { AlertsScreen, HelpScreen, HomeScreen, MapScreen, PassScreen } from './screens'
import { SignIn } from './screens'
import { drainQueue } from './lib/sync'

export type Tab = 'home' | 'pass' | 'map' | 'alerts' | 'help'

export interface Shell {
  lang: Lang
  online: boolean
  crowd: CrowdPublic | null
  crowdAgeSeconds: number | null
  facilities: FacilityList | null
  essentials: PilgrimEssentials | null
  pending: QueuedAction[]
  enqueue: (kind: QueuedKind, body: Record<string, unknown>) => Promise<QueuedAction>
  refresh: () => void
}

export function App() {
  const [tab, setTab] = useState<Tab>('home')
  const [lang, setLangState] = useState<Lang>(currentLang())
  const [online, setOnline] = useState(navigator.onLine)
  const [signedIn, setSignedIn] = useState(Boolean(tokens.access()))

  const [crowd, setCrowd] = useState<CrowdPublic | null>(null)
  const [crowdFetchedAt, setCrowdFetchedAt] = useState<string | null>(null)
  const [facilities, setFacilities] = useState<FacilityList | null>(null)
  const [essentials, setEssentials] = useState<PilgrimEssentials | null>(null)
  const [pending, setPending] = useState<QueuedAction[]>([])

  const reloadQueue = useCallback(async () => {
    setPending(await queueStore.all().catch(() => []))
  }, [])

  const refresh = useCallback(() => {
    // All three are anonymous, so they work before sign-in and after a token
    // expires — which is exactly when the network is likely to be the problem.
    void cached<CrowdPublic>(CACHE_KEYS.crowd, '/crowd/public', { anonymous: true }).then((r) => {
      if (r) {
        setCrowd(r.value)
        setCrowdFetchedAt(r.fetchedAt)
      }
    })
    void cached<FacilityList>(CACHE_KEYS.facilities, '/facilities', { anonymous: true }).then(
      (r) => r && setFacilities(r.value),
    )
    void cached<PilgrimEssentials>(CACHE_KEYS.essentials, '/pilgrim/essentials', {
      anonymous: true,
    }).then((r) => r && setEssentials(r.value))
    void reloadQueue()
  }, [reloadQueue])

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, 60_000)
    return () => window.clearInterval(timer)
  }, [refresh])

  // Drain the queue the instant a signal returns, and periodically while up.
  useEffect(() => {
    const goOnline = () => {
      setOnline(true)
      void drainQueue().then(reloadQueue)
    }
    const goOffline = () => setOnline(false)
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    const timer = window.setInterval(() => {
      if (navigator.onLine) void drainQueue().then(reloadQueue)
    }, 30_000)
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
      window.clearInterval(timer)
    }
  }, [reloadQueue])

  const enqueue = useCallback(
    async (kind: QueuedKind, body: Record<string, unknown>) => {
      // Written to IndexedDB *before* any network attempt, deliberately. If the
      // app is closed or killed between the tap and the response, the action
      // must still exist. For an SOS that is the difference between help coming
      // and nobody knowing.
      const action: QueuedAction = {
        id: localId(),
        kind,
        body,
        queuedAt: new Date().toISOString(),
        attempts: 0,
        lastAttemptAt: null,
        lastError: null,
      }
      await queueStore.put(action)
      await reloadQueue()
      void drainQueue().then(reloadQueue)
      return action
    },
    [reloadQueue],
  )

  const toggleLang = useCallback(() => {
    const next: Lang = lang === 'mr' ? 'en' : 'mr'
    setLang(next)
    setLangState(next)
  }, [lang])

  const shell: Shell = {
    lang,
    online,
    crowd,
    crowdAgeSeconds: ageSecondsSince(crowd?.generated_at ?? crowdFetchedAt),
    facilities,
    essentials,
    pending,
    enqueue,
    refresh,
  }

  if (!signedIn) {
    return <SignIn lang={lang} onSignedIn={() => setSignedIn(true)} />
  }

  const unsent = dueNow(pending).length

  return (
    <div class="app">
      {(!online || unsent > 0) && (
        <div class="banner" role="status">
          {!online && <strong>{t('offline.banner', lang)}</strong>}
          {crowd && (
            <span>
              {t('crowd.lastUpdated', lang)}: {formatAge(shell.crowdAgeSeconds, lang)}
            </span>
          )}
          {unsent > 0 && (
            <span class="banner__pending">
              {unsent} {t('offline.pending', lang)}
            </span>
          )}
        </div>
      )}

      <main class="screen">
        {tab === 'home' && <HomeScreen shell={shell} onGoto={setTab} />}
        {tab === 'pass' && <PassScreen shell={shell} />}
        {tab === 'map' && <MapScreen shell={shell} />}
        {tab === 'alerts' && <AlertsScreen shell={shell} />}
        {tab === 'help' && <HelpScreen shell={shell} onToggleLang={toggleLang} />}
      </main>

      <nav class="tabs" aria-label={t('app.name', lang)}>
        {(['home', 'pass', 'map', 'alerts', 'help'] as const).map((key) => (
          <button
            key={key}
            type="button"
            class={`tab ${tab === key ? 'tab--active' : ''}`}
            aria-current={tab === key ? 'page' : undefined}
            onClick={() => setTab(key)}
          >
            {t(`nav.${key}`, lang)}
          </button>
        ))}
      </nav>
    </div>
  )
}
