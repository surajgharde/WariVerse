/**
 * Larger text and stronger contrast (Track 1, item 4).
 *
 * Stored locally and applied before the first paint, deliberately.
 *
 * The server holds the authoritative copy — a pilgrim who declared "larger
 * text" on a phone that then died should find it already on when they sign in
 * on a borrowed one. But waiting for that read means the first screen a
 * partially-sighted pilgrim sees is the small one, and on 2G that is several
 * seconds of squinting. So: apply from localStorage synchronously at boot,
 * reconcile with the server when it answers.
 *
 * The scale is applied to the root font size rather than by rewriting every
 * rule, because the whole stylesheet is already sized in `rem` against the
 * 18px base the spec sets for a 65-year-old reading in sunlight.
 */

const KEY = 'wariverse.pilgrim.display'

export interface DisplayPreferences {
  largeText: boolean
  highContrast: boolean
}

const DEFAULTS: DisplayPreferences = { largeText: false, highContrast: false }

export function storedPreferences(): DisplayPreferences {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return DEFAULTS
    const parsed = JSON.parse(raw) as Partial<DisplayPreferences>
    return {
      largeText: Boolean(parsed.largeText),
      highContrast: Boolean(parsed.highContrast),
    }
  } catch {
    // A corrupt preference must not stop the app booting. Defaults are legible;
    // they are simply not the pilgrim's choice.
    return DEFAULTS
  }
}

export function applyDisplayPreferences(preferences: DisplayPreferences): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(preferences))
  } catch {
    /* private mode, quota — the class below still applies for this session */
  }
  const root = document.documentElement
  root.classList.toggle('a11y-large', preferences.largeText)
  root.classList.toggle('a11y-contrast', preferences.highContrast)
}
