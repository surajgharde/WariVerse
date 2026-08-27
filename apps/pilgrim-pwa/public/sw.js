/**
 * Service worker (Section 4/M7).
 *
 * Plain JavaScript, hand-written, no Workbox. Workbox is ~15 KB of the budget
 * to express about forty lines of policy, and the policy here is unusual enough
 * that the generated version would need overriding anyway.
 *
 * Two caching strategies, chosen per resource by what it costs to be wrong:
 *
 * **App shell — cache first.** HTML, JS, CSS. These change when we deploy, not
 * during a Wari, and a pilgrim opening the app at a gate should get a screen
 * instantly rather than a network wait.
 *
 * **API reads — network first, cache fallback.** A live answer is better when
 * one exists. When it does not, a cached answer with its age shown beats a
 * spinner. The *app* is what renders the age — see `freshness.ts` — because the
 * service worker cannot know how a given screen should degrade.
 *
 * What is deliberately NOT cached: anything mutating. A POST that a service
 * worker replayed silently would be an SOS the pilgrim did not press or a pass
 * booked twice. Offline writes go through IndexedDB and `sync.ts`, where they
 * are visible and cancellable, never through opaque background replay.
 */

const VERSION = 'v1'
const SHELL_CACHE = `wariverse-shell-${VERSION}`
const API_CACHE = `wariverse-api-${VERSION}`

/** Enough to paint the first screen with no network at all. */
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg']

/**
 * API reads worth keeping. Each is either constant or slow-changing.
 *
 * `/crowd/public` is here despite changing constantly, because a stale crowd
 * reading *with its age attached* is genuinely useful and the alternative
 * offline is nothing at all. The app is required to render the age.
 */
const CACHEABLE_API = [
  '/api/v1/crowd/public',
  '/api/v1/facilities',
  '/api/v1/pilgrim/essentials',
  '/api/v1/zones',
]

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL))
      // Take over immediately. The alternative is a pilgrim running the old
      // shell until every tab is closed, which on a phone may be never.
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== SHELL_CACHE && key !== API_CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const request = event.request

  // Never touch a mutation. See the module comment: a silently replayed POST is
  // an SOS nobody pressed.
  if (request.method !== 'GET') return

  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return

  if (url.pathname.startsWith('/api/')) {
    if (CACHEABLE_API.some((path) => url.pathname.startsWith(path))) {
      event.respondWith(networkFirst(request))
    }
    // Everything else API-side is left to the network. A cached pass or a
    // cached incident list would be worse than an error.
    return
  }

  event.respondWith(cacheFirst(request))
})

async function cacheFirst(request) {
  const cached = await caches.match(request)
  if (cached) return cached

  try {
    const response = await fetch(request)
    if (response.ok) {
      const cache = await caches.open(SHELL_CACHE)
      void cache.put(request, response.clone())
    }
    return response
  } catch {
    // A navigation that cannot be served is the one case worth a fallback:
    // hand back the shell so the app boots and can explain itself.
    if (request.mode === 'navigate') {
      const shell = await caches.match('/index.html')
      if (shell) return shell
    }
    throw new Error('offline and not cached')
  }
}

async function networkFirst(request) {
  try {
    const response = await fetch(request)
    if (response.ok) {
      const cache = await caches.open(API_CACHE)
      void cache.put(request, response.clone())
    }
    return response
  } catch {
    const cached = await caches.match(request)
    if (cached) return cached
    // 503 rather than a fabricated empty body. An empty zone list would render
    // as "nothing to report", which is the opposite of the truth.
    return new Response(
      JSON.stringify({
        error: {
          code: 'OFFLINE',
          message: 'No network and nothing cached for this.',
          message_mr: 'नेटवर्क नाही आणि साठवलेली माहितीही नाही.',
          details: {},
          trace_id: null,
        },
      }),
      { status: 503, headers: { 'content-type': 'application/json' } },
    )
  }
}
