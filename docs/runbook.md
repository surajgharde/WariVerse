# WariVerse runbook

**For the duty officer and the on-call engineer, during the Wari.**

Section 11 asks for "a one-page runbook: *if the AI service is down, do this*".
That page is [The AI service is down](#the-ai-service-is-down). Everything else
here follows the same shape, because Section 11 also asks for a defined and
tested manual mode for **every** module, and E8 makes graceful degradation a
product guarantee rather than an accident.

Two things to hold on to before reading any specific procedure.

**Nothing in this system fails closed.** Every module keeps working when the
one below it dies, in a reduced form that says it is reduced. If you find
yourself about to turn something off "so it stops showing wrong numbers", read
the relevant section first — it almost certainly already shows them as unknown.

**"Unknown" is a safe state. "Clear" is not.** The single most dangerous thing
anybody can do during an incident is make a screen say a zone is fine because
the data stopped arriving. Every degradation below preserves that distinction.
Do not undo it.

---

## Triage in sixty seconds

Open Grafana (`:3000`, board *"Can we still see the Wari?"*) and read the top
row only.

| What you see | What it means | Go to |
| --- | --- | --- |
| Oldest zone reading climbing past 300s | The crowd pipeline has stopped | [AI service down](#the-ai-service-is-down) |
| Cameras online < total, readings still fresh | Partial blindness, system healthy | [Cameras offline](#cameras-are-offline) |
| Everything fresh, API 5xx rising | The API is unwell, the pipeline is fine | [Core API failing](#the-core-api-is-failing) |
| Board empty, Grafana says no data | Prometheus or the API is down | [Core API failing](#the-core-api-is-failing) |
| Nothing wrong on the board, control room reports chaos | Trust the control room | [Total fallback](#total-fallback-paper-mode) |

If you cannot reach Grafana at all, skip to [Total fallback](#total-fallback-paper-mode).

---

## The AI service is down

**Symptoms.** `EveryZoneStale` or `IngestStopped` firing. Density map shows every
zone grey. `/health/deep` reports the crowd pipeline as degraded.

**What still works — this is most of the product.** Passes issue and scan.
QR validation is entirely local to core-api. SOS, incidents and dispatch are
untouched. The breach ledger keeps recording. The pilgrim app still shows pass,
map, facilities and emergency numbers. **Do not take anything else down.**

**What stops.** New density readings, and therefore new crowd alerts, forecasts,
and the automatic reslotting job's input signal.

**What the system does on its own, without you:**

- Redis snapshots expire after 5 minutes, so the map goes to **UNKNOWN** rather
  than freezing on the last green reading. This is the TTL doing its job.
- Every zone renders grey with "no live reading", not as clear.
- The pilgrim app shows the same, with the age of the last reading.

### Do this

1. **Tell the control room, in words, that the crowd map is blind.** Not "the
   AI service is degraded" — say: *"the density map is not updating; treat every
   zone as unknown and switch to radio reports from the zone marshals."*
2. **Stand up radio crowd reporting.** One marshal per zone, reporting a level
   (comfortable / busy / very crowded / do not enter) every 10 minutes on the
   hour and half hour. The console has no manual-entry field by design — a
   typed-in number would be indistinguishable from a measured one on every
   downstream screen. Radio and paper.
3. **Freeze automatic reslotting** if it is going to run on stale throughput:
   ```
   PUT /api/v1/admin/config/reslot_deviation_pct   {"value": 1.0}
   ```
   A threshold of 1.0 means "only reslot on a 100% deviation", which in practice
   never fires. Restore it to `0.20` afterwards.
4. **Try to bring it back:**
   ```bash
   docker compose ps ai-engine
   docker compose logs --tail=200 ai-engine
   docker compose restart ai-engine
   ```
   Common causes, in the order they actually happen: the container OOMed on the
   vision stack; `AI_SERVICE_TOKEN` does not match core-api's; the machine's
   clock drifted and readings are being rejected as outside the accepted window
   (check `wariverse_readings_rejected_total` — a rising rejection count with
   zero acceptances is authentication or clock, not a dead process).
5. **If it will not come back, run the simulator.** It publishes through exactly
   the same ingest path and is honest about what it is — readings arrive tagged
   `source: sim` and every screen shows that tag:
   ```bash
   docker compose stop ai-engine
   CROWD_SOURCE=sim docker compose up -d ai-engine
   ```
   Only do this for a rehearsal or a demo. **During a live Wari, simulated
   density on an operator's screen is worse than a grey map** — grey prompts a
   radio call, plausible numbers do not.

### Coming back

Readings resume within one aggregation window (10s). Zones repopulate as their
cameras report. Stand down radio reporting only once the oldest-reading panel is
back under 90 seconds for every zone, and restore `reslot_deviation_pct`.

---

## Cameras are offline

**Symptoms.** `CameraHeartbeatLost` or `MostCamerasOffline`. Some zones fresh,
others stale.

**What the system does on its own.** The watchdog marks the camera offline
within 120 seconds, raises **one** coverage alert per affected zone (not per
camera — three dead cameras in one corridor is one problem for the operator),
and drops that zone's confidence so its density reads as an estimate.

### Do this

1. Check whether it is one camera or the estate. `MostCamerasOffline` means
   power, network or the NVR — **do not** start rebooting individual cameras.
2. For a single camera: check PoE, then the RTSP URL, then the camera.
3. For a zone that has lost **all** coverage, treat that zone as
   [AI-service-down](#the-ai-service-is-down) locally: radio reporting for that
   zone only, and tell the operator its number is now an estimate.
4. A camera that comes back clears its own alert. No action needed.

**Do not** delete or deactivate a camera to silence the alert. The alert is the
only signal that a corridor is unwatched, and a deactivated camera is a corridor
nobody is even being told about.

---

## The core API is failing

**Symptoms.** `ApiDown`, or `ApiErrorRate` over 1%.

**This is the one genuine outage.** Nothing else in the product works without
core-api. Treat it as the top priority.

### Do this

1. **Get a trace id.** Every error the API emits carries one, in the envelope
   and in the `x-trace-id` header. An operator can read it off their screen.
   ```bash
   docker compose logs core-api | grep <trace_id>
   ```
2. **Check the database first.** core-api will not serve without Postgres, and
   most "API is down" reports are a database problem:
   ```bash
   docker compose ps db
   docker compose exec db pg_isready -U wariverse
   curl -s localhost:8000/health/deep | jq
   ```
3. **Check disk.** A full disk stops Postgres writing and presents as a hung
   API. `df -h` on the host, and `docker system df` — build cache and old
   images are usually the culprit.
4. **Restart:** `docker compose restart core-api`. It is stateless; nothing is
   lost. Sessions survive because tokens are JWTs.
5. **While it is down**, the control room is on radio and paper for everything.
   The pilgrim app still works offline for pass display and emergency numbers —
   a pass already on a phone still scans, because QR validation happens against
   a signature the scanner can check without the network.

---

## Redis is down

**Symptoms.** Live map stops updating but `/crowd/live` still answers on
refresh. WebSocket connections refused.

**What still works.** Everything. Redis holds the live snapshot cache, the
WebSocket fan-out and the job locks — all three degrade rather than fail.

- Density reads fall back to Postgres. Slower, still correct.
- Event publishing is best-effort and never fatal: losing an event costs an
  operator two seconds of freshness, and refusing an ingest because Redis is
  down would throw away the reading itself.
- Background jobs run **without** their single-runner lock. With three replicas
  a job may run three times; each is idempotent by design, so this is safe.
- A WebSocket that cannot subscribe is **refused at connect time** rather than
  left open and silent — a silent socket looks exactly like a quiet temple.

### Do this

`docker compose restart redis`. Clients reconnect with backoff on their own.
No manual intervention on the application side.

---

## The assistant is answering strangely, or not at all

**What the system does on its own.** With no API key, a timeout, an HTTP error
or a model that never stops calling tools, the assistant falls back to
deterministic templated answers over the same five read-only tools. Turns are
logged with outcome `degraded`.

### Do this

- Check `AssistantDegradedSilently` and the assistant panel. Confirm
  `GEMINI_API_KEY` and the provider's status page.
- To switch it off entirely, without a deploy:
  ```
  PUT /api/v1/admin/config/assistant_enabled   {"value": false}
  ```
  Callers then get `ASSISTANT_DISABLED`, which the app renders as "the
  information is on the main screens, the helpline is on the home page".
- **If a pilgrim reports a wrong answer**, find the turn — every one is logged
  with its tool calls:
  ```
  GET /api/v1/assistant/turns?session_id=<id>
  ```
  The tool calls beside the answer are what make the complaint answerable. If
  the tools returned correct data and the answer was wrong, that is a model
  failure and the assistant should be switched off until it is understood.

---

## Palkhi tracking has gone quiet

**Symptoms.** `PalkhiTrackingBlind`; halt-town board showing arrival times as
unknown.

**Almost always flat batteries, not a server fault.** Phones back off their
reporting interval as they drain — ten minutes at 25%, fifteen at 10% — which is
the design working, not failing.

### Do this

1. A silent Dindi shows **no ETA at all** rather than a projected one. That is
   correct. Do not ask for it to be "filled in".
2. Get the leaders' numbers and call them — the endpoint is audited, which is
   why it exists:
   ```
   GET /api/v1/dindis/{id}/leader-contact
   ```
3. Tell the affected halt towns to plan against the **printed** schedule, and
   that they will get no early warning of a deviation until the phone reports
   again.
4. If a group has genuinely diverted, an administrator updates its schedule
   (`PUT /dindis/{id}/schedule`) so downstream towns are warned in order.

---

## Total fallback (paper mode)

When core-api and the database are both unreachable and will not return quickly.

1. **Gates stop scanning and start counting.** One tally per gate per 30-minute
   slot, on paper. This is what the throughput reconciliation will be rebuilt
   from afterwards.
2. **Admit on the printed pass.** A pass card shows its slot time and reference
   in plain text next to the QR precisely so a human can read it without a
   scanner. Honour the time; do not turn people away because a scanner is dead.
3. **SOS goes to the control-room SMS number** (`CONTROL_ROOM_SMS_NUMBER`),
   which is printed on the pilgrim app's offline essentials screen and works
   with no API at all.
4. **Crowd control is radio and marshals.** As it was before this system
   existed.
5. **Write down the time everything stopped.** The audit log will have a hole in
   it, and a review needs to know that the hole is an outage rather than a
   deletion.

---

## After any incident

- [ ] Record the start and end time, and the trace id of the first error.
- [ ] Check the breach ledger still verifies: `GET /api/v1/breaches/verify`.
      A restart is not supposed to affect the chain; confirm it did not.
- [ ] Reconcile any paper gate tallies into the slot records.
- [ ] Restore any config you changed (`reslot_deviation_pct`,
      `assistant_enabled`) and confirm the value that is live.
- [ ] Note it for the post-event analytics pack. The trust's real annual need is
      an evidence report for planning next year's Wari, and an outage during the
      peak hour is part of that evidence.
