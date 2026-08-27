/**
 * IndexedDB: the pass, the queue, and the last thing we knew.
 *
 * Thin on purpose. Every rule about *what* to keep and *when* it stops being
 * true lives in `queue.ts` and `freshness.ts`, which are testable without a
 * browser. This file is the plumbing those rules sit on.
 *
 * IndexedDB rather than localStorage for two reasons that both matter here: it
 * survives storage pressure better on a 2016 Android with 1 GB of RAM, and it
 * is available inside the service worker, which is where a background sync
 * would drain the queue.
 */

import type { QueuedAction } from './queue'

const DB_NAME = 'wariverse'
const DB_VERSION = 1

/** The pass, its secret, and the cached envelope. One row; there is one pass. */
const PASS_STORE = 'pass'
/** Offline actions awaiting a network. */
const QUEUE_STORE = 'queue'
/** Cached API responses, each with the time it was fetched. */
const CACHE_STORE = 'cache'

let handle: Promise<IDBDatabase> | null = null

function open(): Promise<IDBDatabase> {
  handle ??= new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(PASS_STORE)) db.createObjectStore(PASS_STORE)
      if (!db.objectStoreNames.contains(QUEUE_STORE)) db.createObjectStore(QUEUE_STORE, { keyPath: 'id' })
      if (!db.objectStoreNames.contains(CACHE_STORE)) db.createObjectStore(CACHE_STORE)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  return handle
}

function run<T>(store: string, mode: IDBTransactionMode, work: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const tx = db.transaction(store, mode)
        const request = work(tx.objectStore(store))
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      }),
  )
}

// ---------------------------------------------------------------------------
// the pass
// ---------------------------------------------------------------------------
export interface StoredPass {
  id: string
  reference: string
  /** Base32. Handed over once at booking and never again — losing it means the
   *  device can no longer compute a rolling code offline. */
  secret: string
  /** Last envelope the server gave us. Signed, so caching it is safe. */
  envelope: string | null
  envelopeFetchedAt: string | null
  /** The whole pass view, for rendering while offline. */
  view: unknown
}

export const passStore = {
  get: () => run<StoredPass | undefined>(PASS_STORE, 'readonly', (s) => s.get('current')),
  put: (pass: StoredPass) => run(PASS_STORE, 'readwrite', (s) => s.put(pass, 'current')),
  clear: () => run(PASS_STORE, 'readwrite', (s) => s.delete('current')),
}

// ---------------------------------------------------------------------------
// the queue
// ---------------------------------------------------------------------------
export const queueStore = {
  all: () => run<QueuedAction[]>(QUEUE_STORE, 'readonly', (s) => s.getAll()),
  put: (action: QueuedAction) => run(QUEUE_STORE, 'readwrite', (s) => s.put(action)),
  remove: (id: string) => run(QUEUE_STORE, 'readwrite', (s) => s.delete(id)),
}

/** Local id for something that has never been to a server. */
export function localId(): string {
  return `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

// ---------------------------------------------------------------------------
// cached responses
// ---------------------------------------------------------------------------
export interface CachedAt<T> {
  value: T
  fetchedAt: string
}

export const cacheStore = {
  get: <T>(key: string) => run<CachedAt<T> | undefined>(CACHE_STORE, 'readonly', (s) => s.get(key)),
  put: <T>(key: string, value: T) =>
    run(CACHE_STORE, 'readwrite', (s) =>
      s.put({ value, fetchedAt: new Date().toISOString() } satisfies CachedAt<T>, key),
    ),
}

/** Cache keys. Named so the service worker and the app cannot disagree. */
export const CACHE_KEYS = {
  crowd: 'crowd.public',
  facilities: 'facilities',
  essentials: 'pilgrim.essentials',
  zones: 'zones',
  // The archive is the one cache worth keeping indefinitely: an abhang from the
  // 17th century does not go stale, and the pilgrim most likely to read one is
  // on a bus with no signal.
  heritage: 'heritage',
} as const
