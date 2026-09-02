import AsyncStorage from '@react-native-async-storage/async-storage';
import type { ConversationResponse, CrowdStatus, IVRTurn, Language, ToolWidget } from '@/types/domain';

type MessageRequest = { sessionId: string; language: Language; message: string; latitude?: number | null; longitude?: number | null };

const now = () => new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

function calcDistanceStr(lat1: number, lon1: number, lat2: number, lon2: number): string {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  const d = R * c;
  return d < 1 ? `${Math.round(d * 1000)} m` : `${d.toFixed(1)} km`;
}

const routeWidget: ToolWidget = {
  type: 'route_guidance',
  data: {
    origin: { latitude: 18.517, longitude: 73.856, label: 'Current location' },
    destination: { latitude: 18.519, longitude: 73.851, label: 'Temple entrance' },
    routeCoordinates: [
      { latitude: 18.517, longitude: 73.856 },
      { latitude: 18.516, longitude: 73.854 },
      { latitude: 18.518, longitude: 73.853 },
      { latitude: 18.519, longitude: 73.851 },
    ],
    estimatedTime: '18 min walk',
    distance: '1.2 km',
    avoidAreas: ['Gate 3 — high congestion'],
  },
};

function crowdWidget(
  zoneId = 'mukhdarshan-queue',
  zoneName = 'Mukhdarshan Queue',
  density = 25,
  status: CrowdStatus = 'LOW',
  lat = 17.6782,
  lon = 75.3288
): ToolWidget {
  return {
    type: 'crowd_density',
    data: {
      zoneId,
      zoneName,
      density,
      status,
      latitude: lat,
      longitude: lon,
      updatedAt: 'Just now',
    },
  };
}

function facilityWidget(): ToolWidget {
  return {
    type: 'nearby_facility',
    data: {
      category: 'medical',
      name: 'Wari Medical Center',
      distance: '0.8 km',
      latitude: 18.516,
      longitude: 73.855,
      availability: 'Open · Volunteer staffed',
    },
  };
}

function responseText(language: Language, kind: string, extra?: { category?: string; name?: string; distance?: string }): string {
  if (kind === 'crowd') {
    return language === 'mr' ? 'गेट ३ वर सध्या जास्त गर्दी आहे. शक्य असल्यास थोडा वेळ थांबा.' : language === 'hi' ? 'गेट 3 पर अभी भीड़ ज़्यादा है। संभव हो तो थोड़ी देर रुकें।' : 'Gate 3 is currently busy. If you can, consider waiting a little while.';
  }
  if (kind === 'facility') {
    const cat = extra?.category || 'medical';
    const dist = extra?.distance || '0.3 km';
    const name = extra?.name || 'facility';
    if (cat === 'food') {
      return language === 'mr' ? `तुमच्या जवळ ${name} (${dist} अंतरावर) उपलब्ध आहे.` : language === 'hi' ? `आपके पास ${name} (${dist} दूर) उपलब्ध है।` : `The nearest food & dining option is ${name} (${dist} away).`;
    }
    if (cat === 'accommodation') {
      return language === 'mr' ? `तुमच्या जवळ ${name} (${dist} अंतरावर) राहण्याची सोय आहे.` : language === 'hi' ? `आपके पास ${name} (${dist} दूर) ठहरने की सुविधा है।` : `The nearest stay & accommodation option is ${name} (${dist} away).`;
    }
    if (cat === 'water') {
      return language === 'mr' ? `तुमच्या जवळ ${name} (${dist} अंतरावर) पिण्याचे पाणी उपलब्ध आहे.` : language === 'hi' ? `आपके पास ${name} (${dist} दूर) पीने का पानी उपलब्ध है।` : `The nearest drinking water post is ${name} (${dist} away).`;
    }
    if (cat === 'toilet') {
      return language === 'mr' ? `तुमच्या जवळ ${name} (${dist} अंतरावर) स्वच्छतागृह आहे.` : language === 'hi' ? `आपके पास ${name} (${dist} दूर) शौचालय है।` : `The nearest restroom block is ${name} (${dist} away).`;
    }
    return language === 'mr' ? `तुमच्या जवळ ${name} (${dist} अंतरावर) आहे.` : language === 'hi' ? `आपके पास ${name} (${dist} दूर) है।` : `The nearest medical post is ${name} (${dist} away).`;
  }
  if (kind === 'route') {
    return language === 'mr' ? 'मी तुमच्यासाठी कमी गर्दीचा मार्ग दाखवत आहे.' : language === 'hi' ? 'मैं आपके लिए कम भीड़ वाला रास्ता दिखा रहा हूँ।' : 'I found a quieter route to the temple for you.';
  }
  if (kind === 'forecast') {
    return language === 'mr' ? 'गेट ३ ला भेट देण्यासाठी सकाळी १० पूर्वीचा वेळ चांगला आहे.' : language === 'hi' ? 'गेट 3 जाने के लिए सुबह 10 बजे से पहले का समय बेहतर रहेगा।' : 'Before 10 AM looks like the best time to visit Gate 3.';
  }
  if (kind === 'temple') {
    return language === 'mr' ? 'मंदिराची आजची माहिती येथे आहे.' : language === 'hi' ? 'मंदिर की आज की जानकारी यहाँ है।' : 'Here is the latest temple information.';
  }
  if (kind === 'lost') {
    return language === 'mr' ? 'तुमची हरवलेली व्यक्तीची विनंती स्वयंसेवक टीमकडे पाठवली आहे.' : language === 'hi' ? 'आपके खोए हुए व्यक्ति की सूचना स्वयंसेवक टीम को भेज दी गई है।' : 'Your lost person report has been shared with the volunteer team.';
  }
  if (kind === 'escalation') {
    return language === 'mr' ? 'या प्रश्नासाठी स्वयंसेवक तुमची अधिक चांगली मदत करू शकतात.' : language === 'hi' ? 'इस सवाल में स्वयंसेवक आपकी बेहतर मदद कर सकते हैं।' : 'A volunteer can help you more directly with this request.';
  }
  return language === 'mr' ? 'मी तुमच्या वारीच्या प्रवासात मदत करण्यासाठी येथे आहे.' : language === 'hi' ? 'मैं आपकी वारी यात्रा में मदद करने के लिए यहाँ हूँ।' : 'I’m here to help with your Wari journey.';
}

export const mockConversationApi = {
  async sendMessage(request: MessageRequest): Promise<ConversationResponse> {
    await new Promise((resolve) => setTimeout(resolve, 650));
    const query = request.message.toLowerCase();
    let kind = 'normal';
    let widgets: ToolWidget[] = [];
    let facilityExtra: { category?: string; name?: string; distance?: string } | undefined = undefined;

    if (query.includes('crowd') || query.includes('गर्दी') || query.includes('भीड़') || query.includes('gate') || query.includes('gate-')) {
      kind = 'crowd';
      widgets = [crowdWidget()];
    } else if (
      query.includes('hospital') || query.includes('doctor') || query.includes('medical') || query.includes('facility') ||
      query.includes('toilet') || query.includes('water') || query.includes('food') || query.includes('restaurant') ||
      query.includes('police') || query.includes('station') || query.includes('rest') || query.includes('hotel') ||
      query.includes('stay') || query.includes('lodging') || query.includes('मेडिकल') || query.includes('अन्नछत्र') ||
      query.includes('जेवण') || query.includes('पाणी') || query.includes('शौचालय') || query.includes('सुविधा') ||
      query.includes('हॉस्पिटल') || query.includes('पोलीस')
    ) {
      kind = 'facility';
      let category: 'food' | 'accommodation' | 'water' | 'medical' | 'toilet' | 'rest' | 'police' = 'medical';
      let defaultName = 'Sub-District Govt Hospital Pandharpur';
      let defaultLat = 17.6738;
      let defaultLng = 75.3312;
      let defaultPhone = '+912166222333';

      if (query.includes('food') || query.includes('restaurant') || query.includes('hotel') || query.includes('dhaba') || query.includes('जेवण') || query.includes('भोजन') || query.includes('अन्नछत्र') || query.includes('खाना') || query.includes('breakfast')) {
        category = 'food';
        defaultName = 'Shree Vitthal Free Food Annachatra';
        defaultLat = 17.6780;
        defaultLng = 75.3275;
        defaultPhone = '+919822012345';
      } else if (query.includes('stay') || query.includes('accommodation') || query.includes('lodging') || query.includes('room') || query.includes('मुक्काम') || query.includes('निवास')) {
        category = 'accommodation';
        defaultName = 'Shri Vitthal Rukmini Bhakta Niwas Residence';
        defaultLat = 17.6795;
        defaultLng = 75.3315;
        defaultPhone = '1800-233-1000';
      } else if (query.includes('water') || query.includes('paani') || query.includes('पाणी') || query.includes('पानी')) {
        category = 'water';
        defaultName = 'Bhima Ghat Pure Drinking Water Station';
        defaultLat = 17.6808;
        defaultLng = 75.3265;
        defaultPhone = '1800-233-1000';
      } else if (query.includes('toilet') || query.includes('washroom') || query.includes('restroom') || query.includes('शौचालय') || query.includes('स्वच्छतागृह')) {
        category = 'toilet';
        defaultName = 'Pandharpur Sanitation & Washroom Block #4';
        defaultLat = 17.6765;
        defaultLng = 75.3290;
        defaultPhone = '1800-233-1000';
      } else if (query.includes('police') || query.includes('पोलीस') || query.includes('पुलिस') || query.includes('chowky') || query.includes('cop') || query.includes('thana')) {
        category = 'police';
        defaultName = 'Pandharpur City Police Station & Gate 1 Outpost';
        defaultLat = 17.6755;
        defaultLng = 75.3298;
        defaultPhone = '112';
      }

      const userLat = request.latitude ?? 17.6778;
      const userLng = request.longitude ?? 75.3283;
      const token = process.env.EXPO_PUBLIC_MAPBOX_TOKEN || '';

      const facilityWidgets: ToolWidget[] = [];

      // 1. Check for Volunteer Community Charity Sevas from AsyncStorage / Local Store
      try {
        const storedSevas = await AsyncStorage.getItem('wariverse-my-sevas');
        if (storedSevas) {
          const parsedSevas = JSON.parse(storedSevas) as any[];
          const matchingSevas = parsedSevas.filter((s) => s.category === category && s.isActive !== false);
          for (const seva of matchingSevas) {
            facilityWidgets.push({
              type: 'nearby_facility',
              data: {
                category,
                name: seva.title,
                providerName: seva.providerName || 'Volunteer Seva Trust',
                distance: calcDistanceStr(userLat, userLng, seva.latitude, seva.longitude),
                latitude: seva.latitude,
                longitude: seva.longitude,
                phone: seva.contactPhone || defaultPhone,
                availability: 'Open · Free Community Charity Seva (Available)',
                isCharity: true,
                isSeva: true,
                isLocked: Boolean(seva.isLocked),
                lockedByName: seva.lockedByName,
              },
            });
          }
        }
      } catch {
        // Continue to public POIs
      }

      // 2. Fetch live Mapbox Places around live GPS coordinates
      let publicName = defaultName;
      let publicLat = defaultLat;
      let publicLng = defaultLng;
      let publicPhone: string | undefined = defaultPhone;
      let publicDistStr = calcDistanceStr(userLat, userLng, defaultLat, defaultLng);

      if (token) {
        try {
          const mapboxCategoryMap: Record<string, string> = {
            medical: 'hospital',
            police: 'police',
            food: 'restaurant',
            accommodation: 'hotel',
            water: 'drinking_water',
            toilet: 'restroom',
            rest: 'park',
          };
          const mbCat = mapboxCategoryMap[category] || 'restaurant';
          const url = `https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(mbCat)}.json?proximity=${userLng},${userLat}&access_token=${token}&country=IN&limit=5`;
          const res = await fetch(url);
          const data = await res.json();
          if (data && data.features && data.features.length > 0) {
            const first = data.features[0];
            const rawText = (first.text || '').trim();
            const placeName = (first.place_name || '').trim();

            if (!rawText || rawText.length < 5 || ['hotel', 'hospital', 'restaurant', 'police', 'doctor'].includes(rawText.toLowerCase())) {
              const parts = placeName.split(',').map((s: string) => s.trim());
              publicName = parts.length > 1 ? `${parts[0]}, ${parts[1]}` : `${parts[0]} Pandharpur`;
            } else {
              publicName = rawText;
            }

            publicLng = first.center[0];
            publicLat = first.center[1];
            if (first.properties && (first.properties.tel || first.properties.phone)) {
              publicPhone = first.properties.tel || first.properties.phone;
            }
            publicDistStr = calcDistanceStr(userLat, userLng, publicLat, publicLng);
          }
        } catch {
          // Graceful fallback to default public facility
        }
      }

      // Append official/public facility widget
      facilityWidgets.push({
        type: 'nearby_facility',
        data: {
          category,
          name: publicName,
          distance: publicDistStr,
          latitude: publicLat,
          longitude: publicLng,
          phone: publicPhone,
          availability: 'Open · 24x7 Public Service',
          isCharity: false,
        },
      });

      const topData = facilityWidgets[0]?.data as any;
      facilityExtra = {
        category,
        name: topData?.name,
        distance: topData?.distance,
      };
      widgets = facilityWidgets;
    } else if (query.includes('palkhi') || query.includes('पालखी') || query.includes('पालकी') || query.includes('palki') || query.includes('track palkhi')) {
      kind = 'palkhi';
      const isTukaram = query.includes('tukaram') || query.includes('तुकाराम');
      widgets = [{
        type: 'palkhi_location',
        data: {
          latitude: isTukaram ? 17.6845 : (request.latitude ?? 17.6792),
          longitude: isTukaram ? 75.3210 : (request.longitude ?? 75.3278),
          palkhiName: isTukaram ? 'Shree Sant Tukaram Maharaj Palkhi (Dehu)' : 'Shree Sant Dnyaneshwar Maharaj Palkhi (Alandi)',
          currentPlace: isTukaram ? 'Akluj / Pirachi Kuroli Sthal' : 'Wakhari Ringan Ground (Solapur Limit)',
          nextPlace: 'Pandharpur Vitthal Rukmini Mandir Precinct',
          etaMinutes: 20,
          chiefName: isTukaram ? 'Shri. Bhavarth Dekhane' : 'Shri. Adv. Rajendra Umap',
          chiefPhone: isTukaram ? '9168359955' : '9822069465',
          nodalOfficer: isTukaram ? 'PSI Sachin Mali (Mohol P.S.)' : 'API Zalte (Barshi City P.S.)',
          nodalPhone: isTukaram ? '8408989444' : '8888852097',
          policePortalUrl: 'https://ashadhi.solapurpolice.gov.in/',
          updatedAt: 'Live from Solapur Police GPS Portal',
          isSimulated: false,
        },
      }];
    } else if (
      query.includes('route') || query.includes('way') || query.includes('path') || query.includes('direction') ||
      query.includes('reach') || query.includes('show route') || query.includes('temple') || query.includes('रस्ता') ||
      query.includes('रास्ता') || query.includes('मंदिर') || query.includes('मार्ग')
    ) {
      kind = 'route';
      widgets = [routeWidget];
    } else if (query.includes('when') || query.includes('forecast') || query.includes('avoid') || query.includes('कब') || query.includes('वेळ')) {
      kind = 'forecast';
      widgets = [{
        type: 'congestion_forecast',
        data: {
          zoneId: 'gate-3',
          zoneName: 'Gate 3',
          points: [{ time: '8 AM', value: 38 }, { time: '10 AM', value: 62 }, { time: '12 PM', value: 89 }, { time: '2 PM', value: 72 }, { time: '4 PM', value: 54 }],
          recommendation: responseText(request.language, 'forecast'),
          updatedAt: 'Updated just now',
        },
      }];
    } else if (query.includes('temple') || query.includes('दर्शन') || query.includes('मंदिर')) {
      kind = 'temple';
      widgets = [{
        type: 'temple_info',
        data: {
          title: 'Temple information',
          timings: '6:00 AM – 11:00 PM',
          rituals: ['Morning aarti · 6:30 AM', 'Evening aarti · 7:00 PM'],
          description: 'Please follow volunteer guidance and keep walkways clear.',
        },
      }];
    } else if (query.includes('lost') || query.includes('हरव') || query.includes('खो') || query.includes('missing')) {
      kind = 'lost';
      widgets = [{
        type: 'lost_and_found',
        data: { incidentType: 'PERSON', status: 'Searching', referenceId: 'WF-2026-00124', nextAction: 'Stay near the last known location and keep your phone reachable.' },
      }];
    } else if (query.includes('volunteer') || query.includes('human') || query.includes('मदत') || query.includes('help')) {
      kind = 'escalation';
      widgets = [{
        type: 'human_escalation',
        data: { status: 'Volunteer available', message: 'I can connect you with a Wari volunteer for personal assistance.', contactAvailable: true },
      }];
    } else if (query.includes('emergency') || query.includes('sos') || query.includes('आपत्कालीन') || query.includes('आपात')) {
      widgets = [{ type: 'sos', data: { status: 'CONFIRMATION_REQUIRED', message: 'Emergency assistance will be requested and your current location may be shared with the control room.' } }];
    }

    return {
      sessionId: request.sessionId,
      messageId: `assistant-${Date.now()}`,
      language: request.language,
      responseText: responseText(request.language, kind, facilityExtra),
      widgets,
    };
  },
  async confirmSOS(language: Language): Promise<ConversationResponse> {
    await new Promise((resolve) => setTimeout(resolve, 900));
    return {
      sessionId: 'wariverse-session',
      messageId: `sos-${Date.now()}`,
      language,
      responseText: language === 'mr' ? 'मदतीची विनंती पाठवली आहे.' : language === 'hi' ? 'मदद की request भेज दी गई है।' : 'Help has been requested.',
      widgets: [{ type: 'sos', data: { status: 'ACTIVATED', message: language === 'mr' ? 'मदतीची विनंती पाठवली आहे.' : language === 'hi' ? 'मदद की request भेज दी गई है।' : 'Help has been requested.', controlRoomStatus: 'Connected', timestamp: now() } }],
    };
  },
};

export const mockIvrApi = {
  async start(input: { sessionId: string; language?: Language; latitude?: number | null; longitude?: number | null }): Promise<IVRTurn> {
    const lang = input.language || 'en';
    const prompts = {
      mr: 'वारीव्हर्स डिजिटल हेल्पलाइनमध्ये आपले स्वागत आहे. गर्दीच्या माहितीसाठी १ दाबा, दर्शन वेळेसाठी २ दाबा, जवळील मोफत सेवेसाठी ३ दाबा, किंवा आपत्कालीन मदतीसाठी ४ दाबा. आपण बोलण्यासाठी बटण दाबून धरू शकता.',
      hi: 'वारीव्हर्स डिजिटल हेल्पलाइन में आपका स्वागत है। भीड़ की स्थिति के लिए 1 दबाएं, दर्शन समय के लिए 2 दबाएं, पास की सेवाओं के लिए 3 दबाएं, या आपातकालीन सहायता के लिए 4 दबाएं। आप बोलने के लिए बटन दबाकर भी रख सकते हैं।',
      en: 'Welcome to WariVerse Helpline. Press 1 for Live Crowd Density, 2 for Temple Schedule & Timings, 3 for Nearby Seva & Facilities, 4 for Emergency SOS, or hold the button to speak.',
    };
    return {
      sessionId: input.sessionId,
      state: 'menu',
      language: lang,
      prompt: prompts[lang] || prompts.en,
      audioBase64: null,
      mediaType: 'audio/mpeg',
      options: [
        { key: '1', label: lang === 'mr' ? '१ · गर्दीची माहिती' : lang === 'hi' ? '1 · भीड़ की स्थिति' : '1 · Live Crowd Status' },
        { key: '2', label: lang === 'mr' ? '२ · दर्शन व आरती वेळ' : lang === 'hi' ? '2 · दर्शन समय' : '2 · Temple Schedule' },
        { key: '3', label: lang === 'mr' ? '३ · जवळील सुविधा' : lang === 'hi' ? '3 · पास की सुविधाएं' : '3 · Nearby Seva & Facilities' },
        { key: '4', label: lang === 'mr' ? '४ · आपत्कालीन SOS' : lang === 'hi' ? '4 · आपातकालीन SOS' : '4 · Emergency SOS' },
      ],
      endsSession: false,
    };
  },

  async press(input: { sessionId: string; key: string; latitude?: number | null; longitude?: number | null; turnId?: string }): Promise<IVRTurn> {
    const lang: Language = 'en';
    if (input.key === '1') {
      return {
        sessionId: input.sessionId,
        state: 'menu',
        language: lang,
        prompt: 'Live Crowd Status: Mukhdarshan Queue is LOW (25% capacity, 15-20 min wait). Gate 2 North is MODERATE (52%). Padsparsha Queue is HIGH (78% capacity, 2-3 hrs wait). Gate 3 South is HEAVY (82%). Recommended fast route: Mukhdarshan Queue.',
        audioBase64: null,
        mediaType: 'audio/mpeg',
        options: [
          { key: '1', label: '1 · Refresh Crowd' },
          { key: '2', label: '2 · Temple Schedule' },
          { key: '3', label: '3 · Nearby Seva' },
          { key: '0', label: '0 · Back to Main Menu' },
        ],
        widgets: [
          crowdWidget('mukhdarshan-queue', 'Mukhdarshan Queue (15-20 min)', 25, 'LOW', 17.6782, 75.3288),
          crowdWidget('darshan-mandap-token', 'Sant Dnyaneshwar Darshan Mandap', 45, 'MODERATE', 17.6798, 75.3292),
          crowdWidget('padsparsha-queue', 'Padsparsha Touch Darshan Queue', 78, 'HIGH', 17.6773, 75.3312),
          crowdWidget('gate-3', 'Gate 3 (South Entrance)', 82, 'HIGH', 17.6779, 75.3301),
        ],
        endsSession: false,
      };
    }
    if (input.key === '2') {
      return {
        sessionId: input.sessionId,
        state: 'menu',
        language: lang,
        prompt: 'Shri Vitthal Mandir Pandharpur is open 24 Hours for Ashadhi Ekadashi 2026. Mukhdarshan queue is 15-20 min. Padsparsha queue is 2-3 hrs standard.',
        audioBase64: null,
        mediaType: 'audio/mpeg',
        options: [
          { key: '1', label: '1 · Crowd Status' },
          { key: '3', label: '3 · Nearby Facilities' },
          { key: '0', label: '0 · Back to Main Menu' },
        ],
        widgets: [{
          type: 'temple_info',
          data: {
            title: 'Shri Vitthal Rukmini Mandir 2026 Schedule',
            timings: 'Open 24 Hours (Ashadhi Ekadashi Special)',
            rituals: ['Kakad Aarti · 4:30 AM', 'Mahapuja · 12:00 AM Midnight', 'Shej Aarti · 11:30 PM'],
            events: ['Mukhdarshan Queue (15-20 min)', 'Padsparsha Sanctum Queue (2-3 hrs)'],
            description: 'North entrance token passes available at Shri Sant Dnyaneshwar Darshan Mandap.',
          },
        }],
        endsSession: false,
      };
    }
    if (input.key === '3') {
      return {
        sessionId: input.sessionId,
        state: 'menu',
        language: lang,
        prompt: 'The nearest facility is Wari Medical Center (0.8 km away). Water stations and Annachhatra food distribution are available along the palkhi route.',
        audioBase64: null,
        mediaType: 'audio/mpeg',
        options: [
          { key: '1', label: '1 · Live Crowd Status' },
          { key: '2', label: '2 · Temple Schedule' },
          { key: '0', label: '0 · Back to Main Menu' },
        ],
        widgets: [facilityWidget()],
        endsSession: false,
      };
    }
    if (input.key === '4') {
      return {
        sessionId: input.sessionId,
        state: 'sos_confirm',
        language: lang,
        prompt: 'Emergency SOS requested. Wari Control Room and Solapur Rural Police have been notified. Stay calm near your location.',
        audioBase64: null,
        mediaType: 'audio/mpeg',
        options: [
          { key: '0', label: '0 · Main Menu' },
        ],
        widgets: [{
          type: 'sos',
          data: {
            status: 'ACTIVATED',
            message: 'Emergency SOS activated. Control room helpline 1800-233-1000 notified.',
            controlRoomStatus: 'Connected · Dispatch Active',
            timestamp: now(),
          },
        }],
        endsSession: false,
      };
    }
    return mockIvrApi.start({ sessionId: input.sessionId, language: lang });
  },

  async speak(input: { sessionId: string; audio: Blob; fileName?: string; turnId?: string }): Promise<IVRTurn> {
    return {
      sessionId: input.sessionId,
      state: 'speech',
      language: 'en',
      prompt: 'I understand your query. The nearest water point and medical assistance is located 500m ahead near Gate 2.',
      audioBase64: null,
      mediaType: 'audio/mpeg',
      options: [
        { key: '0', label: '0 · Back to Menu' },
      ],
      widgets: [facilityWidget()],
      endsSession: false,
    };
  },
};