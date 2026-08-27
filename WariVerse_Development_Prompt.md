# WariVerse — Web Application Development Prompt

**Team ID:** U35LE4S3 · **Track:** Pilgrim Experience & Temple Management · **Stage:** Post-PDR build
**Deliverable:** A production-grade web platform (Admin Command Center + Pilgrim PWA + AI service)

> **How to use this file.** Each section is a self-contained prompt block. Paste **Section 0 (Master Prompt)** first into your AI coding tool (Claude Code, Cursor, etc.), then feed sections one at a time in the order given by **Section 18 (Build Order)**. Do not paste the whole file at once — it will produce shallow code. One section = one working, testable increment.

---

## AMENDMENT LOG

> This document is the original requirements brief. Where the built system
> deliberately diverges from it, the divergence is recorded here and marked
> `[AMENDED]` inline, rather than the text being silently rewritten — a spec
> edited to match whatever got built stops being able to answer "what did we
> actually ask for?"

### A-1 · Zero paid or account-gated third-party services (2026-08-26)

**Changed:** Section 6 (architecture), Section 7 (tech stack), Section 11
(observability), Section 14 (repository structure).

**What changed.** The brief named FCM web push, an SMS gateway
(MSG91 / Gupshup), an IVR hook, Sentry, and OSM tile servers. **None of these
are used, and none are needed.** The delivered system requires **no external
account and no paid service whatsoever**. The single external key it can use —
`GEMINI_API_KEY` — is optional and free-tier, and the assistant answers without
it from the same read-only tools.

**Why.** Three reasons, in order of weight:

1. **Section 11 already demanded it, implicitly.** "A system that needs 4G is a
   system that fails during the Wari" (E5). A notification path that depends on
   a commercial gateway's uptime, and a basemap that blocks on a tile CDN, are
   the same class of dependency as 4G. The control room during the Wari is
   precisely where they fail.
2. **Deployability by a temple trust.** A recurring vendor invoice is a
   procurement decision, an annual renewal, and a single point of
   administrative failure between one Wari and the next. Removing it removes an
   entire category of reason the system does not get deployed.
3. **Reproducibility.** Anyone — a reviewer, a successor team, a judge — can run
   the whole system from a clean clone in one command with no signup.

**What replaced them.**

| Named in the brief | What is actually used |
| --- | --- |
| FCM web push | Not implemented. In-app + WebSocket delivery. |
| SMS gateway (MSG91 / Gupshup) | None. `OTP_DEBUG_ECHO` in development; SOS fallback opens the **device's own** SMS app to a configured control-room number, which needs no gateway account. |
| IVR hook | Not implemented. Tier 3 / M10, never reached. |
| Sentry | Not implemented. Structured JSON logs with `trace_id`, plus Prometheus + Grafana, both self-hosted. |
| OSM / commercial tiles | MapLibre rendering **our own zone polygons** on a flat background. No tile server. |
| `services/notifier/` | Not built. Nothing needed it. |

**What this costs, stated honestly.** Real push notifications and an IVR channel
for feature-phone pilgrims are genuinely valuable and are genuinely absent. Both
were Tier 3. Adding either later means adding a provider setting to
`app/core/config.py` *at the same time* as the `.env` entry — the drift this
amendment cleans up happened because a placeholder was added to `.env.example`
that no code ever read.

### A-2 · Plain asyncio scheduler instead of Celery/RQ (Phase 1)

**Changed:** Section 6 (architecture).

Section 6 names Celery or RQ for async jobs. The system runs ten scheduled jobs
— reslotting, no-show expiry, camera watchdog, alert maintenance, SLA sweep,
three purges, chain verification, Palkhi sweep — and Celery would add a broker
process and a worker image to run what are, in substance, timers.

They are implemented as an asyncio interval loop per job with a **Redis
single-runner lock**, so three API replicas do not reslot the same passes three
times. Every job is a plain `async` callable taking no arguments, so moving them
behind Celery later is a decorator on each, not a rewrite. The lock deliberately
**fails open** — with Redis down a job runs anyway, because each is idempotent
and a missed reslot is worse than a duplicated one.

### A-3 · Breach evidence is redacted, never deleted (Phase 6)

**Changed:** Section 4/M5.

The brief allows a System Admin to delete breach evidence. Implemented as
*redaction*: the clip goes, with a mandatory written reason, and the record and
its chain hash remain. A ledger whose rows can vanish is not a ledger — deleting
a row would break the hash chain at exactly the point someone wanted it broken,
which is indistinguishable from the tampering the chain exists to detect. Where
the literal instruction and its stated purpose disagree, the purpose wins.

---

## SECTION 0 — MASTER PROMPT (paste this first)

```
You are a senior full-stack engineer building "WariVerse", an AI-powered crowd
intelligence and pilgrim management platform for the Pandharpur Wari and the
Shri Vitthal-Rukmini Temple, Maharashtra.

CONTEXT
- Scale: 10-15 lakh pilgrims converge on Pandharpur during Ashadhi Ekadashi.
  Darshan queues run 18-24 hours. Palkhi processions walk 250 km over 18 days.
- Users are largely non-technical, elderly, Marathi-speaking, on low-end Android
  phones with intermittent 2G/3G connectivity.
- Operators are temple trust staff, police, and volunteers who need one screen,
  not five dashboards.

YOUR MANDATE
Build a working, deployable web system — not a mockup. Every feature must run
against real or simulated data end to end. No placeholder functions, no "TODO:
implement later", no fake setTimeout data unless it is an explicit, labelled
simulation mode.

NON-NEGOTIABLE ENGINEERING RULES
1. Typed end to end: Python type hints + Pydantic on backend, TypeScript strict
   mode on frontend. No `any`.
2. Every API endpoint: request/response schema, error model, and auth guard.
3. Every AI output carries a confidence score and a timestamp. Nothing is
   presented to an operator as certainty when it is a prediction.
4. Privacy by design: no facial recognition, no biometric identification, no
   individual pilgrim tracking. Crowd analytics are anonymous and aggregate.
   (See Section 14 — this is a hard constraint, not a preference.)
5. Offline-first on the pilgrim app. Assume the network drops mid-Wari.
6. Marathi is the default language. English is the fallback. Not the reverse.
7. Write tests for the queue logic, pass-allocation logic, and alert
   escalation logic. These are the parts that hurt people when they break.

Confirm you understand, then wait for Section 1.
```

---

## SECTION 1 — PROBLEM STATEMENT (grounding context)

```
PROBLEMS THIS SYSTEM SOLVES (from field research):

P1. QUEUE INTEGRITY FAILURE
    VIPs and locally influential people bypass the 24-hour darshan queue at the
    final stage via side entries, using social and political pressure. Human
    guards cannot refuse. Ordinary pilgrims who waited all night are pushed back.
    => Need: an impartial, automated, evidence-backed record of every entry
       through a restricted point, so enforcement does not depend on a guard's
       courage.

P2. CROWD CONGESTION AND CRUSH RISK
    Density spikes at chokepoints (Chandrabhaga ghat, temple corridors, Palkhi
    halt towns) build faster than human observers can report them.
    => Need: real-time density measurement + prediction with enough lead time
       to act (divert, hold, open relief routes).

P3. UNMANAGED, UNPREDICTABLE QUEUES
    Pilgrims stand for 18-24 hours with no idea when their turn comes, so nobody
    leaves the line to eat, rest, or use facilities. This compounds density.
    => Need: a time-slotted darshan pass that is honest about wait time and
       updates dynamically, so the queue becomes a schedule instead of a wall.

P4. SLOW EMERGENCY RESPONSE
    Medical emergencies, missing persons, and incidents are reported by word of
    mouth across police, volunteers, and medical camps with no shared picture.
    => Need: one incident log, one map, one escalation chain.

DESIGN PRINCIPLE: The system must degrade gracefully. If the AI service dies,
the queue, passes, alerts and incident log must keep working on manual input.
A crowd-safety system that fails closed is worse than no system.
```

---

## SECTION 2 — USERS, ROLES & PERMISSIONS

```
Build a role-based access system with exactly these roles:

1. PILGRIM (public, no login required to browse; phone-OTP to book)
   - Books/holds a Smart Darshan Pass
   - Sees live wait time, crowd status by zone, safe route guidance
   - Raises SOS, reports a missing person, finds nearest facility
   - Reads Palkhi/Dindi schedule and live position

2. VOLUNTEER / SEVEKARI (login)
   - Scans pass QR at checkpoints
   - Acknowledges and closes low-severity incidents
   - Reports ground observations (density, blockage, facility issue)

3. SECURITY OFFICER (login)
   - Everything Volunteer can do
   - Views live density map and camera zone status
   - Reviews queue-breach events and marks them Verified / False Positive /
     Authorised (with a mandatory reason)

4. MEDICAL / EMERGENCY RESPONDER (login)
   - Receives dispatched incidents, updates status, logs outcome

5. TEMPLE ADMINISTRATOR (login, MFA)
   - Full command center, capacity configuration, pass quota control
   - Analytics, exports, audit trail

6. SYSTEM ADMIN (login, MFA)
   - User/role management, camera & zone configuration, retention settings

Implement as JWT (access 15 min + refresh 7 days) with a permission matrix in
code, not scattered `if role == "admin"` checks. Every privileged action writes
to an append-only audit log with actor, action, target, timestamp, and IP.
```

---

## SECTION 3 — SCOPE CONTROL

```
BUILD IN THIS ORDER. Do not start a tier until the previous one runs.

TIER 1 — CORE (must exist for the demo to mean anything)
  M1 Smart Darshan Pass (booking, dynamic slotting, QR, checkpoint scan)
  M2 Crowd Intelligence (zone density from video/simulation, heat map, alerts)
  M3 Command Center dashboard (live map, zone cards, alert feed)
  M4 Incident & SOS management (raise, dispatch, track, close)

TIER 2 — DIFFERENTIATORS
  M5 Queue Breach & Audit System (tripwire events, evidence clip, review flow)
  M6 Predictive Crowd Forecasting (30/60/90-minute density forecast per zone)
  M7 Pilgrim PWA (offline-first, Marathi, navigation, facility finder)

TIER 3 — DEPTH (build only if Tier 1+2 are stable)
  M8 Palkhi / Dindi live tracking and halt-town readiness
  M9 Analytics & post-Wari reporting
  M10 IVR/SMS fallback channel for feature-phone pilgrims
      [AMENDED A-1] NOT BUILT. Tier 3, never reached — and an IVR line is the
      one item on this list that genuinely does require a paid account, so it
      is also the one deliberately left for a deployment to add.

Explicitly OUT OF SCOPE: facial recognition, individual identity tracking,
payment gateway, native mobile apps, hardware procurement.
```

---

## SECTION 4 — FUNCTIONAL SPEC: MODULE BY MODULE

### M1 — Smart Darshan Pass

```
Build a dynamic, capacity-aware darshan pass system.

DATA IN: temple throughput capacity (darshan/hour, configurable, default 6000),
current live queue length, current zone density, historical hourly demand curve.

CORE LOGIC (write this as a pure, unit-tested service — not inside a route):
  - Day is divided into 30-minute slots from 04:00 to 23:00.
  - Each slot has: capacity, booked_count, walk_in_reserve (default 25% of
    capacity, never bookable online — protects pilgrims without smartphones).
  - available = capacity - booked_count - walk_in_reserve
  - A pass is issued with: slot_start, slot_end, gate_assignment, QR token.
  - DYNAMIC RESLOTTING: a background job runs every 5 minutes. If actual
    throughput in the last 30 min deviates >20% from planned, shift all
    downstream unfulfilled passes by the computed delta and push a
    notification. Never shift a pass EARLIER without explicit user opt-in
    (people travel; an early slot they cannot reach is a false promise).
  - Group booking: 1 pass covers up to 6 members, one QR, one scan.
  - No-show handling: pass expires 45 minutes after slot_end and its capacity
    is released back to the pool.

QR TOKEN: signed JWT payload {pass_id, slot, group_size, issued_at}, HMAC
signature, rotating every 60s via TOTP-style counter so screenshots shared on
WhatsApp cannot be reused. Offline scan verification must work — the scanner
app caches the day's public key and validates without network.

ENDPOINTS:
  GET  /api/v1/slots?date=            -> availability grid
  POST /api/v1/passes                 -> book (phone OTP verified)
  GET  /api/v1/passes/{id}            -> status + live ETA
  POST /api/v1/passes/{id}/cancel
  POST /api/v1/checkpoints/scan       -> volunteer scans QR, returns
                                         ALLOW / EARLY / EXPIRED / INVALID
ACCEPTANCE:
  - 10,000 passes bookable without oversubscribing any slot (test it).
  - Reslotting test: force a 40% throughput drop, assert all downstream passes
    shift and notifications queue.
  - Scanned QR cannot be scanned twice.
```

### M2 — AI Crowd Intelligence Engine

```
Build the crowd analytics service as a SEPARATE Python service (FastAPI +
worker), not inside the main API. It must be independently restartable.

PIPELINE PER CAMERA STREAM:
  1. Ingest: RTSP feed OR uploaded video file OR simulation generator.
     Sample at 2 FPS — 30 FPS is wasted compute for crowd density.
  2. Detect: YOLOv8 (person class only). In dense crowds, head-detection
     weights outperform full-body; make the model path configurable.
  3. Track: ByteTrack/DeepSORT for movement vectors — but discard track IDs
     after computing flow. Do not persist per-person trajectories.
  4. Compute per zone, per 10-second window:
       - person_count
       - density = count / zone_area_m2  (people per m²)
       - flow_vector (dominant direction + magnitude, px/s -> m/s calibrated)
       - stagnation_index: % of tracks with near-zero velocity over 60s
       - counter-flow ratio: fraction of movement opposing dominant direction
  5. Classify density level (use published crowd-safety thresholds):
       SAFE      < 2.0 p/m²
       MODERATE  2.0 - 3.5
       HIGH      3.5 - 5.0    -> operator alert
       CRITICAL  > 5.0        -> immediate alert + suggested action
     ALSO alert on: stagnation_index > 0.7 with density > 3.0 (a stalled dense
     crowd is the crush precursor, not raw density alone), and counter-flow
     ratio > 0.35 (opposing streams = turbulence).
  6. Publish: write aggregate to Redis (TTL 5 min) + Postgres time-series
     (1-min rollups) + emit WebSocket event.

CAMERA CALIBRATION: each zone needs a homography (4 image points -> 4 real
world points) so pixel counts convert to m². Build a small admin UI to click
those 4 points on a still frame. Without this the density number is fiction.

SIMULATION MODE (REQUIRED — you will not have live temple CCTV):
  Build `sim_engine.py` that generates realistic zone telemetry: baseline
  diurnal curve, Ekadashi surge, Palkhi arrival spikes, random incidents.
  A single env flag CROWD_SOURCE=live|video|sim switches the source. The rest
  of the system cannot tell the difference. This is what you demo.

ACCEPTANCE: run a sample crowd video, produce a heat map overlay and a density
time series, and trigger a CRITICAL alert when the threshold is crossed.
```

### M3 — Temple Command Center (Admin Web App)

```
Single-screen operations console. Layout, in priority order:

LEFT RAIL (persistent): live map of temple complex + Wari corridor.
  - Zone polygons colour-coded by density level
  - Camera markers with online/offline/degraded status
  - Active incident pins
  - Palkhi position marker
CENTER: the map, or a camera grid, or the forecast view (tabbed).
RIGHT RAIL: prioritised alert feed. Newest critical at top. Each alert card:
  severity, zone, metric that triggered it, confidence, age, recommended
  action, and two buttons — Acknowledge, Dispatch.
TOP STRIP: 6 KPIs — pilgrims in complex, current wait time, darshan/hour
  (actual vs target), open incidents, breach events pending review, system
  health (cameras online / total).

RULES:
  - Nothing auto-refreshes the whole page. WebSocket patches state in place.
  - An unacknowledged CRITICAL alert escalates visually after 60s and pages the
    next role in the chain after 180s. Log every escalation.
  - Every number on screen shows its "as of" timestamp on hover. Stale data
    (>90s) renders greyed with a stale badge. Operators must never act on a
    number they think is live but isn't.
  - Add a "What changed in the last 15 minutes" strip — operators returning
    from a walkabout need catch-up, not a full re-read.
```

### M4 — Emergency & Incident Management

```
INCIDENT LIFECYCLE: reported -> triaged -> dispatched -> on_scene -> resolved
                    -> closed (with outcome note)

TYPES: medical, missing_person, crowd_crush_risk, fire, structural, lost_item,
       facility_failure, security, other

SOURCES: pilgrim SOS (app), volunteer report, AI-generated alert, control room
         manual entry, phone call logged by operator.

SOS FLOW (pilgrim side):
  - One large button, works with 3 taps max, works offline (queues and fires on
    reconnect), captures GPS + last known zone + optional 10s audio note.
  - Sends SMS fallback to a control-room number if the app cannot reach the API.
    [AMENDED A-1] Implemented, and without a gateway account: the app hands the
    message to the DEVICE's own SMS app, addressed to CONTROL_ROOM_SMS_NUMBER
    (served on /pilgrim/essentials so it survives going offline). Blank is a
    supported state — the app then says no number is configured rather than
    rendering a button that dials nothing.
  - Shows the pilgrim a confirmation with a reference number and, if available,
    the ETA of the nearest responder. Never leave them staring at a spinner.

DISPATCH LOGIC:
  - Auto-suggest the nearest available responder unit by type and haversine
    distance, but a human confirms. No auto-dispatch.
  - SLA timers per severity (critical 3 min, high 10 min, normal 30 min) with
    breach highlighting.

MISSING PERSON (add this — it is the single most common Wari incident and the
PDR does not cover it):
  - Report with photo, name, age, last-seen zone, contact number, language.
  - Broadcasts to volunteer app in surrounding zones and to announcement desks.
  - Reunification is logged and closes the case. Photos auto-purge in 30 days.
```

### M5 — Queue Breach & Audit System

```
This is the most sensitive module in the product. Build it carefully.

WHAT IT DOES: detects and records unauthorised entries through restricted
access points, so that queue-skipping is documented rather than argued about.

DETECTION:
  - "Digital tripwire": operator draws a line/polygon on a camera frame at each
    restricted gate. Directional crossing detection (entry vs exit).
  - Event = person track crosses tripwire in the restricted direction during a
    window when that gate is flagged closed.
  - Cross-reference: was a valid pass scanned at this gate within ±30 seconds?
    If yes -> authorised, no event. If no -> breach event.
  - Automatically retain the 10-second clip (5s pre-roll, 5s post) as evidence.

WHAT IT MUST NOT DO — HARD CONSTRAINTS:
  - No facial recognition. No identity inference. No matching a person across
    cameras. The system reports "an unauthorised entry occurred at Gate 3 at
    14:22", never "who" — identification is a human, lawful process.
  - No public exposure. Breach records are visible only to Security Officer and
    Administrator roles, never on any pilgrim-facing surface.
  - Every breach event requires human review before it counts. Reviewer marks
    Verified / False Positive / Authorised-with-reason. AI output alone is
    never a finding.

INTEGRITY (this is what makes it useful against pressure):
  - Evidence clips are hashed (SHA-256) on write; hash chained to the previous
    event so a deleted or altered record breaks the chain visibly.
  - Deletion is impossible for any role except System Admin, requires a written
    reason, and the deletion itself is logged permanently.
  - Daily summary report: breach count by gate and hour, review status, no
    personal data. This is the artefact the trust takes to a governance meeting.

ACCESS CONTROL: clip playback requires re-authentication and logs every view.
RETENTION: 90 days default, configurable, auto-purge job with a purge log.
  [AMENDED A-3] Removal by a System Admin is implemented as REDACTION, not
  deletion: the clip goes with a mandatory written reason, and the record and
  its chain hash remain. Deleting the row would break the chain at exactly the
  point somebody wanted it broken — indistinguishable from the tampering the
  chain exists to detect. The purge log carries a row on EVERY run, including
  the ones that purge nothing, because a governance review asks "has this been
  running", not "what was deleted".
```

### M6 — Predictive Crowd Forecasting

```
Forecast density per zone at t+30, t+60, t+90 minutes.

FEATURES: last 6h density series per zone, inbound flow from upstream zones,
active pass bookings for upcoming slots, time of day, day of Wari calendar,
Palkhi ETA, weather (rain compresses crowds into covered areas — this matters),
scheduled aarti/ritual times.

MODEL: start with a gradient-boosted regressor (LightGBM) per zone on rolling
windows. Do NOT start with an LSTM — you have no historical data yet and will
overfit noise. Ship the boosted model with a documented MAE.

COLD START (you have no history): seed with the simulation engine's generated
season, and clearly label forecasts as "model trained on simulated data" in
the UI until real data exists. Never hide the provenance of a prediction.

OUTPUT CONTRACT: {zone, horizon_min, predicted_density, confidence_interval,
model_version, trained_on}. The UI renders the interval band, not just a line.

RECOMMENDATION LAYER: when a forecast crosses HIGH, generate a concrete
suggested action from a rules table, e.g.
  "Zone C forecast 4.2 p/m² at 15:30. Suggested: hold Gate 2 intake for 12 min,
   divert via Corridor B, pause slot 15:00-15:30 pass releases."
An LLM may phrase the recommendation, but the trigger and the numbers come from
the rules table. Never let a language model invent a safety instruction.
```

### M7 — Pilgrim PWA

```
Progressive Web App. Installable. Marathi default. Must work on a 2016 Android
phone on 2G.

SCREENS (that is all — resist adding more):
  1. Home: my pass status + live wait + one-tap SOS + crowd status of my zone
  2. Pass: book / view QR / group members / reslot notification
  3. Map: my position, safe route to gate, facilities (toilet, water, medical,
     food, rest zone, lost-and-found desk), crowd colour overlay
  4. Alerts: location-based advisories, Palkhi position, ritual timings
  5. Help: emergency numbers, missing person report, language switch

OFFLINE REQUIREMENTS (non-negotiable):
  - Service worker caches: map tiles for the Pandharpur + corridor bounding
    box, facility list, the pass QR, emergency numbers, and the last known
    crowd snapshot with its timestamp.
  - Offline UI shows a clear "last updated 14 min ago" banner. Never render
    stale crowd data as if it were live — that is how people walk into a crush.
  - Actions taken offline (SOS, reports) queue in IndexedDB and sync on
    reconnect with a visible pending state.

PERFORMANCE BUDGET: first load < 200 KB gzipped JS, LCP < 2.5s on 3G,
fully usable without JS for the pass QR (server-rendered fallback page).

ACCESSIBILITY: minimum 18px base type, 4.5:1 contrast, 48px touch targets,
full flow completable one-handed, screen-reader labelled in Marathi. Your user
is often 65+ years old, walking, and tired.
```

### M8 — Palkhi & Dindi Tracking (enhancement beyond the PDR)

```
The Wari is a 250 km, 18-day walking procession — the temple is only the last
day. Track it.

  - Register each Dindi (group) with leader contact, expected head count, and
    planned halt schedule.
  - Palkhi live position via volunteer phone GPS pings (one designated device
    per Dindi, 60s interval, battery-aware).
  - Halt-town readiness board: expected arrival, expected head count, water
    points, sanitation units, medical camp, and current status per town.
  - Deviation alert: if actual pace deviates >45 min from schedule, notify the
    next halt town so arrangements shift with it.
This turns the product from "a temple tool" into "a Wari tool" — and it is the
part no competing entry will have built.
```

---

## SECTION 5 — ENHANCEMENTS BEYOND THE PDR (state these as deliberate additions)

```
The PDR is sound. These additions close its gaps — include them and be able to
justify each one in one sentence:

E1. Walk-in reserve quota (25%) — protects pilgrims without smartphones from
    being priced out of darshan by digitally literate ones. Equity by design.
E2. Missing-person module — the highest-frequency real incident at Wari.
E3. Stagnation + counter-flow metrics — crush events correlate with stalled and
    opposing flow, not with raw density alone. Density-only alerts fire late.
E4. Hash-chained evidence ledger — makes the breach audit resistant to the
    exact political pressure the problem statement describes.
E5. Offline-first PWA + SMS/IVR fallback — a system that needs 4G is a system
    that fails during the Wari, when networks are saturated.
E6. Simulation engine — lets the system be validated, tuned, and demonstrated
    before a single camera is installed, and enables tabletop drills.
E7. Palkhi/Dindi tracking — extends coverage from the 1-day peak to the full
    18-day event.
E8. Graceful degradation contract — every module has a defined manual mode.
E9. Zero-biometric architecture — DPDP Act 2023 compliant by construction,
    which is what will let a temple trust actually deploy this.
E10. Post-event analytics pack — the trust's real annual need: an evidence
    report for planning next year's Wari.
```

---

## SECTION 6 — SYSTEM ARCHITECTURE

```
                 ┌──────────────┐    ┌──────────────┐
   CCTV / RTSP ─►│  AI Service  │    │ Pilgrim PWA  │  Volunteer PWA
   or Sim Engine │  (FastAPI +  │    │  (React TS)  │  Admin Console
                 │   worker)    │    └──────┬───────┘        │
                 └──────┬───────┘           │                │
                        │ events            └────────┬───────┘
                        ▼                            ▼
                 ┌────────────────────────────────────────┐
                 │        Core API (FastAPI)              │
                 │  auth · passes · incidents · zones ·   │
                 │  breach review · analytics · WS hub    │
                 └───┬──────────────┬─────────────┬───────┘
                     ▼              ▼             ▼
              PostgreSQL      Redis           Object store
              (+TimescaleDB)  (cache,         (evidence clips,
               source of      pub/sub,         hashed, encrypted)
               truth          queues)

  Async jobs: Celery/RQ — reslotting, forecasting, notifications, purges.
    [AMENDED A-2] Built as a plain asyncio interval scheduler with Redis
    single-runner locks (app/workers/scheduler.py). Celery adds a broker and a
    worker image for what is a set of timers; the jobs are plain callables, so
    registering them as Celery tasks later is a decorator, not a rewrite.
  Notifications: FCM web push + SMS gateway (MSG91/Gupshup) + IVR hook.
    [AMENDED A-1] NOT USED. No push key, no SMS gateway account, no IVR.
    Delivery is in-app + WebSocket; the SOS fallback opens the device's own SMS
    app to a control-room number, which needs no gateway. See the Amendment Log.

BOUNDARIES: the AI service NEVER writes to the core database directly. It
publishes events. The core API owns all state. This is what lets you restart
the AI service during an event without losing a single pass or incident.
```

---

## SECTION 7 — TECH STACK (locked — do not substitute)

```
Backend:    Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic
Database:   PostgreSQL 16 + TimescaleDB (density time-series) + PostGIS (zones)
Cache/Bus:  Redis 7 (cache, pub/sub, rate limit, Celery broker)
AI/CV:      YOLOv8 (Ultralytics), OpenCV, ByteTrack/DeepSORT, LightGBM
LLM:        Gemini API — used ONLY for: multilingual pilgrim assistant replies,
            natural-language phrasing of rule-generated recommendations, and
            report summarisation. Never for safety decisions.
Frontend:   React 18 + TypeScript (strict) + Vite, TanStack Query, Zustand,
            Tailwind, MapLibre GL (with OSM tiles; Google Maps only for
            routing if needed — MapLibre lets you self-host tiles offline)
            [AMENDED A-1] Shipped with NO tiles at all. The console draws our
            own zone polygons on a flat background: a control room during the
            Wari is exactly where a map that blocks on a tile CDN fails, and
            everything an operator needs (zone, colour, label) is in our own
            database. Set VITE_MAP_STYLE to add a basemap you can reach.
PWA:        Workbox service worker, IndexedDB (Dexie)
i18n:       i18next — mr (default), hi, en. All strings externalised from day 1.
Realtime:   WebSocket (FastAPI native) + Redis pub/sub fan-out
Infra:      Docker Compose (dev), single VPS or AWS ECS (prod), Nginx, GitHub
            Actions CI, Sentry, Prometheus + Grafana
            [AMENDED A-1] Sentry NOT used — it is an external account, and the
            errors it would carry are pilgrim-facing traces. Replaced by
            structured JSON logs carrying the same `trace_id` the client sees in
            its error envelope, so an operator can read a trace id off a
            screenshot and you can find the request. Prometheus + Grafana are
            self-hosted behind `--profile observability`.
Testing:    pytest + httpx (backend), Vitest + Playwright (frontend)
```

---

## SECTION 8 — DATA MODEL

```
Generate SQLAlchemy models + Alembic migration for:

users(id, phone, name, role, language, password_hash?, mfa_secret?, is_active,
      created_at)
zones(id, name, name_mr, geom POLYGON, area_m2, capacity_persons, zone_type,
      parent_zone_id)
cameras(id, zone_id, name, stream_url, homography_matrix JSONB, status,
        last_heartbeat_at, is_tripwire_enabled)
tripwires(id, camera_id, name, geometry JSONB, restricted_direction,
          active_schedule JSONB)
density_readings(time TIMESTAMPTZ, zone_id, person_count, density,
                 flow_dx, flow_dy, stagnation_index, counterflow_ratio,
                 confidence, source)            -- TimescaleDB hypertable
slots(id, date, start_time, end_time, capacity, booked_count, walkin_reserve,
      status, gate_id)
passes(id, slot_id, holder_phone_hash, group_size, qr_secret, status,
       issued_at, scanned_at, scanned_by, original_slot_id, reslot_count)
incidents(id, type, severity, status, zone_id, location POINT, reported_by,
          source, description, sla_due_at, created_at, resolved_at,
          outcome_note)
incident_events(id, incident_id, actor_id, action, note, created_at)
responders(id, user_id, unit_type, current_location POINT, status,
           last_ping_at)
breach_events(id, tripwire_id, camera_id, occurred_at, clip_uri, clip_sha256,
              prev_hash, chain_hash, review_status, reviewed_by, review_reason,
              reviewed_at)
alerts(id, type, severity, zone_id, trigger_metric, trigger_value, confidence,
       recommended_action, status, acknowledged_by, acknowledged_at,
       escalated_at)
dindis(id, name, leader_name, leader_phone, expected_count, route_id)
dindi_pings(time, dindi_id, location POINT, battery, speed)
halt_towns(id, name, geom, expected_arrival, water_points, sanitation_units,
           medical_camps, readiness_status)
facilities(id, zone_id, type, name, name_mr, location POINT, status, capacity)
missing_persons(id, name, age, photo_uri, last_seen_zone_id, contact_phone,
                status, reported_at, resolved_at, purge_after)
audit_log(id, actor_id, action, target_type, target_id, meta JSONB, ip,
          created_at)   -- append-only, no UPDATE/DELETE grants

INDEX RULES: time-series on (zone_id, time DESC); passes on (slot_id, status);
incidents on (status, severity, created_at DESC); GIST on all geom columns.
PII RULE: store holder_phone_hash (HMAC), never the raw number, except in an
encrypted contact table accessible only for active-pass notification delivery.
```

---

## SECTION 9 — API CONTRACT

```
Version everything under /api/v1. Standard error envelope:
  { "error": {"code": "SLOT_FULL", "message": "...", "message_mr": "...",
              "details": {...}, "trace_id": "..."} }

AUTH        POST   /auth/otp/request        POST /auth/otp/verify
            POST   /auth/login              POST /auth/refresh
PASSES      GET    /slots                   POST /passes
            GET    /passes/{id}             POST /passes/{id}/cancel
            POST   /checkpoints/scan
CROWD       GET    /zones                   GET  /zones/{id}/density
            GET    /density/live            GET  /density/history
            GET    /forecast?zone=&horizon=
ALERTS      GET    /alerts                  POST /alerts/{id}/acknowledge
INCIDENTS   POST   /incidents               GET  /incidents
            PATCH  /incidents/{id}          POST /incidents/{id}/dispatch
            POST   /sos
BREACH      GET    /breaches                GET  /breaches/{id}/clip (audited)
            POST   /breaches/{id}/review
PALKHI      GET    /dindis                  POST /dindis/{id}/ping
            GET    /halt-towns
ADMIN       CRUD   /cameras /zones /tripwires /users /config
ANALYTICS   GET    /analytics/summary       GET  /analytics/export?format=csv
HEALTH      GET    /health  /health/deep  /metrics

WEBSOCKET  /ws?token=   channels: density, alerts, incidents, passes:{id}
  Event shape: {type, channel, payload, server_time, seq}
  Client must handle: out-of-order seq, reconnect with last_seq replay,
  and a heartbeat every 20s with automatic backoff reconnect.

RATE LIMITS: OTP 3/hour/phone, pass booking 5/day/phone, SOS 3/10min (but
never hard-block SOS — degrade to SMS instead).
```

---

## SECTION 10 — FRONTEND & DESIGN DIRECTION

```
Two apps, one design system, deliberately different densities.

DESIGN LANGUAGE — derive it from the Wari itself, not from a dashboard
template. Reference points: the saffron of the Palkhi, the deep indigo of
Vitthal, Devanagari letterforms, the abir-gulal palette.

  Tokens:
    --saffron:   #E8622B   (primary action, Palkhi)
    --indigo:    #1B2A5E   (surface dark, Vitthal)
    --tulsi:     #2E7D5B   (safe state)
    --amber:     #E0A106   (moderate / warning)
    --sindoor:   #C42B1C   (critical)
    --paper:     #FAF7F2   (light surface)
    --ink:       #16181D   (text)

  Type: a Devanagari-first pairing — Mukta or Baloo Bhaijaan 2 for Marathi
  headings, Inter or IBM Plex Sans for Latin/data. Test every screen with real
  Marathi strings, which run 20-40% longer than English. Layouts that only fit
  English are broken layouts.

  Density: Admin console is information-dense, 14px base, tabular numerals,
  monospaced metrics. Pilgrim PWA is the opposite — 18px base, one primary
  action per screen, generous spacing, no jargon.

  Signature element: the live density map is the product. Give it real craft —
  smooth colour interpolation between zone states, flow arrows that show which
  way the crowd is actually moving, and a time-scrubber that lets an operator
  replay the last hour. That replay scrubber is the thing judges will remember.

  Motion: restraint. Alerts pulse once on arrival, then hold. Map transitions
  ease at 200ms. Respect prefers-reduced-motion. Nothing decorative animates —
  in a control room, movement means something happened.

  Empty and failure states: write them properly. "No cameras online in Zone C —
  switch to manual density entry" is useful. "No data" is not.
```

---

## SECTION 11 — NON-FUNCTIONAL REQUIREMENTS

```
SCALE TARGET:   50,000 concurrent PWA sessions, 200 concurrent operator
                sessions, 40 camera streams, 5,000 pass bookings/minute at
                release windows.
LATENCY:        API p95 < 300ms; density event to operator screen < 3s;
                SOS acknowledgement < 1s.
AVAILABILITY:   99.9% during Wari window; planned zero-downtime deploys only
                outside 03:00-00:00.
LOAD TEST:      Locust script simulating a pass-release stampede — 20,000
                requests in 60 seconds. Must queue, not collapse.
DEGRADATION:    Define and test the manual mode for every module. Ship a
                one-page runbook: "if the AI service is down, do this."
OBSERVABILITY:  Structured JSON logs with trace_id, Prometheus metrics per
                endpoint and per zone pipeline, Grafana board, Sentry on both
                apps, alerting on camera heartbeat loss > 2 min.
                [AMENDED A-1] Delivered without Sentry (see A-1). Everything
                else is built: per-endpoint AND per-zone-pipeline metrics —
                which answer different questions and can disagree, since a
                green API over forty dark cameras is the failure the split
                exists to expose. The camera-heartbeat alert is
                `CameraHeartbeatLost` in infra/prometheus/alerts.yml.
BACKUP:         Postgres PITR, hourly snapshot during Wari, restore drill
                documented and actually performed once.
```

---

## SECTION 12 — SECURITY, PRIVACY & COMPLIANCE

```
Treat this as a regulated deployment. India's DPDP Act 2023 applies.

HARD ARCHITECTURAL CONSTRAINTS
  - No facial recognition, no gait analysis, no biometric templates, ever.
  - No cross-camera re-identification of individuals.
  - Track IDs are ephemeral, in-memory, and discarded after aggregation.
  - Video frames are processed in memory and not persisted, EXCEPT the
    10-second breach evidence clip, which is a deliberate, narrow exception
    with its own controls.

DATA MINIMISATION
  - Phone numbers stored as HMAC hashes; raw numbers only in an encrypted
    table with a 30-day TTL after the pass is used.
  - Location pings from pilgrims are opt-in, coarse (100m grid), and never
    stored against an identity.
  - Missing-person photos auto-purge 30 days after case closure.

CONSENT & NOTICE
  - Clear, Marathi-first consent screen before any location or notification
    permission, with a plain explanation of what is collected and why.
  - Physical signage text (generate it) for CCTV zones, in Marathi, Hindi and
    English, as required for lawful monitoring.

ACCESS CONTROL
  - MFA for Administrator and System Admin.
  - Evidence clip access: re-authentication, purpose note, and an access log
    entry visible to the Administrator.
  - Append-only audit log with no delete grant at the database role level.

APPLICATION SECURITY
  - Argon2id password hashing, JWT rotation, refresh-token reuse detection
  - Strict CORS, CSP, HSTS, secure/httpOnly/sameSite cookies
  - Parameterised queries only; Pydantic validation on every input
  - Rate limiting and OTP throttling; signed and expiring media URLs
  - Secrets in environment/secret manager, never in the repo
  - Dependency scanning in CI; run `bandit` and `npm audit` on every PR

ETHICS NOTE TO CARRY INTO THE PITCH: the breach system's purpose is
accountability of a process, not surveillance of people. It records that a
rule was broken at a gate, and leaves the question of who to lawful human
process. Say this explicitly — it is a strength, not a caveat.
```

---

## SECTION 13 — AI ASSISTANT (Gemini) SCOPE & GUARDRAILS

```
Build a pilgrim assistant with a deliberately narrow contract.

MAY DO:
  - Answer questions in Marathi/Hindi/English about darshan timings, pass
    status, facility locations, route guidance, Palkhi schedule, ritual info.
  - Convert a rules-engine recommendation into clear operator-facing language.
  - Summarise a shift's incidents into a handover note.

MAY NOT DO:
  - Decide or alter a crowd-safety action, a density threshold, or a dispatch.
  - Issue, cancel, or reslot a pass.
  - Assess a medical emergency.
  - Answer from its own knowledge about live state. Every factual claim about
    crowd, wait time, or pass status must come from a tool call to your API.

IMPLEMENTATION: function-calling with a fixed tool list (get_pass_status,
get_zone_crowd, find_nearest_facility, get_schedule, raise_sos_draft).
System prompt states: if a tool returns nothing, say you don't know and offer
the helpline number. Never fabricate a wait time — a wrong number here sends a
person into a crowd at the wrong moment.
Log every assistant turn with its tool calls for review.
```

---

## SECTION 14 — REPOSITORY STRUCTURE

```
wariverse/
├── docker-compose.yml
├── .env.example
├── README.md                    # setup in <10 commands
├── docs/
│   ├── architecture.md  api.md  runbook.md  privacy-dpia.md  demo-script.md
├── services/
│   ├── core-api/
│   │   ├── app/{api,core,models,schemas,services,workers,tests}
│   │   └── alembic/
│   ├── ai-engine/
│   │   ├── app/{pipeline,detectors,tracking,zones,tripwire,forecast,sim}
│   │   └── models/            # weights, gitignored
│   └── notifier/              # SMS/push/IVR fan-out
│                              # [AMENDED A-1] NOT BUILT. No gateway account is
│                              # used, so there was nothing to fan out to.
├── apps/
│   ├── admin-console/         # React TS
│   ├── pilgrim-pwa/           # React TS + Workbox
│   └── volunteer-app/         # React TS, scanner-first
├── packages/
│   ├── ui/                    # shared design system
│   └── api-client/            # generated from OpenAPI
└── infra/  (nginx, grafana, prometheus, github workflows, locust)
```

---

## SECTION 15 — BUILD ORDER (feed sections in this sequence)

```
PHASE 1  Foundation      Sections 0,1,2,7,8,14 -> repo, docker, DB, auth, RBAC,
                          audit log, health checks, CI
PHASE 2  Pass system     Section 4/M1 + 9      -> slots, booking, QR, scanner,
                          reslotting job, tests
PHASE 3  Crowd engine    Section 4/M2          -> sim engine first, then YOLO
                          pipeline, zones, calibration UI, WS events
PHASE 4  Command center  Section 4/M3 + 10     -> live map, alert feed, KPIs,
                          replay scrubber
PHASE 5  Incidents       Section 4/M4          -> SOS, dispatch, SLA, missing
                          person
PHASE 6  Breach audit    Section 4/M5 + 12     -> tripwires, evidence chain,
                          review flow, access logging
PHASE 7  Pilgrim PWA     Section 4/M7          -> offline shell, pass, map,
                          alerts, i18n, a11y
PHASE 8  Forecasting     Section 4/M6          -> LightGBM, intervals,
                          recommendation rules
PHASE 9  Palkhi + AI     Sections 4/M8, 13     -> Dindi tracking, assistant
PHASE 10 Harden          Sections 11,12        -> load test, runbook, DPIA,
                          observability, backup drill

At the end of every phase: it runs with `docker compose up`, tests pass, and
the README tells a new person how to see it working. A phase is not done
because the code exists.
```

---

## SECTION 16 — DEMO & VALIDATION PLAN

```
Build a scripted 5-minute demo that runs off the simulation engine:

  T+0:00  Pilgrim books a Smart Darshan Pass in Marathi on a phone viewport.
          Shows honest wait time: "your slot 15:30, current wait 4h 20m".
  T+0:45  Cut to Command Center. Normal state, all zones green, 38/40 cameras
          online, current throughput 5,800/hr vs 6,000 target.
  T+1:30  Simulation injects a Palkhi arrival surge. Zone C density climbs.
          Stagnation index rises before density hits CRITICAL — the alert fires
          on stagnation first. Make this beat explicit; it is the technical
          insight of the whole project.
  T+2:15  Forecast panel: Zone C predicted 4.6 p/m² at +30 min, with interval.
          Recommendation card appears with a concrete diversion action.
  T+2:45  Operator acts. Downstream passes auto-reslot; the pilgrim's phone
          buzzes with a revised slot. Show both screens side by side.
  T+3:30  Restricted Gate 3 tripwire fires. Breach event appears with clip and
          chain hash. Reviewer marks it Verified. Show the tamper-evident
          ledger view.
  T+4:15  Pilgrim raises SOS with the phone in airplane mode — it queues,
          reconnects, dispatches, responder ETA appears.
  T+4:45  Kill the AI service container live. Dashboard shows degraded mode,
          passes and incidents keep working. End on that.

That last 15 seconds wins the round. Anyone can demo a happy path.

VALIDATION CHECKLIST
  [ ] 20,000-request booking stampede handled without oversubscription
  [ ] Density thresholds validated against a published crowd-safety reference
  [ ] Marathi UI reviewed by a native speaker; no truncation, no machine-
      translation artefacts
  [ ] PWA usable on a 3-year-old Android over throttled 3G
  [ ] Breach evidence chain verified after a simulated tampering attempt
  [ ] Full offline flow tested with the network physically off
  [ ] Runbook followed by someone who did not write the code
```

---

## SECTION 17 — DEFINITION OF DONE

```
A feature is done when ALL of these are true:

  1. It works end to end against real or simulated data, not stubs.
  2. It has typed schemas and validated inputs on both ends.
  3. It has tests for its failure paths, not only its happy path.
  4. It has a defined behaviour when its dependency is down.
  5. Its strings are in the i18n files, Marathi included.
  6. Its numbers carry a timestamp and a source in the UI.
  7. Its privileged actions write to the audit log.
  8. Someone else can run it from the README in under ten minutes.

Report progress as: what runs, what does not, and what you chose to defer and
why. Do not report a module complete because its files exist.
```

---

## SECTION 18 — WHAT TO SAY IN THE PITCH (one paragraph, memorise it)

```
"Every crowd-management entry will show you a heat map. WariVerse is built on
three things they will not have. First, we alert on stagnation and counter-flow,
not just density — because crush events begin when a dense crowd stops moving,
and a density-only system alerts too late to matter. Second, our queue-integrity
module is a tamper-evident ledger with hash-chained evidence and zero facial
recognition — it makes the process accountable without making pilgrims
surveilled, which is the only version a temple trust can lawfully deploy under
the DPDP Act. Third, the whole thing degrades gracefully: passes, alerts, and
incidents keep working when the AI service dies and when the network dies, which
during Ashadhi Ekadashi is a certainty, not a risk. We built for the day it
breaks, not the day it demos."
```
