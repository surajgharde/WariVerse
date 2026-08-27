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

  'her.title': { mr: 'वारीचा वारसा', en: 'Wari heritage' },
  'her.empty': { mr: 'अजून नोंदी नाहीत.', en: 'Nothing in the archive yet.' },
  'her.contribute': { mr: 'तुमची आठवण नोंदवा', en: 'Add something you know' },
  'her.submitted': {
    mr: 'धन्यवाद. तपासणीनंतर ते संग्रहात दिसेल.',
    en: 'Thank you. It will appear once it has been checked.',
  },
  'her.what': { mr: 'काय आहे?', en: 'What is it?' },
  'her.titleField': { mr: 'शीर्षक', en: 'Title' },
  'her.bodyField': { mr: 'मजकूर', en: 'The text' },
  'her.credit': { mr: 'कोणाकडून मिळाले?', en: 'Who did this come from?' },
  'her.creditWhy': {
    mr: 'ज्यांच्याकडून हे ऐकले त्यांचे नाव द्या. तेच नाव संग्रहात दिसेल.',
    en: 'Name whoever you heard it from. That is the name the archive prints.',
  },
  'her.kind.abhang': { mr: 'अभंग', en: 'Abhang' },
  'her.kind.ovi': { mr: 'ओवी', en: 'Ovi' },
  'her.kind.story': { mr: 'आठवण', en: 'A memory' },
  'her.kind.ritual': { mr: 'प्रथा', en: 'A custom' },
  'her.kind.place_lore': { mr: 'या जागेची कथा', en: 'Story of a place' },

  'acc.title': { mr: 'मदतीची गरज', en: 'Help I need' },
  'acc.needHelpNow': { mr: 'आत्ता मदत हवी', en: 'I need help now' },
  'acc.declare': { mr: 'माझी गरज नोंदवा', en: 'Set what I need' },
  'acc.saved': { mr: 'नोंदवले. पुन्हा विचारले जाणार नाही.', en: 'Saved. You will not be asked again.' },
  'acc.requested': {
    mr: 'मदत मागवली. स्वयंसेवक येत आहेत.',
    en: 'Help requested. A volunteer is coming.',
  },
  'acc.requestQueued': {
    mr: 'नेटवर्क आल्यावर मदत मागवली जाईल.',
    en: 'Help will be requested when the network returns.',
  },
  'acc.largeText': { mr: 'मोठी अक्षरे', en: 'Larger text' },
  'acc.highContrast': { mr: 'ठळक रंग', en: 'Stronger contrast' },
  'acc.priority': {
    mr: 'तुम्हाला राखीव दर्शन वेळ मिळू शकते.',
    en: 'You can be given a reserved darshan slot.',
  },
  'acc.stepFreeOnly': { mr: 'फक्त पायऱ्या नसलेल्या सुविधा', en: 'Step-free facilities only' },
  'acc.notSurveyed': { mr: 'तपासलेले नाही', en: 'not checked' },
  'acc.need.wheelchair': { mr: 'चाकाची खुर्ची', en: 'Wheelchair' },
  'acc.need.walking_support': { mr: 'चालण्यास आधार', en: 'Help walking' },
  'acc.need.stretcher': { mr: 'स्ट्रेचर', en: 'Stretcher' },
  'acc.need.vision': { mr: 'दिसण्यात अडचण', en: 'Trouble seeing' },
  'acc.need.hearing': { mr: 'ऐकण्यात अडचण', en: 'Trouble hearing' },
  'acc.need.speech': { mr: 'बोलण्यात अडचण', en: 'Trouble speaking' },
  'acc.need.cognitive': { mr: 'सोपे मार्गदर्शन हवे', en: 'Simple instructions' },
  'acc.need.companion_required': { mr: 'सोबती सोबत हवा', en: 'Must stay with companion' },
  'acc.need.step_free_route': { mr: 'पायऱ्या नसलेला मार्ग', en: 'Step-free route' },
  'acc.need.oxygen': { mr: 'ऑक्सिजन', en: 'Oxygen' },

  'lf.title': { mr: 'हरवले-सापडले', en: 'Lost & found' },
  'lf.reportLost': { mr: 'हरवलेली वस्तू नोंदवा', en: 'Report something lost' },
  'lf.searchFound': { mr: 'सापडलेल्या वस्तू पहा', en: 'See what has been handed in' },
  'lf.what': { mr: 'काय हरवले?', en: 'What was lost?' },
  'lf.describe': { mr: 'थोडक्यात वर्णन', en: 'Describe it briefly' },
  'lf.colour': { mr: 'रंग', en: 'Colour' },
  'lf.mark': { mr: 'फक्त तुम्हाला माहीत असलेली खूण', en: 'A mark only you would know' },
  'lf.markWhy': {
    mr: 'ही खूण कोणालाही दाखवली जात नाही. कक्षात वस्तू परत मिळवण्यासाठी हीच तुमची ओळख.',
    en: 'This is never shown to anyone. It is how the desk knows the item is yours.',
  },
  'lf.queued': {
    mr: 'नोंद घेतली. नेटवर्क आल्यावर पाठवली जाईल.',
    en: 'Saved. It will be sent when the network returns.',
  },
  'lf.noneFound': {
    mr: 'सध्या जमा झालेली अशी वस्तू नाही.',
    en: 'Nothing like that has been handed in yet.',
  },
  'lf.atDesk': { mr: 'कक्ष', en: 'Desk' },
  'lf.askAtDesk': {
    mr: 'तुमची वाटणारी वस्तू दिसल्यास कक्षात जाऊन खूण सांगा.',
    en: 'If one looks like yours, go to the desk and describe your mark.',
  },
  'lf.cat.bag': { mr: 'पिशवी', en: 'Bag' },
  'lf.cat.phone': { mr: 'फोन', en: 'Phone' },
  'lf.cat.documents': { mr: 'कागदपत्रे', en: 'Documents' },
  'lf.cat.money_purse': { mr: 'पैसे / पाकीट', en: 'Money / purse' },
  'lf.cat.jewellery': { mr: 'दागिने', en: 'Jewellery' },
  'lf.cat.footwear': { mr: 'चप्पल / बूट', en: 'Footwear' },
  'lf.cat.clothing': { mr: 'कपडे', en: 'Clothing' },
  'lf.cat.medicine': { mr: 'औषध', en: 'Medicine' },
  'lf.cat.walking_aid': { mr: 'काठी / चाकाची खुर्ची', en: 'Walking stick / wheelchair' },
  'lf.cat.religious_item': { mr: 'पूजेची वस्तू', en: 'Religious item' },
  'lf.cat.other': { mr: 'इतर', en: 'Other' },

  'auth.signIn': { mr: 'सुरू करा', en: 'Continue' },
  'auth.yourName': { mr: 'तुमचे नाव', en: 'Your name' },
  'auth.nameHint': {
    mr: 'फक्त नाव लिहा. कोड किंवा पासवर्ड लागत नाही.',
    en: 'Just your name. No code, no password.',
  },
  'auth.nameNeeded': { mr: 'कृपया तुमचे नाव लिहा.', en: 'Please enter your name.' },

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
