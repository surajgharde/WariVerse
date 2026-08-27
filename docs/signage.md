# Signage and consent copy

Section 12 asks for two things to be generated: physical CCTV signage in
Marathi, Hindi and English "as required for lawful monitoring", and a
Marathi-first consent screen before any location or notification permission.
Both are here.

> ⚠️ **Not yet reviewed.** This copy was drafted alongside the code. Before it
> is printed or shipped it needs a native Marathi speaker and the trust's legal
> adviser. Section 16's validation checklist requires the former for the whole
> UI; signage is the part that ends up on a board in a public place, where a
> machine-translation artefact is both embarrassing and legally weak.

**Marathi is the operational language.** It is first on every sign and every
screen, and the English is the translation — not the other way round. The queue
in Pandharpur runs in Marathi.

---

## 1. CCTV zone signage

Posted at every entrance to a monitored zone, and at each gate. Legibility rule:
the first line readable at 10 metres, the body at 2 metres.

### Board A — standard monitored zone

**मराठी**

> **गर्दी सुरक्षेसाठी कॅमेरा निरीक्षण**
>
> या भागात गर्दीचे प्रमाण मोजण्यासाठी कॅमेरे लावले आहेत.
>
> • फक्त किती लोक आहेत आणि गर्दी कशी हलते आहे एवढेच मोजले जाते.
> • **चेहरा ओळखला जात नाही. कोणत्याही व्यक्तीची ओळख पटवली जात नाही.**
> • चित्रीकरण साठवले जात नाही.
> • ही माहिती चेंगराचेंगरी टाळण्यासाठी वापरली जाते.
>
> माहिती नियंत्रक: श्री विठ्ठल-रुक्मिणी मंदिर समिती, पंढरपूर
> तक्रार / चौकशी: ____________________

**हिंदी**

> **भीड़ सुरक्षा हेतु कैमरा निगरानी**
>
> इस क्षेत्र में भीड़ की मात्रा मापने के लिए कैमरे लगाए गए हैं।
>
> • केवल यह मापा जाता है कि कितने लोग हैं और भीड़ कैसे चल रही है।
> • **चेहरा नहीं पहचाना जाता। किसी व्यक्ति की पहचान नहीं की जाती।**
> • रिकॉर्डिंग संग्रहीत नहीं की जाती।
> • यह जानकारी भगदड़ रोकने के लिए उपयोग की जाती है।
>
> डेटा नियंत्रक: श्री विठ्ठल-रुक्मिणी मंदिर समिति, पंढरपुर
> शिकायत / पूछताछ: ____________________

**English**

> **Camera monitoring for crowd safety**
>
> Cameras in this area measure how crowded it is.
>
> • Only the number of people and how the crowd is moving is measured.
> • **No face recognition. No individual is identified.**
> • Footage is not stored.
> • This information is used to prevent crowd crush.
>
> Data Fiduciary: Shri Vitthal-Rukmini Temple Committee, Pandharpur
> Grievances / enquiries: ____________________

### Board B — restricted gate (tripwire active)

Posted **only** at gates with an active tripwire. The wording differs because
the claim differs: here a short clip **is** recorded, and a sign that said
"footage is not stored" would be false.

**मराठी**

> **प्रतिबंधित द्वार — नोंद ठेवली जाते**
>
> हे द्वार अधिकृत व्यक्तींसाठी आहे.
>
> • परवानगीशिवाय कोणी आत गेल्यास **१० सेकंदांची चित्रफीत नोंदवली जाते**.
> • ही नोंद ९० दिवसांनी आपोआप नष्ट होते.
> • **चेहरा ओळखला जात नाही.** नियम मोडला गेला एवढीच नोंद होते, कोणी मोडला ही नाही.
> • ही चित्रफीत पाहण्याची प्रत्येक वेळ नोंदवली जाते.
>
> माहिती नियंत्रक: श्री विठ्ठल-रुक्मिणी मंदिर समिती, पंढरपूर
> तक्रार / चौकशी: ____________________

**हिंदी**

> **प्रतिबंधित द्वार — रिकॉर्ड रखा जाता है**
>
> यह द्वार अधिकृत व्यक्तियों के लिए है।
>
> • बिना अनुमति प्रवेश पर **१० सेकंड की क्लिप रिकॉर्ड की जाती है**।
> • यह रिकॉर्ड ९० दिन बाद स्वतः नष्ट हो जाता है।
> • **चेहरा नहीं पहचाना जाता।** केवल यह दर्ज होता है कि नियम टूटा, यह नहीं कि किसने तोड़ा।
> • इस क्लिप को देखे जाने की हर बार नोंद रखी जाती है।
>
> डेटा नियंत्रक: श्री विठ्ठल-रुक्मिणी मंदिर समिति, पंढरपुर
> शिकायत / पूछताछ: ____________________

**English**

> **Restricted gate — entries are recorded**
>
> This gate is for authorised persons.
>
> • Unauthorised entry records a **10-second video clip**.
> • The clip is automatically deleted after 90 days.
> • **No face recognition.** It records that a rule was broken, not who broke it.
> • Every viewing of a clip is logged.
>
> Data Fiduciary: Shri Vitthal-Rukmini Temple Committee, Pandharpur
> Grievances / enquiries: ____________________

---

## 2. In-app consent screens

Shown **before** the browser permission prompt, never after. A native permission
dialog with no explanation in front of it is a dark pattern — the person taps
"allow" without knowing what they agreed to, which is precisely the consent the
DPDP Act does not recognise.

### 2.1 Location

**मराठी** (primary)

> **तुमचे ठिकाण वापरण्याची परवानगी**
>
> जवळचे पाणी, स्वच्छतागृह किंवा वैद्यकीय शिबिर दाखवण्यासाठी आम्हाला तुमचे ठिकाण
> लागते.
>
> • तुमचे ठिकाण **फक्त तुमच्या फोनवर** राहते.
> • ते आमच्या सर्व्हरवर साठवले जात नाही.
> • तुमचा प्रवास कुठून कुठे झाला याची नोंद ठेवली जात नाही.
> • परवानगी नाकारली तरी अ‍ॅप पूर्ण चालते — फक्त यादी अंतरानुसार लागणार नाही.
>
> [ परवानगी द्या ]   [ नको ]

**English**

> **Allow location**
>
> We use your location to show the nearest water point, toilet or medical camp.
>
> • Your location stays **on your phone only**.
> • It is not stored on our servers.
> • We do not keep a record of where you have been.
> • The app works fully without it — the list just won't be sorted by distance.
>
> [ Allow ]   [ Not now ]

### 2.2 Notifications

**मराठी** (primary)

> **सूचना पाठवण्याची परवानगी**
>
> तुमच्या पासची वेळ बदलली किंवा सुरक्षेची सूचना असेल तर आम्ही कळवू.
>
> • फक्त तुमच्या पासबद्दल आणि सुरक्षेबद्दल सूचना.
> • जाहिराती नाहीत.
> • कधीही बंद करता येते.
>
> [ परवानगी द्या ]   [ नको ]

**English**

> **Allow notifications**
>
> We will tell you if your pass time changes or there is a safety alert.
>
> • Only about your pass and about safety.
> • No advertising.
> • You can turn this off at any time.
>
> [ Allow ]   [ Not now ]

### 2.3 Designated Dindi volunteer — position reporting

The strongest consent in the product, because it is the only one where a
**named individual** carries a device that reports position continuously for
eighteen days. §4 of the DPIA marks written consent here as blocking.

**मराठी** (primary)

> **दिंडीचे ठिकाण पाठवण्याची परवानगी**
>
> तुम्ही या दिंडीसाठी नोंदणीकृत फोन घेऊन चालणार आहात.
>
> • हा फोन **दिंडीचे ठिकाण** ठराविक वेळाने पाठवेल.
> • हे गटाचे ठिकाण आहे, तुमचे वैयक्तिक ठिकाण म्हणून नोंदवले जात नाही.
> • मुक्कामाच्या गावांना पाणी, जेवण आणि वैद्यकीय व्यवस्था वेळेवर तयार ठेवता यावी
>   यासाठीच याचा वापर होतो.
> • बॅटरी वाचवण्यासाठी नोंदी आपोआप कमी वेळा पाठवल्या जातात.
> • **तुम्ही कधीही ही जबाबदारी दुसऱ्याला देऊ शकता किंवा थांबवू शकता.**
> • वारी संपल्यानंतर ही माहिती हंगामाच्या शेवटी नष्ट केली जाते.
>
> दिंडी: ____________________   स्वयंसेवक: ____________________
> सही: ____________________   दिनांक: ____________

**English**

> **Consent to report your Dindi's position**
>
> You will carry this Dindi's registered phone during the walk.
>
> • This phone will send **the Dindi's position** at intervals.
> • This is the group's position. It is not recorded as your personal location.
> • It is used only so halt towns can have water, food and medical care ready in
>   time.
> • Reporting automatically slows down to save your battery.
> • **You may hand this role to someone else, or stop, at any time.**
> • The data is deleted at the end of the season.
>
> Dindi: ____________________   Volunteer: ____________________
> Signature: ____________________   Date: ____________

---

## 3. Printing notes

- Boards A and B are **not interchangeable**. Board A says footage is not
  stored; posting it at a tripwire gate would be a false statement on a legal
  notice.
- Leave the grievance contact blank in the source file and fill it at print
  time — it must be a real, monitored channel (DPIA §7, blocking).
- Recommended minimum: A3 at every zone entrance, A4 at each gate leaf.
- The volunteer consent form (§2.3) is signed on **paper** and retained by the
  trust. The app screen is a copy of the same words, not a replacement for the
  signature.
