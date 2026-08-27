import { render } from 'preact'

import { App } from './App'
import { applyDisplayPreferences, storedPreferences } from './lib/display'
import { currentLang, setLang } from './i18n'
import './styles.css'

setLang(currentLang())

// Before the first paint, not after the profile read comes back. A partially
// sighted pilgrim on 2G would otherwise spend several seconds looking at the
// small type while the network decides. See `lib/display.ts`.
applyDisplayPreferences(storedPreferences())

const root = document.getElementById('root')
if (!root) throw new Error('#root is missing from index.html')

render(<App />, root)

// Register the service worker after first paint. Registering earlier competes
// with rendering for the main thread, and on a 2016 device that is visible.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => {
      // A failed registration costs the offline shell, not the app. Nothing to
      // show the pilgrim — they cannot act on it.
    })
  })
}
