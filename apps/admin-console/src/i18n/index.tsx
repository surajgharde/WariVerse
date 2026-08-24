/**
 * UI chrome translation.
 *
 * The split, restated from `schemas/command.py`: **if a string describes a
 * fact, the server writes it.** Zone names, alert summaries, recommended
 * actions, KPI notes and the "what changed" lines all arrive from the API in
 * both languages, because they depend on server state and the admin console
 * and the pilgrim app must not word the same fact differently.
 *
 * What lives here is only chrome — tab names, button labels, column headings.
 * Nothing in this file is operationally load-bearing, which is why it can be a
 * flat dictionary rather than i18next.
 *
 * Marathi is the default. This console is operated in Pandharpur.
 */

import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

export type Lang = 'mr' | 'en'

const STRINGS = {
  'app.title': { mr: 'कमांड सेंटर', en: 'Command Center' },
  'app.subtitle': { mr: 'श्री विठ्ठल-रुक्मिणी मंदिर, पंढरपूर', en: 'Shri Vitthal-Rukmini Temple, Pandharpur' },

  'nav.map': { mr: 'नकाशा', en: 'Map' },
  'nav.cameras': { mr: 'कॅमेरे', en: 'Cameras' },
  'nav.replay': { mr: 'रीप्ले', en: 'Replay' },
  'nav.rules': { mr: 'नियम', en: 'Rules' },

  'auth.signIn': { mr: 'साइन इन', en: 'Sign in' },
  'auth.phone': { mr: 'फोन क्रमांक', en: 'Phone number' },
  'auth.password': { mr: 'पासवर्ड', en: 'Password' },
  'auth.mfaCode': { mr: 'सहा-अंकी कोड', en: 'Six-digit code' },
  'auth.mfaPrompt': {
    mr: 'तुमच्या ऑथेंटिकेटर अ‍ॅपमधील कोड टाका.',
    en: 'Enter the code from your authenticator app.',
  },
  'auth.signOut': { mr: 'साइन आउट', en: 'Sign out' },
  'auth.working': { mr: 'तपासत आहे…', en: 'Checking…' },

  'kpi.asOf': { mr: 'यावेळची माहिती', en: 'As of' },
  'kpi.target': { mr: 'लक्ष्य', en: 'target' },
  'kpi.stale': { mr: 'जुनी माहिती', en: 'STALE' },
  'kpi.unknown': { mr: 'माहिती नाही', en: 'no reading' },

  'alerts.title': { mr: 'सूचना', en: 'Alerts' },
  'alerts.acknowledge': { mr: 'स्वीकारा', en: 'Acknowledge' },
  'alerts.dispatch': { mr: 'पथक पाठवा', en: 'Dispatch' },
  'alerts.resolve': { mr: 'निकाली काढा', en: 'Resolve' },
  'alerts.empty': {
    mr: 'सध्या कोणतीही सूचना नाही. सर्व झोन नियंत्रणात आहेत.',
    en: 'No alerts right now. Every reporting zone is within its band.',
  },
  'alerts.emptyStale': {
    mr: 'कोणतीही सूचना नाही — पण काही झोनकडून माहिती येत नाही. शांतता आणि अंधार यात फरक आहे.',
    en: 'No alerts — but some zones are not reporting. Quiet is not the same as clear.',
  },
  'alerts.confidence': { mr: 'खात्री', en: 'confidence' },
  'alerts.rule': { mr: 'नियम', en: 'rule' },
  'alerts.age': { mr: 'कालावधी', en: 'open for' },
  'alerts.escalated': { mr: 'वाढवले', en: 'ESCALATED' },
  'alerts.paging': { mr: 'पुढील अधिकाऱ्यास कळवले', en: 'PAGED' },
  'alerts.acknowledgedBy': { mr: 'स्वीकारले', en: 'Acknowledged' },

  'changes.title': { mr: 'गेल्या १५ मिनिटांत काय बदलले', en: 'What changed in the last 15 minutes' },
  'changes.empty': { mr: 'गेल्या १५ मिनिटांत काहीही बदललेले नाही.', en: 'Nothing changed in the last 15 minutes.' },
  'changes.truncated': {
    mr: 'यादी मर्यादित केली आहे — या कालावधीत आणखी बदल झाले आहेत.',
    en: 'List truncated — more changed in this window than is shown.',
  },

  'zones.title': { mr: 'झोन', en: 'Zones' },
  'zones.unknown': { mr: 'माहिती नाही', en: 'No reading' },
  'zones.people': { mr: 'व्यक्ती', en: 'people' },
  'zones.flow': { mr: 'प्रवाह', en: 'Flow' },
  'zones.stagnation': { mr: 'स्थिरता निर्देशांक', en: 'Stagnation' },
  'zones.counterflow': { mr: 'विरुद्ध प्रवाह', en: 'Counterflow' },
  'zones.occupancy': { mr: 'भरणा', en: 'Occupancy' },
  'zones.cameras': { mr: 'कॅमेरे', en: 'Cameras' },
  'zones.static': { mr: 'स्थिर', en: 'static' },

  'replay.title': { mr: 'गेल्या तासाचा रीप्ले', en: 'Replay the last hour' },
  'replay.play': { mr: 'चालू करा', en: 'Play' },
  'replay.pause': { mr: 'थांबवा', en: 'Pause' },
  'replay.live': { mr: 'थेट', en: 'LIVE' },
  'replay.empty': {
    mr: 'या कालावधीत कोणतीही नोंद नाही. रीप्लेसाठी माहिती उपलब्ध नाही.',
    en: 'No readings in this window. There is nothing to replay.',
  },
  'replay.window': { mr: 'कालावधी', en: 'Window' },
  'replay.openAlerts': { mr: 'सुरू सूचना', en: 'open alerts' },

  'cameras.title': { mr: 'कॅमेरे', en: 'Cameras' },
  'cameras.online': { mr: 'सुरू', en: 'online' },
  'cameras.degraded': { mr: 'अंशतः', en: 'degraded' },
  'cameras.offline': { mr: 'बंद', en: 'offline' },
  'cameras.uncalibrated': { mr: 'कॅलिब्रेट नाही', en: 'uncalibrated' },
  'cameras.lastSeen': { mr: 'शेवटची नोंद', en: 'Last heartbeat' },
  'cameras.empty': {
    mr: 'एकही कॅमेरा नोंदवलेला नाही. गर्दीची माहिती सिम्युलेशन किंवा हाताने येत आहे.',
    en: 'No cameras registered. Density is coming from simulation or manual entry.',
  },

  'conn.live': { mr: 'थेट जोडलेले', en: 'Live' },
  'conn.connecting': { mr: 'जोडत आहे…', en: 'Connecting…' },
  'conn.reconnecting': { mr: 'पुन्हा जोडत आहे…', en: 'Reconnecting…' },
  'conn.closed': { mr: 'जोडणी तुटली', en: 'Disconnected' },
  'conn.degraded': {
    mr: 'थेट जोडणी नाही — दर काही सेकंदांनी माहिती पुन्हा मागवली जात आहे.',
    en: 'No live socket — falling back to polling. Numbers may lag.',
  },

  'source.sim': {
    mr: 'सिम्युलेशन — ही खरी गर्दी नाही',
    en: 'SIMULATION — this is not live crowd data',
  },
  'source.video': { mr: 'रेकॉर्ड केलेला व्हिडिओ', en: 'RECORDED VIDEO' },
  'source.live': { mr: 'थेट कॅमेरे', en: 'LIVE CAMERAS' },

  'error.title': { mr: 'काहीतरी चुकले', en: 'Something went wrong' },
  'error.retry': { mr: 'पुन्हा प्रयत्न करा', en: 'Try again' },
  'error.trace': { mr: 'ट्रेस आयडी', en: 'Trace id' },
  'error.forbidden': {
    mr: 'या स्क्रीनसाठी तुमच्या भूमिकेस परवानगी नाही.',
    en: 'Your role does not have access to this screen.',
  },

  'common.loading': { mr: 'लोड होत आहे…', en: 'Loading…' },
  'common.language': { mr: 'English', en: 'मराठी' },
  'common.close': { mr: 'बंद करा', en: 'Close' },
} as const

export type StringKey = keyof typeof STRINGS

interface I18n {
  lang: Lang
  t: (key: StringKey) => string
  /** Pick the right side of a server-supplied bilingual pair. */
  s: (en: string | null | undefined, mr: string | null | undefined) => string
  toggle: () => void
}

const I18nContext = createContext<I18n | null>(null)

const LANG_KEY = 'wariverse.lang'

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) as Lang) ?? 'mr')

  const toggle = useCallback(() => {
    setLang((current) => {
      const next: Lang = current === 'mr' ? 'en' : 'mr'
      localStorage.setItem(LANG_KEY, next)
      return next
    })
  }, [])

  const value = useMemo<I18n>(
    () => ({
      lang,
      t: (key) => STRINGS[key][lang],
      // Falls back to the other language rather than rendering nothing: a
      // missing Marathi translation should still show the operator the fact.
      s: (en, mr) => (lang === 'mr' ? (mr ?? en ?? '') : (en ?? mr ?? '')),
      toggle,
    }),
    [lang, toggle],
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18n {
  const context = useContext(I18nContext)
  if (!context) throw new Error('useI18n must be used inside I18nProvider')
  return context
}
