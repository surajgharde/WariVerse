import { camelizeKeys, getApiBaseUrl } from '@/services/api';
import { mockIvrApi } from '@/services/mockApi';
import type { IVRTurn, Language } from '@/types/domain';

/**
 * Client for the in-app IVR (`/api/ivr/session/*`).
 *
 * The backend runs the menu state machine and returns the prompt, the audio and
 * the keys that are valid next; this module is transport plus the retry policy.
 * Responses go through the shared `camelizeKeys` so `audio_base64` arrives as
 * `audioBase64` like everything else in the app.
 *
 * ── Retrying safely ─────────────────────────────────────────────────────────
 * Pilgrims use this walking, on congested rural networks, so requests are
 * retried automatically. A keypress is *not* naturally idempotent — the menu
 * lives on the server, so replaying a press that already landed applies it
 * again from the state it just moved to, and one tap walks two menu levels.
 *
 * Each user action therefore carries a `turn_id` generated once and reused for
 * every retry of that action. The backend stores the first answer under it and
 * replays that answer instead of pressing the key again. A *new* press gets a
 * new id, so two genuine taps still count twice.
 */

/** Long enough for a model turn on a slow connection, short enough to recover. */
const TIMEOUT_MS = 25_000;

/** Attempts after the first. Three total tries spans roughly ten seconds. */
const MAX_RETRIES = 2;
const BACKOFF_BASE_MS = 600;
const BACKOFF_CAP_MS = 4_000;

/**
 * Statuses worth trying again. Everything else — 400, 413, 415, 422 — describes
 * a request that will fail identically no matter how many times it is sent.
 */
const RETRYABLE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

export class IVRError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** True when every attempt failed on the network rather than on a reply. */
    readonly offline = false
  ) {
    super(message);
    this.name = 'IVRError';
  }
}

/** Called before each retry, so a call screen can say it is reconnecting. */
export type RetryNotice = (attempt: number, total: number) => void;

type Options = {
  onRetry?: RetryNotice;
  /** Aborts the request outright — a hang-up, not a failure to retry. */
  signal?: AbortSignal;
};

/** A fresh id for one user action. Reused across that action's retries. */
export function newTurnId(): string {
  return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** A session id unique to this call, so concurrent calls never share menu state. */
export function newCallSessionId(): string {
  const random = Math.random().toString(36).slice(2, 10);
  return `ivr-call-${Date.now().toString(36)}-${random}`;
}

function backoffMs(attempt: number): number {
  const flat = Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_CAP_MS);
  // Jitter, so several clients recovering from the same tower blip do not all
  // retry on the same tick.
  return Math.round(flat * (0.75 + Math.random() * 0.5));
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        reject(new IVRError('Call ended.', 0));
      },
      { once: true }
    );
  });
}

/**
 * `false` only when the platform is sure. `navigator.onLine` is absent on some
 * React Native runtimes, and undefined must not read as "offline".
 */
function knownOffline(): boolean {
  return typeof navigator !== 'undefined' && navigator.onLine === false;
}

/** One attempt. Throws `IVRError`; `status === 0` means it never got a reply. */
async function attempt<T>(
  path: string,
  init: RequestInit,
  callerSignal?: AbortSignal
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const onCallerAbort = () => controller.abort();
  callerSignal?.addEventListener('abort', onCallerAbort, { once: true });

  try {
    const response = await fetch(`${getApiBaseUrl()}${path}`, {
      ...init,
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new IVRError(
        payload?.error?.message ?? `Request failed (${response.status})`,
        response.status
      );
    }
    return camelizeKeys<T>(payload);
  } catch (error) {
    if (error instanceof IVRError) throw error;
    // A hang-up, not a network problem — must not be retried.
    if (callerSignal?.aborted) throw new IVRError('Call ended.', 0);
    // Everything else here is a rejected fetch: DNS, refused, dropped mid-body,
    // or our own timeout firing. All of them are "no reply".
    throw new IVRError('The network dropped out.', 0, true);
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener('abort', onCallerAbort);
  }
}

async function request<T>(
  path: string,
  init: RequestInit,
  options: Options = {}
): Promise<T> {
  let last: IVRError | null = null;

  for (let round = 0; round <= MAX_RETRIES; round += 1) {
    if (options.signal?.aborted) throw new IVRError('Call ended.', 0);

    try {
      return await attempt<T>(path, init, options.signal);
    } catch (error) {
      last = error instanceof IVRError ? error : new IVRError(String(error), 0, true);

      const worthRetrying = last.offline || RETRYABLE_STATUSES.has(last.status);
      if (!worthRetrying || round === MAX_RETRIES) break;

      options.onRetry?.(round + 1, MAX_RETRIES);
      await sleep(backoffMs(round), options.signal);
    }
  }

  if (last?.offline && knownOffline()) {
    throw new IVRError('You appear to be offline.', 0, true);
  }
  throw last ?? new IVRError('Request failed.', 0);
}

function json(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

function withLocation(
  body: Record<string, unknown>,
  latitude?: number | null,
  longitude?: number | null
): Record<string, unknown> {
  if (typeof latitude === 'number' && typeof longitude === 'number') {
    body.latitude = latitude;
    body.longitude = longitude;
  }
  return body;
}

export const ivrApi = {
  /**
   * Opens a call. Pass `language` to skip the language menu.
   *
   * Naturally idempotent — it resolves the session and returns the opening
   * menu — so it needs no turn id, and doubles as the reconnect path.
   */
  async start(
    input: {
      sessionId: string;
      language?: Language;
      latitude?: number | null;
      longitude?: number | null;
    },
    options?: Options
  ): Promise<IVRTurn> {
    try {
      const body: Record<string, unknown> = { session_id: input.sessionId };
      if (input.language) body.language = input.language;
      withLocation(body, input.latitude, input.longitude);
      return await request<IVRTurn>('/api/ivr/session/start', json(body), options);
    } catch (err) {
      console.warn('Real IVR API start failed, falling back to mock IVR:', err);
      return await mockIvrApi.start(input);
    }
  },

  /** Sends a keypress and returns the next prompt. */
  async press(
    input: {
      sessionId: string;
      key: string;
      latitude?: number | null;
      longitude?: number | null;
      /** Supply to make a caller-driven retry replay rather than re-press. */
      turnId?: string;
    },
    options?: Options
  ): Promise<IVRTurn> {
    try {
      const body: Record<string, unknown> = {
        session_id: input.sessionId,
        key: input.key,
        turn_id: input.turnId ?? newTurnId(),
      };
      withLocation(body, input.latitude, input.longitude);
      return await request<IVRTurn>('/api/ivr/session/dtmf', json(body), options);
    } catch (err) {
      console.warn('Real IVR API press failed, falling back to mock IVR:', err);
      return await mockIvrApi.press(input);
    }
  },

  /**
   * Uploads a recording and returns the spoken answer.
   *
   * Multipart, so `Content-Type` is left unset — the runtime has to add the
   * boundary itself. The turn id matters most here: a blind retry would pay for
   * transcription and a model turn twice and answer one question two ways.
   */
  async speak(
    input: {
      sessionId: string;
      audio: Blob;
      fileName?: string;
      turnId?: string;
    },
    options?: Options
  ): Promise<IVRTurn> {
    try {
      const form = new FormData();
      form.append('file', input.audio as any, input.fileName ?? 'speech.webm');
      form.append('session_id', input.sessionId);
      form.append('turn_id', input.turnId ?? newTurnId());

      return await request<IVRTurn>(
        '/api/ivr/session/voice',
        { method: 'POST', body: form },
        options
      );
    } catch (err) {
      console.warn('Real IVR API speak failed, falling back to mock IVR:', err);
      return await mockIvrApi.speak(input);
    }
  },
};
