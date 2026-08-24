/**
 * The live density map. Section 10 calls this "the product", and it is.
 *
 * Four decisions worth stating, because each is a place where the obvious
 * implementation would have been wrong.
 *
 * **No basemap tiles by default.** A control room during the Wari is exactly
 * where a map that blocks on a tile CDN fails. The default style is a flat
 * background plus our own polygons — everything the operator needs is a zone,
 * a colour and a label, and all of that is in our own database. Set
 * `VITE_MAP_STYLE` to a style URL if a deployment has a basemap it can reach
 * reliably; the zone layers sit on top either way.
 *
 * **A zone with no reading is grey, not green.** The GeoJSON carries an
 * explicit `hasReading` flag rather than defaulting a missing density to zero.
 * This is the same rule as everywhere else in the product and it matters most
 * here, because a green polygon is the single most reassuring thing on the
 * screen.
 *
 * **Camera markers sit at the zone centroid, and say so.** The schema stores a
 * camera's zone, not its coordinates — cameras are mounted, surveyed and moved
 * by the trust, and we do not have their positions. Rendering a marker at a
 * plausible-looking spot would be inventing data on a safety screen. One
 * marker per zone, at the centroid, labelled with the count.
 *
 * **Flow arrows are omitted below 0.05 m/s.** A crowd that is barely moving has
 * no direction, and an arrow pointing somewhere at random is worse than no
 * arrow — it is a claim about which way people are going.
 */

import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'
import type { Map as MapLibreMap, StyleSpecification } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import type { Camera, ReplayFrame, Zone, ZoneStatus } from '@/api/types'
import { densityColour, flowBearing, UNKNOWN_COLOUR, zoneOpacity } from '@/lib/density'
import { useI18n } from '@/i18n'
import { useLive } from '@/state/live'

/** Shri Vitthal-Rukmini Temple, Pandharpur — the same anchor the seed uses. */
const TEMPLE: [number, number] = [75.3306, 17.6797]

const BLANK_STYLE: StyleSpecification = {
  version: 8,
  glyphs: undefined,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#0d1220' } }],
}

const STYLE_URL = import.meta.env.VITE_MAP_STYLE as string | undefined

interface Props {
  /** When present the map renders this replay frame instead of live state. */
  frame?: ReplayFrame | null
}

export function ZoneMap({ frame = null }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const ready = useRef(false)

  const { zones, cameras, statusFor, config, tick } = useLive()
  const { lang } = useI18n()

  // --- create once --------------------------------------------------------
  useEffect(() => {
    if (!container.current || map.current) return

    const instance = new maplibregl.Map({
      container: container.current,
      style: STYLE_URL ?? BLANK_STYLE,
      center: TEMPLE,
      zoom: 15.2,
      attributionControl: STYLE_URL ? undefined : false,
      // Section 10: map transitions ease at 200ms. Anything longer and an
      // operator dragging the scrubber watches the map catch up to them.
      fadeDuration: 200,
    })

    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
    instance.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left')

    instance.on('load', () => {
      instance.addSource('zones', { type: 'geojson', data: emptyCollection() })
      instance.addSource('flow', { type: 'geojson', data: emptyCollection() })
      instance.addSource('cameras', { type: 'geojson', data: emptyCollection() })

      instance.addLayer({
        id: 'zone-fill',
        type: 'fill',
        source: 'zones',
        paint: {
          'fill-color': ['get', 'colour'],
          'fill-opacity': ['get', 'opacity'],
        },
      })

      instance.addLayer({
        id: 'zone-outline',
        type: 'line',
        source: 'zones',
        paint: {
          'line-color': ['get', 'colour'],
          // A stale zone gets a dashed outline as well as a dead fill. Colour
          // alone is not enough — some operators are colour-blind, and a
          // control room is dim.
          'line-width': ['case', ['get', 'stale'], 1.5, 2.5],
          'line-dasharray': ['case', ['get', 'stale'], ['literal', [2, 2]], ['literal', [1, 0]]],
        },
      })

      // Flow arrows. A line from the zone centroid in the direction the crowd
      // is actually moving, length scaled by speed.
      instance.addLayer({
        id: 'zone-flow',
        type: 'line',
        source: 'flow',
        paint: {
          'line-color': '#e9ecf4',
          'line-width': 2,
          'line-opacity': 0.75,
        },
      })

      instance.addLayer({
        id: 'camera-dot',
        type: 'circle',
        source: 'cameras',
        paint: {
          'circle-radius': 5,
          'circle-color': ['get', 'colour'],
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#0d1220',
        },
      })

      ready.current = true
      instance.fire('wariverse.ready')
    })

    map.current = instance
    return () => {
      instance.remove()
      map.current = null
      ready.current = false
    }
  }, [])

  // --- push data on every change -----------------------------------------
  useEffect(() => {
    const instance = map.current
    if (!instance || !ready.current) return

    const thresholds = normaliseThresholds(config?.density_thresholds)

    // In replay mode the frame is the truth; live status is ignored entirely so
    // the scrubber cannot accidentally blend a past frame with a present one.
    const replayByCode = frame ? new Map(frame.zones.map((z) => [z.zone_code, z])) : null

    const zoneFeatures: GeoJSON.Feature[] = []
    const flowFeatures: GeoJSON.Feature[] = []

    for (const zone of zones) {
      if (!zone.geometry) continue

      let colour = UNKNOWN_COLOUR
      let hasReading = false
      let stale = false
      let density: number | null = null
      let live: ZoneStatus | null = null

      if (replayByCode) {
        const state = replayByCode.get(zone.code)
        if (state) {
          hasReading = true
          density = state.density
          colour = densityColour(state.density, thresholds)
        }
      } else {
        live = statusFor(zone.id)
        if (live) {
          hasReading = true
          stale = live.is_stale
          density = live.density
          // A stale reading keeps its shape on the map but loses its colour.
          // Holding the last colour is how a replay — or a live map — lies.
          colour = stale ? UNKNOWN_COLOUR : densityColour(live.density, thresholds)
        }
      }

      zoneFeatures.push({
        type: 'Feature',
        id: zone.code,
        geometry: zone.geometry,
        properties: {
          code: zone.code,
          name: lang === 'mr' ? zone.name_mr : zone.name,
          colour,
          opacity: zoneOpacity({ isStale: stale, hasReading }),
          stale: stale || !hasReading,
          density,
        },
      })

      // Flow arrows are live-only: the replay rollups keep peak density, not a
      // mean flow vector, and averaging direction over a minute would point at
      // a direction nobody walked.
      if (live && !live.is_stale) {
        const bearing = flowBearing(live.flow.dx, live.flow.dy)
        if (bearing !== null) {
          const centre = centroid(zone.geometry)
          if (centre) flowFeatures.push(arrow(centre, bearing, live.flow.speed_ms))
        }
      }
    }

    const setSource = (id: string, data: GeoJSON.FeatureCollection): void => {
      const source = instance.getSource(id) as maplibregl.GeoJSONSource | undefined
      source?.setData(data)
    }

    setSource('zones', { type: 'FeatureCollection', features: zoneFeatures })
    setSource('flow', { type: 'FeatureCollection', features: flowFeatures })
    setSource('cameras', cameraFeatures(zones, cameras))
  }, [zones, cameras, statusFor, config, frame, lang, tick])

  return (
    <div className="map">
      <div ref={container} className="map__canvas" />
      <MapLegend />
    </div>
  )
}

function MapLegend() {
  const { t } = useI18n()
  const { config } = useLive()
  const thresholds = normaliseThresholds(config?.density_thresholds)

  const bands: Array<[string, number]> = [
    ['safe', thresholds.safe / 2],
    ['moderate', (thresholds.safe + thresholds.moderate) / 2],
    ['high', (thresholds.moderate + thresholds.high) / 2],
    ['critical', thresholds.high + 0.5],
  ]

  return (
    <div className="map__legend">
      {bands.map(([label, sample]) => (
        <span key={label} className="legend__item">
          <i className="legend__swatch" style={{ background: densityColour(sample, thresholds) }} />
          {label}
        </span>
      ))}
      <span className="legend__item">
        <i className="legend__swatch legend__swatch--unknown" style={{ background: UNKNOWN_COLOUR }} />
        {t('zones.unknown')}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// geometry helpers
// ---------------------------------------------------------------------------
function emptyCollection(): GeoJSON.FeatureCollection {
  return { type: 'FeatureCollection', features: [] }
}

function normaliseThresholds(raw: Record<string, number> | undefined) {
  return {
    safe: raw?.safe ?? 2.0,
    moderate: raw?.moderate ?? 3.5,
    high: raw?.high ?? 5.0,
  }
}

/** Area-weighted centroid of a polygon's outer ring. */
export function centroid(polygon: GeoJSON.Polygon): [number, number] | null {
  const ring = polygon.coordinates[0]
  if (!ring || ring.length < 3) return null

  let twiceArea = 0
  let x = 0
  let y = 0
  for (let i = 0; i < ring.length - 1; i += 1) {
    const a = ring[i]
    const b = ring[i + 1]
    // GeoJSON `Position` is `number[]`, so a malformed ring can carry a
    // one-element coordinate. Skip rather than compute a centroid from NaN.
    const ax = a?.[0]
    const ay = a?.[1]
    const bx = b?.[0]
    const by = b?.[1]
    if (ax === undefined || ay === undefined || bx === undefined || by === undefined) continue
    const cross = ax * by - bx * ay
    twiceArea += cross
    x += (ax + bx) * cross
    y += (ay + by) * cross
  }

  if (twiceArea === 0) {
    // Degenerate ring — fall back to the mean of the vertices rather than
    // dividing by zero and putting the marker in the Atlantic.
    const points = ring.slice(0, -1)
    if (points.length === 0) return null
    const sum = points.reduce<[number, number]>((acc, p) => [acc[0] + (p[0] ?? 0), acc[1] + (p[1] ?? 0)], [0, 0])
    return [sum[0] / points.length, sum[1] / points.length]
  }

  const factor = 1 / (3 * twiceArea)
  return [x * factor, y * factor]
}

/**
 * A short line from `origin` along `bearing`, length scaled by speed.
 *
 * Capped at 60 metres: a fast-moving crowd should read as a longer arrow, but
 * not as an arrow that reaches into the next zone and appears to describe it.
 */
function arrow(origin: [number, number], bearing: number, speedMs: number): GeoJSON.Feature {
  const metres = Math.min(60, 20 + speedMs * 25)
  const radians = (bearing * Math.PI) / 180
  // Degrees per metre at this latitude. Good enough over tens of metres.
  const dLat = (metres * Math.cos(radians)) / 111_320
  const dLon = (metres * Math.sin(radians)) / (111_320 * Math.cos((origin[1] * Math.PI) / 180))
  const tip: [number, number] = [origin[0] + dLon, origin[1] + dLat]

  // Two barbs so it reads as an arrow rather than a stick.
  const barb = (offsetDeg: number): [number, number] => {
    const a = ((bearing + 180 + offsetDeg) * Math.PI) / 180
    const len = metres * 0.3
    return [
      tip[0] + (len * Math.sin(a)) / (111_320 * Math.cos((origin[1] * Math.PI) / 180)),
      tip[1] + (len * Math.cos(a)) / 111_320,
    ]
  }

  return {
    type: 'Feature',
    geometry: {
      type: 'MultiLineString',
      coordinates: [
        [origin, tip],
        [barb(-25), tip, barb(25)],
      ],
    },
    properties: { bearing, speed: speedMs },
  }
}

const CAMERA_COLOURS: Record<string, string> = {
  online: '#2e7d5b',
  degraded: '#e0a106',
  offline: '#c42b1c',
}

/**
 * One marker per zone that has cameras, coloured by the worst status in it.
 *
 * Worst rather than most-common on purpose: one offline camera in a zone of
 * four is a fact an operator needs, and a green dot averaging it away is how it
 * stops being one.
 */
function cameraFeatures(zones: Zone[], cameras: Camera[]): GeoJSON.FeatureCollection {
  const byZone = new Map<string, Camera[]>()
  for (const camera of cameras) {
    const list = byZone.get(camera.zone_id) ?? []
    list.push(camera)
    byZone.set(camera.zone_id, list)
  }

  const rank: Record<string, number> = { offline: 0, degraded: 1, online: 2 }
  const features: GeoJSON.Feature[] = []

  for (const zone of zones) {
    const list = byZone.get(zone.id)
    if (!list || list.length === 0 || !zone.geometry) continue
    const centre = centroid(zone.geometry)
    if (!centre) continue

    const worst = list.reduce((acc, c) => ((rank[c.status] ?? 3) < (rank[acc.status] ?? 3) ? c : acc), list[0]!)
    features.push({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: centre },
      properties: {
        colour: CAMERA_COLOURS[worst.status] ?? UNKNOWN_COLOUR,
        zone: zone.code,
        total: list.length,
        online: list.filter((c) => c.status === 'online').length,
      },
    })
  }

  return { type: 'FeatureCollection', features }
}
