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
challenge instead of tokens — enrol at `/auth/mfa/enrol`, confirm at
`/auth/mfa/enrol/confirm`, then complete sign-in at `/auth/mfa/verify`.

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

---

## What is built

| Phase | Module | State |
| ----- | ------ | ----- |
| 1 | Foundation — repo, Docker, schema, auth, RBAC, audit log, health, CI | **done** |
| 2 | M1 Smart Darshan Pass — slots, QR, scan, dynamic reslotting | **done** |
| 3 | M2 Crowd intelligence — sim engine, YOLOv8 pipeline, zone metrics | not started |
| 4 | M3 Command Center — live map, alert feed, replay scrubber | not started |
| 5 | M4 Incidents & SOS — dispatch, SLA, missing persons | not started |
| 6 | M5 Queue breach ledger — tripwires, hash chain, review flow | not started |
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
│   ├── admin-console/    (phase 4)
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
