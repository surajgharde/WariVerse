/**
 * Live console state.
 *
 * Section 4/M3's first rule: *nothing auto-refreshes the whole page; the
 * WebSocket patches state in place.* That is what this store is. Density
 * arrives as deltas and is merged per zone; the map, the rails and the strip
 * all read from here and re-render only where a value actually changed.
 *
 * Three deliberate decisions.
 *
 * **Staleness is recomputed on a clock, not on arrival.** A zone goes grey
 * ninety seconds after its last reading whether or not anything new arrives —
 * in fact *especially* when nothing new arrives, since that is the case that
 * matters. Recomputing only on receipt would leave a dead pipeline's last
 * reading looking fresh forever, which is the exact failure the stale badge
 * exists to prevent. Hence the one-second tick.
 *
 * **The socket is a hint; HTTP is the truth.** Alert events carry an id and a
 * status, not a whole alert, so an alert event triggers a refetch rather than
 * an in-place patch. Acknowledgement state decides what an operator sees as
 * outstanding, and it is worth one round trip to have it come from the same
 * place the audit log does.
 *
 * **Losing the socket degrades to polling and says so.** A console that
 * silently stops updating is worse than one that admits it is behind — the
 * banner is not a nicety, it is the difference between an operator trusting a
 * number and an operator checking it.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef } from 'react'
import type { ReactNode } from 'react'

import { tokens } from '@/api/client'
import {
  alerts as alertsApi,
  command as commandApi,
  crowd as crowdApi,
  incidents as incidentsApi,
} from '@/api/endpoints'
import { CrowdSocket } from '@/api/socket'
import type { SocketState } from '@/api/socket'
import type {
  Alert,
  Camera,
  ChangeDigest,
  ConsoleConfig,
  Incident,
  IncidentSeverity,
  IncidentStatus,
  IncidentType,
  KpiStrip,
  ServerEvent,
  WsZone,
  Zone,
  ZoneStatus,
} from '@/api/types'

/** How often the derived endpoints are re-read. They are computed per request
 * rather than pushed, so polling is the honest mechanism — but slowly, because
 * a KPI strip that re-reads every second is a strip nobody can read. */
const KPI_POLL_MS = 15_000
const DIGEST_POLL_MS = 60_000
/** Only while the socket is down. */
const CROWD_FALLBACK_POLL_MS = 10_000

interface State {
  config: ConsoleConfig | null
  zones: Zone[]
  /** zone_id -> latest status. Patched in place by the socket. */
  statuses: Record<string, ZoneStatus>
  cameras: Camera[]
  alerts: Alert[]
  incidents: Incident[]
  kpis: KpiStrip | null
  digest: ChangeDigest | null
  socket: SocketState
  /** Bumped every second so age-derived rendering re-evaluates. */
  tick: number
  loading: boolean
  error: string | null
}

type Action =
  | { type: 'bootstrapped'; payload: Partial<State> }
  | { type: 'zones'; payload: ZoneStatus[] }
  | { type: 'zone'; payload: ZoneStatus }
  | { type: 'alerts'; payload: Alert[] }
  | { type: 'incidents'; payload: Incident[] }
  | { type: 'cameras'; payload: Camera[] }
  | { type: 'kpis'; payload: KpiStrip }
  | { type: 'digest'; payload: ChangeDigest }
  | { type: 'socket'; payload: SocketState }
  | { type: 'tick' }
  | { type: 'error'; payload: string | null }

const EMPTY: State = {
  config: null,
  zones: [],
  statuses: {},
  cameras: [],
  alerts: [],
  incidents: [],
  kpis: null,
  digest: null,
  socket: 'connecting',
  tick: 0,
  loading: true,
  error: null,
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'bootstrapped':
      return { ...state, ...action.payload, loading: false, error: null }
    case 'zones': {
      const statuses: Record<string, ZoneStatus> = {}
      for (const status of action.payload) statuses[status.zone_id] = status
      return { ...state, statuses }
    }
    case 'zone':
      return { ...state, statuses: { ...state.statuses, [action.payload.zone_id]: action.payload } }
    case 'alerts':
      return { ...state, alerts: action.payload }
    case 'incidents':
      return { ...state, incidents: action.payload }
    case 'cameras':
      return { ...state, cameras: action.payload }
    case 'kpis':
      return { ...state, kpis: action.payload }
    case 'digest':
      return { ...state, digest: action.payload }
    case 'socket':
      return { ...state, socket: action.payload }
    case 'tick':
      return { ...state, tick: state.tick + 1 }
    case 'error':
      return { ...state, error: action.payload, loading: false }
  }
}

/**
 * Convert a socket zone frame into the same shape `/crowd/live` returns.
 *
 * `age_seconds` and `is_stale` are computed here from `observed_at` rather than
 * copied, because they are properties of *now* and the frame is already a few
 * hundred milliseconds old by the time it is parsed.
 */
function fromSocket(zone: WsZone, staleAfter: number): ZoneStatus {
  const age = Math.max(0, (Date.now() - new Date(zone.observed_at).getTime()) / 1000)
  const speed = Math.hypot(zone.flow_dx, zone.flow_dy)
  const bearing = speed < 0.05 ? null : compass(zone.flow_dx, zone.flow_dy)
  return {
    zone_id: zone.zone_id,
    zone_code: zone.zone_code,
    zone_name: zone.zone_name,
    zone_name_mr: zone.zone_name_mr,
    person_count: zone.person_count,
    density: zone.density,
    level: zone.level,
    occupancy_pct:
      zone.capacity_persons > 0
        ? Math.round((1000 * zone.person_count) / zone.capacity_persons) / 10
        : null,
    flow: { speed_ms: speed, direction: bearing, dx: zone.flow_dx, dy: zone.flow_dy },
    stagnation_index: zone.stagnation_index,
    counterflow_ratio: zone.counterflow_ratio,
    confidence: zone.confidence,
    source: zone.source,
    camera_count: zone.camera_count,
    observed_at: zone.observed_at,
    age_seconds: Math.round(age * 10) / 10,
    is_stale: age > staleAfter,
    area_m2: zone.area_m2,
    notes: zone.notes,
  }
}

const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'] as const

function compass(dx: number, dy: number): string {
  const bearing = ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360
  return COMPASS[Math.floor(((bearing + 22.5) % 360) / 45)] ?? 'N'
}

interface LiveStore extends State {
  /** Zone status by id, with staleness re-evaluated against the current tick. */
  statusFor: (zoneId: string) => ZoneStatus | null
  acknowledge: (alertId: string, note?: string) => Promise<void>
  resolve: (alertId: string, resolution: string) => Promise<void>
  /** Open an incident from an alert, acknowledging the alert in the same act. */
  escalateToIncident: (alert: Alert, severity?: IncidentSeverity) => Promise<Incident>
  dispatchUnit: (incidentId: string, responderId: string, note?: string) => Promise<void>
  updateIncident: (
    incidentId: string,
    body: { status?: IncidentStatus; note?: string; outcome_note?: string },
  ) => Promise<void>
  refresh: () => void
}

/**
 * Which kind of incident an alert becomes.
 *
 * Only the mappings that are unambiguous are listed. Everything else becomes
 * `other` rather than being guessed at — an incident filed under the wrong type
 * gets the wrong responder suggested, and a wrong suggestion under time
 * pressure is worse than no suggestion.
 */
function incidentTypeForAlert(alertType: string): IncidentType {
  if (alertType.startsWith('density') || alertType === 'stagnation' || alertType === 'counterflow') {
    return 'crowd_crush_risk'
  }
  if (alertType === 'camera_offline') return 'facility_failure'
  return 'other'
}

const LiveContext = createContext<LiveStore | null>(null)

export function LiveProvider({ children, onAuthFailure }: { children: ReactNode; onAuthFailure: () => void }) {
  const [state, dispatch] = useReducer(reducer, EMPTY)
  const staleAfterRef = useRef(90)
  const socketRef = useRef<SocketState>('connecting')
  socketRef.current = state.socket

  const loadAlerts = useCallback(async () => {
    const page = await alertsApi.list()
    dispatch({ type: 'alerts', payload: page.items })
  }, [])

  const loadIncidents = useCallback(async () => {
    const page = await incidentsApi.list()
    dispatch({ type: 'incidents', payload: page.items })
  }, [])

  const loadCrowd = useCallback(async () => {
    const live = await crowdApi.live()
    dispatch({ type: 'zones', payload: live.zones })
  }, [])

  const loadDerived = useCallback(async () => {
    const [kpis, digest] = await Promise.all([commandApi.kpis(), commandApi.changes()])
    dispatch({ type: 'kpis', payload: kpis })
    dispatch({ type: 'digest', payload: digest })
  }, [])

  // --- bootstrap ---------------------------------------------------------
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [config, zones, live, cameras, alertPage, incidentPage, kpis, digest] = await Promise.all([
          commandApi.config(),
          crowdApi.zones(),
          crowdApi.live(),
          crowdApi.cameras(),
          alertsApi.list(),
          incidentsApi.list(),
          commandApi.kpis(),
          commandApi.changes(),
        ])
        if (cancelled) return
        staleAfterRef.current = config.stale_reading_seconds
        const statuses: Record<string, ZoneStatus> = {}
        for (const status of live.zones) statuses[status.zone_id] = status
        dispatch({
          type: 'bootstrapped',
          payload: {
            config,
            zones,
            statuses,
            cameras,
            alerts: alertPage.items,
            incidents: incidentPage.items,
            kpis,
            digest,
          },
        })
      } catch (exc) {
        if (!cancelled) {
          dispatch({
            type: 'error',
            payload: exc instanceof Error ? exc.message : 'Could not load the console.',
          })
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // --- socket ------------------------------------------------------------
  useEffect(() => {
    const socket = new CrowdSocket(() => tokens.access(), {
      onState: (next) => dispatch({ type: 'socket', payload: next }),
      onAuthFailure,
      onEvent: (event: ServerEvent) => {
        switch (event.type) {
          case 'hello':
            dispatch({
              type: 'zones',
              payload: event.data.zones.map((z) => fromSocket(z, staleAfterRef.current)),
            })
            break
          case 'density.updated':
            dispatch({ type: 'zone', payload: fromSocket(event.data, staleAfterRef.current) })
            break
          case 'alert.raised':
          case 'alert.updated':
            // Refetch rather than patch: the event carries an id, and the feed
            // needs the whole alert including who acknowledged it.
            void loadAlerts()
            break
          case 'incident.raised':
          case 'incident.updated':
          case 'incident.sla_breached':
            // Same reasoning as alerts: the payload is a reference, and the
            // board needs the whole incident including its assigned unit. An
            // SLA breach in particular must not be patched in from an event —
            // it is the row an inquiry reads, and it comes from the database.
            void loadIncidents()
            void loadDerived()
            break
          case 'camera.status_changed':
            void crowdApi.cameras().then((cameras) => dispatch({ type: 'cameras', payload: cameras }))
            break
          case 'heartbeat':
          case 'pong':
            // Liveness only. `CrowdSocket` already noted the frame's arrival.
            break
        }
      },
    })

    socket.start()
    return () => socket.stop()
  }, [loadAlerts, loadIncidents, loadDerived, onAuthFailure])

  // --- the staleness clock ------------------------------------------------
  // One second, deliberately. This is what turns a zone grey when its pipeline
  // dies, and it must not depend on any data arriving to do it.
  useEffect(() => {
    const timer = window.setInterval(() => dispatch({ type: 'tick' }), 1_000)
    return () => window.clearInterval(timer)
  }, [])

  // --- polling for the derived endpoints ----------------------------------
  useEffect(() => {
    const kpiTimer = window.setInterval(() => void commandApi.kpis().then((k) => dispatch({ type: 'kpis', payload: k })).catch(() => {}), KPI_POLL_MS)
    const digestTimer = window.setInterval(
      () => void commandApi.changes().then((d) => dispatch({ type: 'digest', payload: d })).catch(() => {}),
      DIGEST_POLL_MS,
    )
    return () => {
      window.clearInterval(kpiTimer)
      window.clearInterval(digestTimer)
    }
  }, [])

  // --- degraded mode ------------------------------------------------------
  // Only while the socket is not open. Polling alongside a healthy socket would
  // double the load for no freshness gained.
  useEffect(() => {
    if (state.socket === 'open') return
    const timer = window.setInterval(() => void loadCrowd().catch(() => {}), CROWD_FALLBACK_POLL_MS)
    return () => window.clearInterval(timer)
  }, [state.socket, loadCrowd])

  const acknowledge = useCallback(
    async (alertId: string, note?: string) => {
      const updated = await alertsApi.acknowledge(alertId, note)
      dispatch({
        type: 'alerts',
        payload: state.alerts.map((a) => (a.id === updated.id ? updated : a)),
      })
      void loadDerived()
    },
    [state.alerts, loadDerived],
  )

  const resolve = useCallback(
    async (alertId: string, resolution: string) => {
      await alertsApi.resolve(alertId, resolution)
      await loadAlerts()
      void loadDerived()
    },
    [loadAlerts, loadDerived],
  )

  /**
   * Open an incident from an alert.
   *
   * The alert is acknowledged as part of the same action. An operator who
   * dispatches a unit has plainly seen the alert, and leaving it unacknowledged
   * would keep the escalation clock running against somebody who is already
   * dealing with it.
   */
  const escalateToIncident = useCallback(
    async (alert: Alert, severity: IncidentSeverity = 'high'): Promise<Incident> => {
      const incident = await incidentsApi.create({
        type: incidentTypeForAlert(alert.type),
        severity,
        zone_id: alert.zone_id,
        description:
          `Raised from alert ${alert.type} (${alert.trigger_metric} ${alert.trigger_value}).` +
          (alert.recommended_action ? ` Recommended: ${alert.recommended_action}` : ''),
        source: 'control_room',
      })
      if (alert.status === 'open') {
        await alertsApi.acknowledge(alert.id, `Incident ${incident.reference} opened.`).catch(() => {})
      }
      await loadIncidents()
      void loadAlerts()
      void loadDerived()
      return incident
    },
    [loadIncidents, loadAlerts, loadDerived],
  )

  const dispatchUnit = useCallback(
    async (incidentId: string, responderId: string, note?: string) => {
      await incidentsApi.dispatch(incidentId, responderId, note)
      await loadIncidents()
      void loadDerived()
    },
    [loadIncidents, loadDerived],
  )

  const updateIncident = useCallback(
    async (
      incidentId: string,
      body: { status?: IncidentStatus; note?: string; outcome_note?: string },
    ) => {
      await incidentsApi.update(incidentId, body)
      await loadIncidents()
      void loadDerived()
    },
    [loadIncidents, loadDerived],
  )

  const refresh = useCallback(() => {
    void loadCrowd().catch(() => {})
    void loadAlerts().catch(() => {})
    void loadIncidents().catch(() => {})
    void loadDerived().catch(() => {})
  }, [loadCrowd, loadAlerts, loadIncidents, loadDerived])

  const value = useMemo<LiveStore>(() => {
    const staleAfter = state.config?.stale_reading_seconds ?? 90
    return {
      ...state,
      statusFor: (zoneId: string) => {
        const status = state.statuses[zoneId]
        if (!status) return null
        // Re-derive age against the wall clock. `state.tick` is what makes this
        // run once a second; the value itself is not used.
        const age = Math.max(0, (Date.now() - new Date(status.observed_at).getTime()) / 1000)
        return { ...status, age_seconds: Math.round(age * 10) / 10, is_stale: age > staleAfter }
      },
      acknowledge,
      resolve,
      escalateToIncident,
      dispatchUnit,
      updateIncident,
      refresh,
    }
  }, [state, acknowledge, resolve, escalateToIncident, dispatchUnit, updateIncident, refresh])

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}

export function useLive(): LiveStore {
  const context = useContext(LiveContext)
  if (!context) throw new Error('useLive must be used inside LiveProvider')
  return context
}
