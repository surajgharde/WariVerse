# WariVerse

AI-powered crowd intelligence and pilgrim management for the **Pandharpur Wari**
and the Shri Vitthal-Rukmini Temple, Maharashtra.

Ten to fifteen lakh pilgrims converge on Pandharpur for Ashadhi Ekadashi.
Darshan queues run 18–24 hours. The Palkhi walks 250 km over 18 days. WariVerse
exists to make that safer, fairer and legible — without surveilling anyone.

> **Privacy is architectural, not a setting.** No facial recognition, no gait
> analysis, no biometric templates, no cross-camera re-identification, no
> individual pilgrim tracking. Crowd analytics are anonymous and aggregate by
> construction. See [Section 12 of the spec](WariVerse_Development_Prompt.md) and
> `docs/privacy-dpia.md`.

---

## Run it

Requires Docker and Docker Compose. Ten commands, start to working API.

```bash
git clone <repo> && cd wariverse

cp .env.example .env                      # 1. create local config
python - <<'PY'                           # 2. generate real dev secrets
import base64, secrets, pathlib
p = pathlib.Path(".env"); t = p.read_text()
t = t.replace("dev-only-jwt-secret-change-me-0000000000000000", secrets.token_urlsafe(48))
t = t.replace("dev-only-phone-hash-secret-change-me-000000", secrets.token_urlsafe(48))
t = t.replace("dev-only-ai-service-token-change-me", secrets.token_urlsafe(24))
t = t.replace("CONTACT_ENCRYPTION_KEY=", "CONTACT_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
p.write_text(t)
PY

docker compose up -d db redis             # 3. Postgres (Timescale+PostGIS) + Redis
docker compose up -d --build core-api     # 4. API; migrations run on boot

curl localhost:8000/health/deep           # 5. everything should read "ok"
docker compose exec core-api python scripts/seed_dev.py   # 6. demo data
open http://localhost:8000/docs           # 7. interactive API
```

Then try the pilgrim sign-in end to end — no SMS gateway needed in development,
because `OTP_DEBUG_ECHO=true` returns the code:

```bash
# 8. request a code
curl -s -X POST localhost:8000/api/v1/auth/otp/request \
     -H 'content-type: application/json' -d '{"phone":"9876543210"}'
# -> {"sent":true,"expires_in":300,"debug_code":"483920"}

# 9. verify it and get tokens
curl -s -X POST localhost:8000/api/v1/auth/otp/verify \
     -H 'content-type: application/json' \
     -d '{"phone":"9876543210","code":"483920","name":"रुक्मिणी","language":"mr"}'

# 10. use the access token
curl -s localhost:8000/api/v1/auth/me -H "authorization: Bearer <access_token>"
```

`OTP_DEBUG_ECHO` is refused in production — the app will not boot with it on.

### Seeded staff accounts

`scripts/seed_dev.py` creates one account per role, all with the password
`wari-demo-2026-change-me`:

| Phone        | Role               |
| ------------ | ------------------ |
| 9000000001   | `system_admin`     |
| 9000000002   | `administrator`    |
| 9000000003   | `security_officer` |
| 9000000004   | `volunteer`        |
| 9000000005   | `responder`        |

Sign in at `POST /api/v1/auth/login`. Administrator and System Admin get an MFA
challenge instead of tokens — the seed enrols them with the fixed development
secret in `DEV_MFA_SECRET`, so any TOTP app will produce a working code. (Real
deployments enrol per user at `/auth/mfa/enrol`; production seeds nothing.)

**Development sign-in.** With `DEV_LOGIN_ENABLED=true` in `.env`, the admin
console's sign-in screen grows a set of one-click buttons — pick a role, no
password, no TOTP. `POST /api/v1/auth/dev-login` is what they call.

Three locks, all of which must be open: `ENVIRONMENT` must be `development`,
`DEV_LOGIN_ENABLED` must be explicitly true (it defaults to false), and
`assert_production_safe()` lists the flag so the app **refuses to boot** in
production with it on — the same treatment `OTP_DEBUG_ECHO` gets. The console
half is gated on `import.meta.env.DEV`, so it is compiled out of a production
bundle rather than merely hidden. Every dev sign-in is audited with
`dev_login: true`, so a token minted this way stays distinguishable from a real
one.

The seed also creates twelve responder units — ambulances, medical teams,
police, fire, volunteer squads and help desks — spread across the complex with a
position and a fresh ping, so `GET /incidents/{id}/dispatch-options` has
something to rank on a first run rather than demonstrating the empty-roster case.

### Tests

```bash
cd services/core-api
pip install -r requirements-dev.txt
pytest -q            # integration tests skip cleanly without db/redis running
```

With `docker compose up -d db redis` running, all tests execute; the suite
creates and migrates its own `wariverse_test` database. Set
`REQUIRE_INTEGRATION=1` (as CI does) to turn "infrastructure missing" from a
skip into a failure.

The 10,000-pass scale test takes about 90 seconds and is marked `slow`. It runs
in CI; skip it locally with `pytest -m "not slow"`.

The AI engine and the admin console have their own suites, neither of which
needs Postgres, Redis or a camera:

```bash
cd services/ai-engine  && pytest -q
cd apps/admin-console  && npm install && npm test
```

---

## What is built

| Phase | Module | State |
| ----- | ------ | ----- |
| 1 | Foundation — repo, Docker, schema, auth, RBAC, audit log, health, CI | **done** |
| 2 | M1 Smart Darshan Pass — slots, QR, scan, dynamic reslotting | **done** |
| 3 | M2 Crowd intelligence — sim engine, YOLOv8 pipeline, zone metrics | **done** |
| 4 | M3 Command Center — live map, alert feed, KPIs, replay scrubber | **done** |
| 5 | M4 Incidents & SOS — dispatch, SLA, missing persons | **done** |
| 6 | M5 Queue breach ledger — tripwires, hash chain, review flow | **done** |
| 7 | M7 Pilgrim PWA — offline-first, Marathi | not started |
| 8 | M6 Forecasting — LightGBM, intervals, recommendation rules | not started |
| 9 | M8 Palkhi tracking + Gemini assistant | not started |
| 10 | Hardening — load test, runbook, DPIA, observability | not started |

The schema in `services/core-api/app/models/` covers every table the later
phases need, so migrations do not churn as modules land.

### Phase 1 in detail

- **Auth** — phone OTP for pilgrims (accounts created on first verified code, no
  signup form), password + MFA for staff. Access tokens live 15 minutes, refresh
  tokens 7 days and rotate on every use. Replaying a superseded refresh token
  revokes the entire session family and writes an audit entry — the stolen-token
  case is handled, not hoped away.
- **RBAC** — one permission matrix in `app/core/permissions.py`. Routes ask for a
  permission, never a role. `test_permissions.py` encodes the rules the spec
  states in prose, including "only System Admin can delete breach evidence" and
  "breach records are invisible below Security Officer".
- **Audit log** — append-only, enforced by a database trigger that raises on
  `UPDATE` and `DELETE`. Credentials are scrubbed from metadata before write.
- **Breach ledger guard** — evidence fields (`chain_hash`, `prev_hash`,
  `clip_sha256`, `occurred_at`, `sequence`, `payload_snapshot`) are immutable at
  the database level. Review columns remain editable, because human review is
  the point.
- **Error envelope** — every error, including framework validation and unhandled
  crashes, returns `{"error": {code, message, message_mr, details, trace_id}}`.
  Marathi is not optional.
- **Observability** — JSON logs carrying the same `trace_id` the client sees,
  Prometheus metrics per route at `/metrics`, and `/health/deep` that reports
  *degraded* rather than dead when a non-essential dependency is down.

### Phase 2 in detail

- **Slots** — 04:00–23:00 in 30-minute windows, 38 a day, materialised on first
  request and idempotent thereafter. Capacity comes from the configured
  throughput (6,000/hour by default → 3,000 a slot).
- **Walk-in reserve** — 25% of every slot, never offered online, and *rounded
  up* so an uneven split favours the pilgrim without a smartphone. The grid
  reports the reserve explicitly rather than hiding it inside a smaller
  availability number.
- **Booking** — one pass covers up to six people with one QR and one scan.
  Capacity is claimed by a single conditional `UPDATE`, so a release-window
  stampede cannot interleave a read and a write; the `no_oversubscription`
  CHECK constraint is the backstop.
- **QR** — two layers. An Ed25519-signed envelope (pass id, slot, group size,
  gate) that any scanner verifies offline from a cached day key, plus an
  8-digit rolling code on a 60-second TOTP step that the holder's device
  computes locally. A screenshot forwarded on WhatsApp is stale within a
  minute; a forged pass never verifies at all.
- **Scanning** — `ALLOW / EARLY / EXPIRED / INVALID`, every outcome audited.
  Fifteen minutes of early tolerance, because turning an early pilgrim away
  sends them back into the crowd.
- **Reslotting** — every five minutes the scheduler compares actual gate
  throughput against plan. More than 20% short and downstream *unstarted*
  passes shift by the time it takes to clear the backlog at the observed rate,
  rounded up to whole slots and capped at three hours. Passes are never moved
  earlier without opt-in, and every move queues a Marathi-first notification
  that says why.
- **No-shows** — passes expire 45 minutes after their slot ends and their seats
  go back into the pool.

Endpoints: `GET /slots`, `POST /passes`, `GET /passes/{id}`,
`GET /passes/{id}/qr`, `POST /passes/{id}/cancel`, `POST /checkpoints/scan`,
`GET /checkpoints/day-key`, `GET /checkpoints/bundle`, plus
`POST /admin/reslot/run`, `POST /admin/passes/expire` and
`PATCH /admin/slots/{id}` for the administrator.

**Two deliberate deviations from the spec text**, both flagged rather than
silently taken:

1. Section 4/M1 asks for an "HMAC signature ... rotating every 60s" *and* a
   scanner that "caches the day's public key". Those cannot be one mechanism —
   there is no asymmetric rolling code. Authenticity is asymmetric (Ed25519,
   every scanner, no shared secret); freshness is symmetric (TOTP from the
   pass's own secret). Offline *freshness* checking needs the narrow
   `GET /checkpoints/bundle` — the next few hours of passes for one gate, not
   the whole day.
2. Section 6 names Celery/RQ for async jobs. Two five-minute timers did not
   justify a broker and a worker image, so `app/workers/scheduler.py` runs them
   with a Redis single-runner lock. The jobs in `app/workers/jobs.py` are plain
   callables; putting them behind Celery later is a decorator, not a rewrite.

### Phase 4 in detail

The command centre. Four read-only endpoints on the API and the React console
that consumes them.

```bash
docker compose up -d --build ai-engine admin-console   # engine feeds it data
open http://localhost:5173
```

Sign in as `9000000003` / `wari-demo-2026-change-me` (Security Officer — has
`crowd:view_detail`, no MFA prompt). Without the AI engine running the console
still loads and correctly shows every zone as *unknown*, which is the more
interesting screen to look at first.

- **KPI strip** — six numbers with provenance on every one: pilgrims in the
  complex, current wait, darshan/hour against plan, open incidents, breaches
  pending review, cameras online. Each carries its "as of" on hover, greys out
  with a badge past 90 seconds, and expands to show where the number came from.
- **The rule that shapes all of it** — a KPI we are not measuring is `null`, and
  renders as an em dash with an explanation. Never zero. An empty temple and a
  dead pipeline produce the same zero, and a strip that renders them identically
  is how an operator stands down during a surge. Breaches pending review is
  `null` for a structural reason — its intake lands in Phase 6 — and says so
  rather than reporting a confident `0`. Open incidents was the same until
  Phase 5 and now carries a real count; note what did not change when it did,
  which is that it still reads `null` when the board cannot be read and `0`
  only when the board is genuinely empty.
- **Wait time is honest** — queue length divided by the throughput actually
  observed at the gate, not the planned rate. The planned figure would produce a
  shorter, more comfortable number on exactly the day it is most wrong. A
  stalled gate reports *unknown*, not a zero-minute wait.
- **Live map** — zone polygons interpolated within their density band, flow
  arrows showing which way the crowd is actually moving, camera status dots. The
  interpolation is anchored at the published band boundaries so a smooth ramp
  can never make a zone read as a safer band than it is. Stale zones drop to the
  dead grey and a dashed outline rather than holding their last colour.
- **Alert feed** — worst first then newest first, each card carrying the metric
  that fired, the threshold it crossed, the confidence, and the numbered rule
  that produced the recommendation. Unacknowledged criticals escalate visually
  at 60s and show as paged at 180s — both thresholds served from
  `/command/config`, because a console counting to 60 while the server escalates
  at 90 turns a card red before anything has happened.
- **Replay scrubber** — one notch per minute over the last hour, each coloured
  by the *worst* zone in it, so the shape of a surge is legible before anyone
  drags anything. Minutes with no reading are grey and are not skipped: a gap in
  the pipeline is a fact about the evening, and a scrubber that closes the gap
  is one that edits history.
- **"What changed in the last 15 minutes"** — zone level transitions, alert
  lifecycle steps and camera status, worst first. One alert contributes a line
  per step it took, because "raised 14:02, escalated 14:03, acknowledged 14:05"
  loses its point when collapsed to one row.
- **Degradation** — the WebSocket runs a heartbeat watchdog and treats silence
  as death, since an open-but-dead socket looks exactly like a quiet temple.
  Losing it falls back to polling and says so in the header. Reconnect is
  exponential with full jitter, so two hundred consoles knocked offline by one
  blip do not return in lockstep.

Endpoints: `GET /command/kpis`, `GET /command/changes`, `GET /command/replay`,
`GET /command/config` — all behind `crowd:view_detail`, all read-only. The
console's *actions* go through the existing audited routes (`/alerts/{id}/ack`,
`/alerts/{id}/resolve`); adding a second path to them here would mean two places
to keep an audit rule correct, and the one that gets forgotten is always the
newer one.

**One deliberate deviation from the spec text.** Section 4/M3 puts the live map
in the left rail and offers it again as a centre tab. Rendering the same map
twice spends the largest area on screen duplicating something already visible,
and Section 10 is explicit that the density map is the product. So the map is
the centre's default tab at full size, and the left rail carries the zone
roster — every zone, fixed order, with its reading, staleness and flow. The
rail answers "what is the state of zone C" without a click, which is what the
rail-map was for. Incident pins and the Palkhi marker are absent because those
modules do not exist yet; they are map layers, not redesigns.

See [`apps/admin-console/README.md`](apps/admin-console/README.md) for the
front-end specifics.

### Phase 5 in detail

Emergencies. Fourteen endpoints, and one rule that shapes most of them: the
system's job during an emergency is to take the report and tell somebody, not
to be clever about it.

- **SOS is never refused.** Section 9 sets a rate limit of 3 per 10 minutes and
  then says never hard-block an SOS. Those only look contradictory until you
  notice what the limit is *for* — stopping the control room drowning in
  duplicates, not stopping a frightened person getting help. So the fourth press
  inside the window attaches to the caller's own open incident and adds
  "pressed again, 4th time" to its timeline. The phone still gets a reference
  number back. Nothing else on the route can fail either: a `zone_id` from a
  stale offline bundle is dropped and noted rather than raising, because an
  emergency rejected over a foreign key is the worst bug this system could have.
- **Every SOS is graded critical**, whatever the client asked for. A pilgrim is
  not triaging themselves, and the three-minute clock is the whole point of the
  button. An operator re-grades it down in one call once they know more.
- **An offline SOS keeps the clock it started.** A button pressed twenty minutes
  ago in a dead spot has already spent twenty minutes of its three-minute SLA,
  and the board shows it as overdue on arrival with the delay stated. A client
  clock running *fast* is clamped to now — a phone two hours ahead must not be
  able to buy itself extra SLA.
- **Nothing is auto-dispatched.** `dispatch-options` ranks; a human sends.
  Ranking is by **type fit first, then distance**: a volunteer squad standing
  next to a cardiac arrest is closer than the ambulance and is not the right
  answer, and a distance-only sort puts it top and invites the wrong click.
  Units with no known position are still offered, with the gap stated, because
  "we do not know where this unit is" is something an operator can act on.
  ETAs assume 0.7 m/s — half free-flow walking speed — and are labelled as the
  floor they are.
- **An empty suggestion list says which kind of empty it is.** Every unit busy
  and every unit more than two kilometres away are different situations, and the
  operator reaching for the radio needs to know which one they are in.
- **The SLA measures the control room, not the responder.** Only an incident
  with no unit assigned can breach; once somebody is on the way the clock stops.
  The alternative pushes operators to dispatch anybody just to stop a timer.
  Re-grading recomputes the deadline from the *original* report time, so
  re-grading a late incident up shows the breach that was already there rather
  than handing it a fresh three minutes.
- **Closing requires saying what was done.** `outcome_note` is mandatory to
  reach `closed`. A closed incident with no record of what happened is the one
  an inquiry will ask about.
- **Missing persons** get a case *and* an incident — the case holds what makes
  the person findable, the incident puts it on the same board with the same
  dispatch machinery. Graded `high`, not `critical`: a search needs organising
  properly, and grading every case critical would queue it ahead of the cardiac
  arrest. Reunification closes both. Photos purge 30 days after **closure**, not
  after report — a case still open on day 31 has not stopped needing the photo.
- **Contact numbers are hashed on the row and encrypted elsewhere**, with a
  30-day TTL, exactly as Phase 2 handled pass holders. A missing child's name
  is deliberately absent from the audit log, which is append-only and never
  purged — putting it there would outlive the retention the case itself
  promises.
- **The permission split is enforced against the row, not just the role.** A
  volunteer holding `incident:update_low` may work `low` and `normal` incidents
  and may report a sighting on a missing-person case. They cannot re-grade
  anything, which is the part that matters: without that, the route to closing a
  critical is to downgrade it first.

Endpoints: `POST /sos`, `GET /sos/{reference}`, `POST /incidents`,
`GET /incidents`, `GET /incidents/{id}`, `PATCH /incidents/{id}`,
`GET /incidents/{id}/dispatch-options`, `POST /incidents/{id}/dispatch`,
`GET /responders`, `POST /responders/{id}/ping`, `POST /missing-persons`,
`GET /missing-persons`, `GET /missing-persons/{id}`,
`PATCH /missing-persons/{id}`, plus `POST /admin/incidents/sla-sweep` to run the
timer's job on demand.

Two background jobs join the scheduler: `incident_sla` every 15 seconds (the
shortest SLA is three minutes; a minute-granularity sweep would spend a third of
that deadline not knowing) and `photo_purge` hourly.

**Three deliberate additions beyond the spec text**, all flagged rather than
silently taken:

1. Section 4/M4 lists five incident sources and none of them fits a pilgrim
   filing a lost-item report by hand. Adding one matters more than it sounds:
   the SOS duplicate-suppression finds a caller's open emergency by
   `source = 'pilgrim_sos'`, so filing a lost umbrella under that source would
   make the pilgrim's *next* panic press attach itself to the umbrella. Hence
   `pilgrim_report`, and a route that derives the source from the caller's role
   rather than trusting the request body.
2. `GET /sos/{reference}` is not in Section 9's endpoint list, but Section 4/M4
   requires the pilgrim to see a confirmation and an ETA, and the Phase 7 PWA
   needs somewhere to poll. It matches on the caller's own phone hash, so a real
   reference belonging to somebody else answers exactly as a fake one does — the
   404 must not become a way to confirm a code is real. There is no route on
   which a pilgrim can enumerate incidents.
3. A unit cannot mark itself `available` while it is still assigned to an open
   incident. Nothing in the spec says so; without it the board silently
   double-books a unit already in use.

**One thing the spec asks for that is not built.** Section 4/M4 wants an SMS
fallback "if the app cannot reach the API". That belongs to the `notifier`
service, which is a Phase 5 line in the repository tree and does not exist yet.
Incidents currently reach people one way only: the WebSocket, which covers the
control room and the volunteer app but reaches nobody whose screen is off. No
outbound message is queued for them either — unlike Phase 2's pass moves, this
phase writes no notification rows at all, so there is nothing for a notifier to
drain when it lands.

This is stated rather than hidden because it is the gap that matters most in the
offline story. The PWA's queue-and-retry (Phase 7) covers a pilgrim who loses
signal entirely. Nothing yet covers a pilgrim whose phone can reach the cell
network but not us — which is the more common failure at a crowded ghat, and
the exact case the SMS fallback was specified for.

### Phase 6 in detail

The queue-breach ledger. Section 4/M5 opens with "this is the most sensitive
module in the product. Build it carefully" — this is what careful meant.

```bash
# with the stack up and seeded
curl -s localhost:8000/api/v1/breaches/verify -H "authorization: Bearer <officer token>"
```

- **What it records.** That an unauthorised entry occurred at a gate at a time.
  Not who. There is no column in `breach_events` that identifies a person and no
  code path that could populate one — the constraint is enforced by the schema
  having nowhere to put an identity, and by a CI check that fails if a field
  named for a track, a face or a phone appears in any breach schema.
- **Three filters before anything is written.** A crossing becomes a record only
  if it is in the restricted direction, the gate was flagged closed, *and* no
  valid pass was scanned at that gate within ±30 seconds. Most crossings never
  become events, and the rejection reasons come back on the ingest response —
  "the engine saw 40 and the ledger has 3" is a question somebody will ask.
- **The hash chain.** `chain_hash = SHA256(prev_hash ‖ canonical(payload))` over
  a canonical JSON serialisation with sorted keys. Edit an `occurred_at` by a
  microsecond, reorder a sequence, delete a row — `GET /breaches/verify`
  recomputes every hash and names each break separately, because "one bad row"
  and "everything after this was rewritten" are different findings. The database
  enforces it from below too: `trg_breach_evidence_immutable` raises on any
  UPDATE touching an evidence column.
- **Review columns are deliberately outside the hash.** If `review_status` were
  inside it, the first officer to mark an event verified would invalidate every
  hash after it — a chain that fires constantly means nothing.
- **Deletion redacts the clip, never the record.** Taken literally, Section
  4/M5's deletion rule means `DELETE FROM breach_events`, which breaks the chain
  — and a broken chain cannot distinguish an authorised deletion from tampering,
  which is the one distinction the ledger exists to make. So `DELETE
  /breaches/{id}` clears the clip, keeps the row and its hash, and records who
  and why. `clip_sha256` survives, so if the clip resurfaces from a backup it
  can still be shown to be the one the record refers to. The literal reading is
  still *detected*: an out-of-band row deletion breaks the chain, which is what
  verification is for.
- **`breach:delete` is the one permission Administrator is denied.** The temple
  administrator who might come under pressure to make a record go away is not
  the person who can. System Admin only, reason mandatory, logged to the
  append-only audit table.
- **Clip playback is re-authenticated and logged.** `POST` rather than `GET`,
  because it verifies a password and writes an access-log entry — a GET that
  must be audited is one a browser will prefetch and replay. A stated purpose is
  required; failures are audited too.
- **Nothing is a finding until a human says so.** Every event lands `pending`.
  The console's ledger view disables the review buttons entirely while the chain
  does not verify, and puts the chain banner above the records rather than
  beside them.
- **Retention.** 90 days, configurable, hourly purge job that clears clips and
  writes a `purge_log` row on every run — including the empty ones, which are
  the evidence the job has been running all season rather than since Tuesday.
  An hourly job also re-verifies the chain and writes any failure to the audit
  log.

Endpoints: `GET /breaches`, `GET /breaches/{id}`, `POST /breaches/{id}/review`,
`POST /breaches/{id}/clip`, `DELETE /breaches/{id}`, `GET /breaches/verify`,
`GET /breaches/summary`, `GET|POST /tripwires`, plus `POST /ingest/breach` for
the vision pipeline.

**One deliberate deviation from the spec text**, the redaction-versus-deletion
choice above. It is the third such flag in this repo and follows the same rule
as the other two: where the literal instruction and its stated purpose disagree,
serve the purpose and say so.

---

## Architecture

```
   CCTV / RTSP ─►  AI Service  ──events──►  Core API  ◄── Pilgrim PWA
   or Sim Engine  (FastAPI +               (FastAPI)      Volunteer app
                   worker)                     │          Admin console
                                               ▼
                              PostgreSQL 16 · TimescaleDB · PostGIS
                              Redis 7 (cache, pub/sub, sessions)
                              Object store (evidence clips, hashed)
```

The AI service **never writes to the database**. It publishes events; the Core
API owns all state. That boundary is what lets the AI service be restarted
during the Wari without losing a single pass or incident — and it is what makes
the "kill the container live" moment in the demo survivable.

### Stack

Python 3.11 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 · Alembic ·
PostgreSQL 16 + TimescaleDB + PostGIS · Redis 7 · YOLOv8 · LightGBM ·
React 18 + TypeScript + Vite · MapLibre GL · Workbox · i18next.

### Repository

```
wariverse/
├── docker-compose.yml
├── .env.example
├── services/
│   ├── core-api/        FastAPI: auth, passes, incidents, zones, breach, audit
│   ├── ai-engine/        (phase 3) crowd pipeline + simulation
│   └── notifier/         (phase 5) SMS / push / IVR fan-out
├── apps/
│   ├── admin-console/    React + MapLibre command centre
│   ├── pilgrim-pwa/      (phase 7)
│   └── volunteer-app/    (phase 5)
├── packages/             shared UI + generated API client
└── infra/                nginx, prometheus, grafana, locust
```

---

## Design principle

The system degrades gracefully. If the AI service dies, passes, alerts,
incidents and the queue keep working on manual input. If the network dies, the
pilgrim app queues actions and tells the user plainly how old its data is.

A crowd-safety system that fails closed is worse than no system. This one is
built for the day it breaks, not the day it demos.
