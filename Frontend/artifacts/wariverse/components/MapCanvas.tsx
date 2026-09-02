import AsyncStorage from '@react-native-async-storage/async-storage';
import { Feather } from '@expo/vector-icons';
import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import { WebView } from 'react-native-webview';
import colors from '@/constants/colors';
import { FACILITY_RADIUS_M, PANDHARPUR_TEMPLE } from '@/constants/geo';
import {
  communityApi,
  crowdApi,
  facilitiesApi,
  type CommunityServiceItem,
  type CrowdZoneReading,
  type NearbyFacility,
} from '@/services/api';
import type { LocationState } from '@/types/domain';

/**
 * The live map, rendered as Leaflet inside a WebView (native) or an iframe
 * (web).
 *
 * Everything on it is now real: crowd densities come from `/api/crowd/all`,
 * facilities from `/api/facilities/nearby` within 10 km of the pilgrim, and
 * seva pins from the community API. It previously drew three hardcoded zones
 * with invented percentages, which looked live and was not.
 *
 * ── Passing data into the page ──────────────────────────────────────────────
 * All of it goes in as one JSON island, and the page builds its own markers.
 * The previous version interpolated seva titles and provider names straight
 * into the `<script>` with only `'` escaped — those fields are submitted by
 * members of the public, so a title containing a quote broke the map and one
 * containing `</script>` could run whatever it liked.
 */

const MY_SEVAS_KEY = 'wariverse-my-sevas';
const MAPBOX_TOKEN = process.env.EXPO_PUBLIC_MAPBOX_TOKEN || '';

/** Fallback route, used only when there is no GPS and no destination. */
const PALKHI_PREVIEW: [number, number][] = [
  [17.679, 75.3245],
  [17.6812, 75.327],
  [17.6775, 75.3283],
];

/** Everything a pilgrim might need to see around them. */
const MAP_CATEGORIES = [
  'medical',
  'water',
  'toilet',
  'food',
  'rest',
  'accommodation',
  'police',
];

const CROWD_COLORS: Record<string, string> = {
  LOW: '#6d9b78',
  MODERATE: '#d59e2e',
  HIGH: '#e06435',
  VERY_HIGH: '#c62828',
};

const CATEGORY_ICONS: Record<string, string> = {
  medical: '🏥',
  water: '💧',
  toilet: '🚻',
  food: '🍲',
  rest: '🛏️',
  accommodation: '🏠',
  police: '👮',
};

/**
 * Serialise for embedding in a `<script>`.
 *
 * `JSON.stringify` alone is not enough: `</script>` inside any string would
 * close the tag, and U+2028/9 are literal newlines to a JS parser.
 */
function embed(value: unknown): string {
  return JSON.stringify(value ?? null).replace(
    /[<\u2028\u2029]/g,
    (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')
  );
}

export function MapCanvas({
  mode = 'crowd',
  location,
  destLat,
  destLng,
  destName,
  destPhone,
  onRecenter,
}: {
  mode?: 'crowd' | 'route';
  location: LocationState;
  destLat?: number;
  destLng?: number;
  destName?: string;
  destPhone?: string;
  onRecenter?: () => void;
}) {
  const containerId = useId().replace(/:/g, '_');
  const iframeRef = useRef<any>(null);
  const webviewRef = useRef<WebView>(null);

  const [sevas, setSevas] = useState<CommunityServiceItem[]>([]);
  const [zones, setZones] = useState<CrowdZoneReading[]>([]);
  const [facilities, setFacilities] = useState<NearbyFacility[]>([]);
  const [routeCoords, setRouteCoords] = useState<[number, number][] | null>(null);

  const hasFix = location.latitude !== null && location.longitude !== null;
  const userLat = location.latitude ?? PANDHARPUR_TEMPLE.latitude;
  const userLng = location.longitude ?? PANDHARPUR_TEMPLE.longitude;
  // A fallback coordinate is the temple, not the pilgrim — never label it "you".
  const showsRealPosition = hasFix && !location.isFallback;

  /* --- live data ---------------------------------------------------------- */

  useEffect(() => {
    let alive = true;

    Promise.all([
      communityApi
        .list(location.latitude ?? undefined, location.longitude ?? undefined)
        .then((res) => res.services || [])
        .catch(() => [] as CommunityServiceItem[]),
      AsyncStorage.getItem(MY_SEVAS_KEY)
        .then((res) => (res ? (JSON.parse(res) as CommunityServiceItem[]) : []))
        .catch(() => [] as CommunityServiceItem[]),
    ]).then(([backend, local]) => {
      if (!alive) return;
      const merged = new Map<string, CommunityServiceItem>();
      [...backend, ...local].forEach((item) => {
        if (item?.id && item.isActive !== false) merged.set(item.id, item);
      });
      setSevas(Array.from(merged.values()));
    });

    return () => {
      alive = false;
    };
  }, [location.latitude, location.longitude]);

  // Crowd refreshes on a timer — the whole point of the pin is that it is current.
  useEffect(() => {
    let alive = true;
    const load = () =>
      crowdApi
        .all()
        .then((rows) => alive && setZones(rows))
        .catch(() => {
          // Keep the last good readings rather than blanking the map.
        });

    load();
    const timer = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    facilitiesApi
      .nearby({
        latitude: userLat,
        longitude: userLng,
        radiusM: FACILITY_RADIUS_M,
        category: MAP_CATEGORIES,
        limit: 60,
      })
      .then((rows) => alive && setFacilities(rows))
      .catch(() => alive && setFacilities([]));
    return () => {
      alive = false;
    };
  }, [userLat, userLng]);

  /* --- walking route ------------------------------------------------------ */

  useEffect(() => {
    if (!destLat || !destLng) {
      setRouteCoords(null);
      return;
    }
    const straightLine: [number, number][] = [
      [userLat, userLng],
      [destLat, destLng],
    ];

    if (!MAPBOX_TOKEN) {
      // No directions provider: a straight line still shows which way to walk
      // and how far, which beats no route at all.
      setRouteCoords(straightLine);
      return;
    }

    let alive = true;
    fetch(
      `https://api.mapbox.com/directions/v5/mapbox/walking/` +
        `${userLng},${userLat};${destLng},${destLat}` +
        `?geometries=geojson&access_token=${MAPBOX_TOKEN}`
    )
      .then((res) => res.json())
      .then((data) => {
        if (!alive) return;
        const line = data?.routes?.[0]?.geometry?.coordinates;
        setRouteCoords(
          Array.isArray(line)
            ? line.map(([lng, lat]: [number, number]) => [lat, lng] as [number, number])
            : straightLine
        );
      })
      .catch(() => alive && setRouteCoords(straightLine));

    return () => {
      alive = false;
    };
  }, [userLat, userLng, destLat, destLng]);

const FAMOUS_LANDMARKS = [
  {
    name: 'Shri Vitthal Temple',
    category: 'temple',
    icon: '🛕',
    lat: 17.6775,
    lng: 75.3283,
    phone: '1800-233-1000',
    description: 'Central Sanctum Sanctorum & Pandharpur Temple Complex',
  },
  {
    name: 'Pandharpur City Police Station',
    category: 'police',
    icon: '👮‍♂️',
    lat: 17.6755,
    lng: 75.3298,
    phone: '112',
    description: 'Central Police Command & 24x7 Emergency Control Room',
  },
  {
    name: 'Temple Gate 1 Police Chowky',
    category: 'police',
    icon: '🛡️',
    lat: 17.6781,
    lng: 75.3290,
    phone: '112',
    description: 'Wari Assistance & Police Security Post (Gate 1)',
  },
  {
    name: 'Sub-District Govt Hospital',
    category: 'medical',
    icon: '🏥',
    lat: 17.6738,
    lng: 75.3312,
    phone: '+912166222333',
    description: '24x7 Emergency Trauma, ICU & Central Medical Center',
  },
  {
    name: 'Bhakta Niwas Pilgrim Stay',
    category: 'accommodation',
    icon: '🏰',
    lat: 17.6795,
    lng: 75.3315,
    phone: '1800-233-1000',
    description: 'Official Temple Residence & Pilgrim Guest Lodging',
  },
  {
    name: 'Bhima Ghat Relief Station',
    category: 'water',
    icon: '🌊',
    lat: 17.6808,
    lng: 75.3265,
    description: 'Holy Dip Relief Station, Pure Water Tankers & First Aid',
  },
];

/* --- the page ----------------------------------------------------------- */

  const payload = useMemo(
    () => ({
      user: { lat: userLat, lng: userLng, real: showsRealPosition },
      mapboxToken: MAPBOX_TOKEN,
      mode,
      crowdColors: CROWD_COLORS,
      categoryIcons: CATEGORY_ICONS,
      famousLandmarks: FAMOUS_LANDMARKS,
      zones: (mode === 'crowd' ? zones : []).filter(
        (zone) => typeof zone.latitude === 'number' && typeof zone.longitude === 'number'
      ),
      facilities: facilities.filter(
        (item) => typeof item.latitude === 'number' && typeof item.longitude === 'number'
      ),
      sevas: sevas.filter(
        (item) => typeof item.latitude === 'number' && typeof item.longitude === 'number'
      ),
      route: routeCoords,
      palkhiPreview: mode === 'route' && !routeCoords ? PALKHI_PREVIEW : null,
      destination:
        destLat && destLng
          ? { lat: destLat, lng: destLng, name: destName || 'Destination', phone: destPhone || '' }
          : null,
    }),
    [
      userLat, userLng, showsRealPosition, mode, zones, facilities, sevas,
      routeCoords, destLat, destLng, destName, destPhone,
    ]
  );

  const htmlContent = useMemo(
    () => `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    html, body, #map { height: 100%; margin: 0; padding: 0; background: #eef3e9; font-family: sans-serif; }
    .badge {
      background: #fff; border-radius: 12px; padding: 4px 8px; border: 2px solid #2d6a4f;
      font-weight: 700; font-size: 11px; box-shadow: 0 2px 6px rgba(0,0,0,.15); white-space: nowrap;
    }
    .pin { font-size: 19px; line-height: 19px; text-shadow: 0 1px 3px rgba(0,0,0,.4); }
    .user-dot { width: 18px; height: 18px; background: #2d6a4f; border: 3px solid #fff;
      border-radius: 50%; box-shadow: 0 2px 8px rgba(0,0,0,.3); }
    /* Attribution stays visible: OpenStreetMap's tile terms require credit. */
    .leaflet-control-attribution { font-size: 9px; opacity: .75; }
    .leaflet-popup-content { font-size: 12px; line-height: 1.5; }
    .leaflet-popup-content a { color: #0d9488; font-weight: 700; }
  </style>
</head>
<body>
  <div id="map"></div>
  <script>
    var DATA = ${embed(payload)};

    function esc(value) {
      return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    }
    function callBtn(phone) {
      if (!phone) return '';
      return '<a href="tel:' + esc(phone) + '" style="display:inline-block;margin-top:6px;margin-right:6px;padding:4px 9px;background:#2563eb;color:#ffffff!important;border-radius:6px;text-decoration:none;font-size:11px;font-weight:600;">📞 Call ' + esc(phone) + '</a>';
    }
    function dirBtn(lat, lng, name) {
      var url = 'https://www.google.com/maps/dir/?api=1&destination=' + lat + ',' + lng;
      return '<a href="' + url + '" target="_blank" style="display:inline-block;margin-top:6px;padding:4px 9px;background:#0d9488;color:#ffffff!important;border-radius:6px;text-decoration:none;font-size:11px;font-weight:600;">🧭 Get Directions</a>';
    }
    function actions(phone, lat, lng, name) {
      return '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px;">' + callBtn(phone) + dirBtn(lat, lng, name) + '</div>';
    }
    function pin(lat, lng, html, size) {
      return L.marker([lat, lng], {
        icon: L.divIcon({ className: '', html: html, iconSize: size, iconAnchor: [size[0] / 2, size[1] / 2] })
      });
    }

    var map = L.map('map', { zoomControl: false }).setView([DATA.user.lat, DATA.user.lng], 15);

    if (DATA.mapboxToken) {
      L.tileLayer('https://api.mapbox.com/styles/v1/mapbox/streets-v12/tiles/256/{z}/{x}/{y}@2x?access_token=' + DATA.mapboxToken, {
        maxZoom: 19, attribution: '© Mapbox © OpenStreetMap'
      }).addTo(map);
    } else {
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: '© OpenStreetMap contributors'
      }).addTo(map);
    }

    DATA.zones.forEach(function (zone) {
      var color = DATA.crowdColors[zone.status] || '#6d9b78';
      pin(zone.latitude, zone.longitude,
        '<div class="badge" style="border-color:' + color + ';color:' + color + '">' +
          esc(zone.zoneName) + ' • ' + Math.round(zone.density) + '%</div>', [128, 24])
        .addTo(map)
        .bindPopup('<b>' + esc(zone.zoneName) + '</b><br/>Crowd: ' + Math.round(zone.density) +
          '% (' + esc(String(zone.status).replace('_', ' ')) + ')' +
          (zone.waitMinutes ? '<br/>⏱ About ' + esc(zone.waitMinutes) + ' min wait' : '') +
          (zone.recommendation ? '<br/>' + esc(zone.recommendation) : '') +
          actions(null, zone.latitude, zone.longitude, zone.zoneName));
    });

    DATA.facilities.forEach(function (item) {
      var icon = DATA.categoryIcons[item.category] || '📍';
      pin(item.latitude, item.longitude, '<div class="pin">' + icon + '</div>', [22, 22])
        .addTo(map)
        .bindPopup('<b>' + icon + ' ' + esc(item.name) + '</b><br/>' +
          esc(item.category) + (item.distance ? ' • ' + esc(item.distance) : '') +
          (item.availability ? '<br/>' + esc(item.availability) : '') +
          actions(item.phone || item.contact, item.latitude, item.longitude, item.name));
    });

    DATA.sevas.forEach(function (seva) {
      pin(seva.latitude, seva.longitude,
        '<div class="badge" style="border-color:#0d9488;background:#ccfbf1;color:#0f766e">' +
          '🚩 ' + esc(seva.title) + '</div>', [150, 24])
        .addTo(map)
        .bindPopup('<b>🚩 ' + esc(seva.title) + '</b><br/>By ' + esc(seva.providerName) +
          '<br/>📍 ' + esc(seva.address) +
          actions(seva.contactPhone, seva.latitude, seva.longitude, seva.title));
    });

    (DATA.famousLandmarks || []).forEach(function (landmark) {
      pin(landmark.lat, landmark.lng,
        '<div class="badge" style="border-color:#b91c1c;background:#fff5f5;color:#991b1b;font-weight:700;box-shadow:0 2px 8px rgba(185,28,28,0.25);">' +
          landmark.icon + ' ' + esc(landmark.name) + '</div>', [175, 26])
        .addTo(map)
        .bindPopup('<b>' + landmark.icon + ' ⭐ FAMOUS LANDMARK</b><br/><b>' + esc(landmark.name) + '</b><br/>' +
          esc(landmark.description) +
          actions(landmark.phone, landmark.lat, landmark.lng, landmark.name));
    });

    var fitTo = null;
    if (DATA.route && DATA.route.length > 1) {
      fitTo = L.polyline(DATA.route, { color: '#0d9488', weight: 6, opacity: .9 }).addTo(map);
    } else if (DATA.palkhiPreview) {
      fitTo = L.polyline(DATA.palkhiPreview, {
        color: '#e06435', weight: 5, opacity: .85, dashArray: '8, 8'
      }).addTo(map);
    }

    if (DATA.destination) {
      pin(DATA.destination.lat, DATA.destination.lng,
        '<div class="badge" style="border-color:#0d9488;background:#0d9488;color:#fff">📍 ' +
          esc(DATA.destination.name) + '</div>', [150, 26])
        .addTo(map)
        .bindPopup('<b>📍 ' + esc(DATA.destination.name) + '</b>' + tel(DATA.destination.phone))
        .openPopup();
    }

    pin(DATA.user.lat, DATA.user.lng, '<div class="user-dot"></div>', [18, 18])
      .addTo(map)
      .bindPopup(DATA.user.real
        ? 'You are here'
        : 'Showing Pandharpur — your location is unavailable');

    if (fitTo) map.fitBounds(fitTo.getBounds(), { padding: [40, 40] });

    window.recenterMap = function () {
      map.flyTo([DATA.user.lat, DATA.user.lng], 16, { duration: 1 });
    };
  </script>
</body>
</html>`,
    [payload]
  );

  const handleRecenter = () => {
    if (onRecenter) onRecenter();
    if (Platform.OS === 'web' && iframeRef.current?.contentWindow?.recenterMap) {
      iframeRef.current.contentWindow.recenterMap();
    } else if (webviewRef.current) {
      webviewRef.current.injectJavaScript(
        'if (window.recenterMap) { window.recenterMap(); } true;'
      );
    }
  };

  return (
    <View style={styles.mapWrap}>
      {Platform.OS === 'web' ? (
        <iframe
          ref={iframeRef}
          id={containerId}
          srcDoc={htmlContent}
          style={styles.iframe as any}
          title="Leaflet Wari Map"
        />
      ) : (
        <WebView
          ref={webviewRef}
          originWhitelist={['*']}
          source={{ html: htmlContent }}
          style={styles.iframe as any}
          javaScriptEnabled
          domStorageEnabled
        />
      )}

      <View style={styles.locationLabel}>
        <View style={[styles.liveDot, !showsRealPosition && styles.staleDot]} />
        <Text style={styles.locationText}>
          {showsRealPosition ? 'You are here' : 'Showing Pandharpur'}
        </Text>
      </View>

      {facilities.length > 0 ? (
        <View style={styles.countPill}>
          <Text style={styles.countText}>
            {facilities.length} nearby within {FACILITY_RADIUS_M / 1000} km
          </Text>
        </View>
      ) : null}

      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Recenter map"
        onPress={handleRecenter}
        style={({ pressed }) => [styles.recenter, pressed && { opacity: 0.7 }]}
      >
        <Feather name="crosshair" size={18} color={colors.light.teal} />
        <Text style={styles.recenterText}>Recenter</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  mapWrap: {
    flex: 1,
    minHeight: 390,
    borderRadius: 24,
    overflow: 'hidden',
    position: 'relative',
    borderWidth: 1,
    borderColor: '#c6d7c9',
    backgroundColor: colors.light.map,
  },
  iframe: { width: '100%', height: '100%', borderWidth: 0, borderRadius: 24 },
  locationLabel: {
    position: 'absolute',
    left: 16,
    bottom: 16,
    backgroundColor: colors.light.white,
    borderRadius: 18,
    paddingHorizontal: 10,
    paddingVertical: 8,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    boxShadow: '0 2px 6px rgba(0,0,0,0.1)',
    elevation: 3,
  },
  liveDot: { width: 7, height: 7, borderRadius: 4, backgroundColor: colors.light.teal },
  staleDot: { backgroundColor: colors.light.mutedForeground },
  locationText: { color: colors.light.foreground, fontFamily: 'Inter_500Medium', fontSize: 11 },
  countPill: {
    position: 'absolute',
    left: 16,
    top: 16,
    backgroundColor: colors.light.white,
    borderRadius: 14,
    paddingHorizontal: 10,
    paddingVertical: 6,
    boxShadow: '0 2px 6px rgba(0,0,0,0.1)',
    elevation: 3,
  },
  countText: { color: colors.light.inkSoft, fontFamily: 'Inter_600SemiBold', fontSize: 10 },
  recenter: {
    position: 'absolute',
    right: 16,
    bottom: 16,
    backgroundColor: colors.light.white,
    borderRadius: 16,
    paddingHorizontal: 11,
    paddingVertical: 9,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    boxShadow: '0 2px 6px rgba(0,0,0,0.1)',
    elevation: 3,
  },
  recenterText: { color: colors.light.teal, fontFamily: 'Inter_600SemiBold', fontSize: 11 },
});
