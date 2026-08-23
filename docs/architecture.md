# WariVerse — Architecture

## The shape of the system

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
```

## The one boundary that matters

**The AI service never writes to the database.** It publishes events over HTTP
(authenticated with a shared service token) and Redis pub/sub. The Core API owns
all state.

This is not tidiness. It is the reason the AI service can be killed and restarted
mid-Wari — during a live incident, on a bad night — without losing a pass, an
incident, or an audit entry. Every module has a manual mode that keeps working
when the intelligence layer is gone.

## Data stores

| Store | Role | What breaks without it |
| ----- | ---- | ---------------------- |
| PostgreSQL 16 | Source of truth for every entity | Everything. This is the only hard dependency. |
| TimescaleDB | `density_readings` and `dindi_pings` hypertables, 1-minute continuous aggregate | Time-series queries slow down; nothing is lost |
| PostGIS | Zone polygons, incident points, GIST indexes, nearest-responder queries | Map and spatial dispatch |
| Redis 7 | OTP codes, refresh-token families, rate limits, WebSocket fan-out | Sign-in and live push degrade; passes and incidents keep working |
| Object store | Breach evidence clips, hashed and encrypted | Clip playback only; the ledger record survives |

`/health/deep` reflects this hierarchy: it returns **degraded**, not **down**,
when a non-essential dependency fails, and 503 only when Postgres is gone.

## Schema notes

The full model set lives in `services/core-api/app/models/`. Three decisions are
worth calling out:

**Phone numbers are HMAC hashes.** `passes.holder_phone_hash`,
`incidents.reporter_phone_hash`, `users.phone_hash` — all keyed HMAC-SHA256, so
a stolen database cannot be brute-forced against the small space of Indian phone
numbers. Raw numbers exist only in `contact_secrets`, Fernet-encrypted, with a
`purge_after` timestamp and a job that enforces it. Staff accounts keep a raw
phone because they are employees on a roster, not pilgrims.

**`density_readings` carries no identity.** Per-person track IDs are ephemeral
and in-memory inside the AI service; only zone aggregates are ever persisted.
There is no table in this schema that could answer "where was this person" —
that is a structural guarantee, not a policy.

**`breach_events` is a hash chain.** Each row stores `prev_hash` and
`chain_hash = SHA256(prev_hash || canonical(payload))`, plus a monotonic
`sequence`. A deleted or edited row breaks the chain visibly. A database trigger
makes the evidence fields immutable; only System Admin can delete, only with a
written reason, and the deletion itself is logged permanently. This is what makes
the queue-integrity record hold up against exactly the political pressure the
problem statement describes.

## Authentication

- **Pilgrims** — phone OTP. No password, no signup form. A 70-year-old walking
  the Wari should not have to invent a password. Accounts are created on first
  verified code.
- **Staff** — password (Argon2id) with account lockout after five failures.
- **Administrator and System Admin** — password *and* TOTP. The password step
  returns an MFA challenge token, never session tokens.

Access tokens live 15 minutes; refresh tokens live 7 days and rotate on every
use. Each login opens a *token family*: only the newest refresh token in a family
is valid. Presenting a superseded one means a token was stolen and replayed, so
the whole family is revoked and an audit entry is written. Access-token
revocation on logout uses a Redis denylist scoped to the token's own TTL.

Role changes take effect on the *next request*, not the next login — a revoked
officer does not keep their permissions for fifteen minutes.

## Authorisation

One matrix, `app/core/permissions.py`. Routes declare a permission:

```python
@router.post("/breaches/{id}/review")
async def review(actor: Actor = Depends(require(Permission.BREACH_REVIEW))): ...
```

Roles are never compared in route code. Adding a role means editing one dict,
not auditing fifty handlers. `app/tests/test_permissions.py` asserts the
invariants the spec states in prose.

## Observability

- **Logs** — one JSON object per line, carrying the `trace_id` the client
  received in its error envelope and `x-trace-id` header. An operator can hand
  you a trace id from a screenshot and you can find the request.
- **Metrics** — `/metrics` exposes per-route request counts and a latency
  histogram bucketed around the p95 < 300 ms target. The route *template* is the
  label, not the raw path, so pass ids do not explode cardinality.
- **Health** — `/health` for load balancers, `/health/deep` for humans.

## Phase map

Modules land in the order given by Section 15 of the development prompt. The
schema already contains every table the later phases need, so migrations do not
churn as features arrive.
