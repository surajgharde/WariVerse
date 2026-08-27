/**
 * Marathi first, English second.
 *
 * Not "Marathi supported" — Marathi *default*, and the language toggle is on the
 * Help screen rather than the home screen because the person who needs it is the
 * minority here.
 *
 * The split is the same as everywhere else in this product: strings that
 * describe a *fact* come from the server in both languages (zone advice, alert
 * text, the offline notice). What is here is chrome — labels, buttons, headings.
 */

export type Lang = 'mr' | 'en'

const STRINGS = {
  'app.name': { mr: 'वारीव्हर्स', en: 'WariVerse' },

  'nav.home': { mr: 'मुख्यपृष्ठ', en: 'Home' },
  'nav.pass': { mr: 'पास', en: 'Pass' },
  'nav.map': { mr: 'नकाशा', en: 'Map' },
  'nav.alerts': { mr: 'सूचना', en: 'Alerts' },
  'nav.help': { mr: 'मदत', en: 'Help' },

  'sos.button': { mr: 'मदत हवी', en: 'I need help' },
  'sos.confirm': { mr: 'नक्की मदत हवी?', en: 'Send for help?' },
  'sos.confirmYes': { mr: 'होय, मदत पाठवा', en: 'Yes, send help' },
  'sos.cancel': { mr: 'नको', en: 'Cancel' },
  'sos.sending': { mr: 'पाठवत आहे…', en: 'Sending…' },
  'sos.queued': {
    mr: 'नेटवर्क नाही. तुमची विनंती नोंदवली आहे आणि नेटवर्क आल्यावर लगेच पाठवली जाईल.',
    en: 'No network. Your request is saved and will be sent the moment a signal returns.',
  },
  'sos.callInstead': { mr: 'किंवा थेट फोन करा', en: 'Or call directly' },
  'sos.reference': { mr: 'संदर्भ क्रमांक', en: 'Reference' },

  'pass.none': { mr: 'तुमच्याकडे पास नाही', en: 'You have no pass' },
  'pass.book': { mr: 'पास काढा', en: 'Book a pass' },
  'pass.showAtGate': { mr: 'हे द्वारावर दाखवा', en: 'Show this at the gate' },
  'pass.rotates': { mr: 'सेकंदांत बदलेल', en: 'changes in' },
  'pass.group': { mr: 'एकूण व्यक्ती', en: 'People' },
  'pass.slot': { mr: 'तुमची वेळ', en: 'Your time' },
  'pass.entry': { mr: 'अंदाजे प्रवेश', en: 'Estimated entry' },
  'pass.reslotted': {
    mr: 'रांग हळू चालत असल्याने तुमची वेळ पुढे ढकलली आहे.',
    en: 'Your time was moved back because the queue is running slow.',
  },
  'pass.offlineOk': {
    mr: 'हा पास नेटवर्कशिवायही चालतो.',
    en: 'This pass works without a network.',
  },

  'crowd.yourZone': { mr: 'तुमच्या भागातील गर्दी', en: 'Crowd where you are' },
  'crowd.level.safe': { mr: 'मोकळे', en: 'Comfortable' },
  'crowd.level.moderate': { mr: 'गर्दी आहे', en: 'Busy' },
  'crowd.level.high': { mr: 'खूप गर्दी', en: 'Very crowded' },
  'crowd.level.critical': { mr: 'जाऊ नका', en: 'Do not enter' },
  'crowd.unknown': { mr: 'माहिती नाही', en: 'Unknown' },
  'crowd.lastUpdated': { mr: 'शेवटची माहिती', en: 'Last updated' },

  'map.facilities': { mr: 'सुविधा', en: 'Facilities' },
  'map.type.toilet': { mr: 'स्वच्छतागृह', en: 'Toilet' },
  'map.type.water': { mr: 'पाणी', en: 'Water' },
  'map.type.medical': { mr: 'वैद्यकीय', en: 'Medical' },
  'map.type.food': { mr: 'अन्नछत्र', en: 'Food' },
  'map.type.rest_zone': { mr: 'विश्रांती', en: 'Rest' },
  'map.type.lost_and_found': { mr: 'हरवले-सापडले', en: 'Lost & found' },
  'map.type.help_desk': { mr: 'मदत कक्ष', en: 'Help desk' },
  'map.type.charging': { mr: 'चार्जिंग', en: 'Charging' },
  'map.outOfService': { mr: 'बंद आहे', en: 'Out of service' },

  'alerts.timings': { mr: 'आरती आणि दर्शन वेळा', en: 'Aarti and darshan times' },
  'alerts.none': { mr: 'सध्या कोणतीही सूचना नाही.', en: 'No advisories right now.' },

  'help.emergency': { mr: 'आपत्कालीन क्रमांक', en: 'Emergency numbers' },
  'help.missingPerson': { mr: 'हरवलेली व्यक्ती नोंदवा', en: 'Report a missing person' },
  'help.language': { mr: 'English', en: 'मराठी' },
  'help.name': { mr: 'नाव', en: 'Name' },
  'help.age': { mr: 'वय', en: 'Age' },
  'help.lastSeen': { mr: 'शेवटचे कुठे पाहिले', en: 'Last seen' },
  'help.contact': { mr: 'तुमचा फोन क्रमांक', en: 'Your phone number' },
  'help.submit': { mr: 'नोंदवा', en: 'Submit' },

  'auth.signIn': { mr: 'फोनवर कोड मागवा', en: 'Get a code on your phone' },
  'auth.phone': { mr: 'फोन क्रमांक', en: 'Phone number' },
  'auth.code': { mr: 'कोड', en: 'Code' },
  'auth.verify': { mr: 'पुढे जा', en: 'Continue' },
  'auth.yourName': { mr: 'तुमचे नाव', en: 'Your name' },

  'offline.banner': { mr: 'ऑफलाइन', en: 'Offline' },
  'offline.pending': { mr: 'पाठवायचे बाकी', en: 'waiting to send' },
  'common.loading': { mr: 'थांबा…', en: 'Loading…' },
  'common.retry': { mr: 'पुन्हा प्रयत्न करा', en: 'Try again' },
  'common.close': { mr: 'बंद करा', en: 'Close' },
} as const

export type StringKey = keyof typeof STRINGS

const LANG_KEY = 'wariverse.pilgrim.lang'

export function currentLang(): Lang {
  return (localStorage.getItem(LANG_KEY) as Lang) ?? 'mr'
}

export function setLang(lang: Lang): void {
  localStorage.setItem(LANG_KEY, lang)
  document.documentElement.lang = lang
}

export function t(key: StringKey, lang: Lang = currentLang()): string {
  return STRINGS[key][lang]
}

/** Pick the right half of a server-supplied bilingual pair. */
export function s(en: string | null | undefined, mr: string | null | undefined, lang: Lang = currentLang()): string {
  return lang === 'mr' ? (mr ?? en ?? '') : (en ?? mr ?? '')
}
