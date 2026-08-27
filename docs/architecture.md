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

  Phase 10 added a second family alongside it (`app/core/metrics.py`), and the
  split is the point. Per-route metrics answer *is the API healthy*. Per-zone
  metrics answer *is the system still seeing the Wari*. Those can disagree, and
  the gap between them is where an outage hides: an API serving 200 OK on every
  route while forty cameras have gone dark is a green dashboard over a blind
  control room.

  Two properties worth knowing. **Nothing is collected on scrape** — every value
  is set by the path that already holds it (ingest, camera watchdog, Palkhi
  sweep), because a collector that fans out to Postgres turns monitoring into
  load, which is the wrong behaviour precisely when the system is under strain.
  And **gauges go stale deliberately**: when a feed dies its zone keeps its last
  density and `wariverse_zone_reading_age_seconds` climbs without limit. The
  alert fires on the age, never the density. A metric that zeroed itself when
  its feed died would render an unmeasured zone as an empty one.
- **Alerting** — `infra/prometheus/alerts.yml`. Every rule fires on the system
  going blind or silent, never on the crowd being busy: `density` appears in no
  rule, reading-age appears in two. A dense zone already reaches an operator
  through the product's own alert pipeline with a recommended action; paging an
  engineer for it would train them to silence the pager that also carries
  "the cameras are dark".
- **Health** — `/health` for load balancers, `/health/deep` for humans.

## Phase map

Modules land in the order given by Section 15 of the development prompt. The
schema already contains every table the later phases need, so migrations do not
churn as features arrive.

That last claim held through Phase 8 and stopped being literally true in Phase
9, which is worth recording rather than quietly editing away. Phase 1 built the
*entities* Section 8 names — zones, passes, incidents, breaches, `dindis`,
`halt_towns`. What later phases needed and Phase 1 could not have anticipated
was never an entity:

- **`forecasts` (0006)** — a forecast is not an entity, it is a claim about one.
  Stored rather than recomputed so `target_at` is a real column and a scoring
  query can join a prediction to the reading that eventually arrived.
- **`dindi_schedule` (0007)** — Section 8 put `expected_arrival` on
  `halt_towns`, which works for a route with one Palkhi and breaks the moment
  forty Dindis share the Alandi road and reach Saswad on different evenings. A
  town's real question is "who is arriving at me tonight, and how many", and
  that is a join table, not a column.
- **`assistant_turns` (0007)** — Section 13's transcript. The assistant is the
  only component here that emits sentences nobody wrote, and its contract is
  that every factual claim came from a tool call. That is unfalsifiable unless
  the calls are stored beside the answer.

Phase 9 also denormalised tracking state onto `dindis` (status, last ping,
position along the route). Those are derivable from the ping hypertable, and
deriving them was the wrong trade: the halt-town readiness board reads every
active Dindi on every refresh, so the alternative is one time-series scan per
Dindi per poll.

### Alerts have three subjects, not one

`alerts.zone_id` was the only subject until Phase 9. A Palkhi alert has no zone
— the Wari is 250 km of road, and what an operator clicks through to is a Dindi
and the town it is about to reach. `alerts` therefore carries `dindi_id` and
`halt_town_id` as explicit nullable foreign keys rather than a generic
`subject_type` / `subject_id` pair, for the same reason `zone_id` is one:
"every alert about Saswad tonight" should be an indexed query, not a string
comparison against a discriminator column.
