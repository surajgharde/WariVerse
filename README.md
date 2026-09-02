# 🚩 WariVerse

**A multilingual AI companion for the ~2 million pilgrims who walk the Pandharpur Wari.**

Every year, *warkaris* walk 250 km from Alandi and Dehu to the Vitthal temple at
Pandharpur. On the road they need the same handful of answers, over and over:
*How long is the darshan queue right now? Where is the nearest water point or
medical post? Which gate is less crowded? Where is my mother — we got separated
an hour ago?* WariVerse answers those questions in **Marathi, Hindi and English**,
by chat, by voice, and over an in-app phone line for pilgrims who cannot type.

This repository is the full stack: an Expo/React Native app and a FastAPI backend.

> ⚠️ **This is a working prototype, not a deployed public service.** The zone,
> facility and temple-timing datasets are approximate placeholders compiled from
> public sources, and crowd density is produced by a simulator standing in for a
> real CCTV feed. See [Status & caveats](#-status--caveats) before trusting any
> number in it.

---

## ✨ What it does

| | Capability |
| --- | --- |
| 💬 | **Conversational assistant** — GPT-4o with eight real tools (crowd, forecast, routes, facilities, temple info, lost & found, SOS, human escalation). It only phrases facts the tools returned; it is forbidden from inventing timings or distances. |
| 🗣️ | **Voice in, voice out** — speech-to-text via Deepgram with a Whisper fallback; text-to-speech via ElevenLabs for English/Hindi and Google WaveNet for Marathi. |
| ☎️ | **In-app IVR dialer** — a native-feeling phone keypad and call screen that drives the same menu tree as a real helpline, with **no telephony provider and no per-minute cost**. Twilio webhooks exist too, so a feature phone with no data reaches the same assistant. |
| 👥 | **Live crowd density** — six monitored zones (three gates, the temple, Bhima ghat, the main road) with a 12-hour forecast that names the first hour crowds *ease*, not the quietest hour at 2 AM. |
| 🗺️ | **Map & route guidance** — Mapbox/Leaflet map centred on Pandharpur, live crowd badges, POI search, one-tap call and Google Maps directions from every pin. Routing picks between three surveyed corridors rather than free-routing pilgrims across barricades. |
| 🙏 | **Community seva** — pilgrims publish free offerings (food, water, shelter, medical help) and register community facilities; others discover, reserve and unlock them from the map. |
| 🛕 | **Palkhi tracking** — live procession position and Solapur Rural Police contacts surfaced in-app. |
| 🆘 | **Emergency SOS** — unauthenticated by design, with a human always in the loop: the model can only raise an emergency as `PENDING`; an explicit confirmation activates it. |
| 🔎 | **Lost & found** — reports keyed by a collision-free reference id (`WF-2026-00124`), published live to a control-room channel. |
| 🔐 | **Phone + OTP auth** — 30-day JWT, 3 codes per number per hour, numbers normalised so `9876543210` and `+91 98765-43210` are the same pilgrim. |

Two principles run through the whole codebase, and explain most of its odd
corners:

- **Degrade, never disappear.** Postgres down, Redis down, no LLM key, no
  network — the app still answers with cached or bundled reference data. The
  test suite deliberately runs *without* Postgres or Redis so the degraded path
  stays honest.
- **Safety wording is never generated.** SOS prompts, crowd warnings and
  helpline numbers come from a translation file, not from a model.

---

## 🏗️ Repository layout

```
WariVerse/
├── Frontend/                      pnpm workspace (Expo + TypeScript)
│   └── artifacts/
│       ├── wariverse/             ← the pilgrim app
│       │   ├── app/               Expo Router screens: chat, map, help,
│       │   │                      settings, auth, ivr-dialer
│       │   ├── components/        MapCanvas, WidgetCards, IVRActiveCall,
│       │   │                      IVRKeypad, ChatMessage
│       │   ├── services/          api.ts (real) · mockApi.ts (offline
│       │   │                      fallback) · ivrApi · ivrAudio · speech
│       │   ├── store/             AppContext — auth, language, GPS
│       │   └── types/             domain types
│       ├── api-server/            small Express service
│       └── mockup-sandbox/        Vite + shadcn/ui component sandbox
│
└── wariverse-backend/             FastAPI + Postgres + Redis
    ├── app/
    │   ├── routers/               conversation, crowd, facilities, routes,
    │   │                          temple, lost_found, sos, auth, voice,
    │   │                          ivr, ivr_session, community, admin
    │   ├── services/              llm_orchestrator, crowd (+ simulator),
    │   │                          facility, route, palkhi, sos, ivr_state,
    │   │                          stt, tts, voice, sms
    │   ├── models/                SQLAlchemy + Pydantic v2 schemas
    │   └── data/                  reference.py, temple.py, i18n.py
    ├── alembic/                   migrations
    ├── scripts/                   idempotent seed · crowd simulator
    └── tests/                     pytest suite (~27 modules)
```

**Detailed docs live next to the code:**
[`wariverse-backend/README.md`](wariverse-backend/README.md) documents every
endpoint, the data model and the design decisions behind them.
[`Frontend/README.md`](Frontend/README.md) covers the app screens and UI.

---

## 🚀 Getting started

### 1. Backend

```bash
cd wariverse-backend
cp .env.example .env          # JWT_SECRET and ADMIN_API_KEY are required
docker compose up --build
```

That brings up nginx → the API (4 uvicorn workers) → Postgres + Redis, plus a
single-replica crowd simulator. Migrations and the idempotent seed run before
the API serves. Interactive docs: <http://localhost:8000/docs>.

Prefer no Docker:

```bash
python -m venv .venv && .venv\Scripts\activate   # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head && python -m scripts.seed
uvicorn app.main:app --reload
```

### 2. Frontend

```bash
cd Frontend
pnpm install
pnpm --filter @workspace/wariverse run dev    # Metro on http://localhost:8081
```

Point the app at the backend:

```bash
# Frontend/artifacts/wariverse/.env.local
EXPO_PUBLIC_API_URL=http://localhost:8000
```

On a physical device `localhost` is the phone — use your machine's LAN IP.
Left unset, the client derives the host from Metro.

### 3. Checks

```bash
cd wariverse-backend && pytest      # runs without Postgres or Redis, by design
cd Frontend && pnpm run typecheck   # strict TS across the workspace
```

---

## 🧰 Tech stack

| Layer | Built with |
| --- | --- |
| App | Expo 54, React Native 0.81, React 19, Expo Router, TypeScript, Reanimated, react-native-webview (map) |
| API | FastAPI, SQLAlchemy 2 async, Pydantic v2, Alembic, structlog |
| Data | PostgreSQL 16 (haversine on a btree index — no PostGIS needed for a 250 km corridor), Redis 7 (sessions, crowd cache, TTS cache, pub/sub) |
| AI | OpenAI GPT-4o (tool calling) · Whisper + Deepgram Nova-2 (STT) · ElevenLabs + Google WaveNet + OpenAI `tts-1` (TTS) |
| Ops | Docker Compose, nginx, pnpm workspaces |

---

## ⚠️ Status & caveats

The backend README documents these in full; the ones that matter most before
this goes anywhere near real pilgrims:

- **Reference data is placeholder.** Zones, facilities, waypoints and temple
  timings must be replaced with the surveyed dataset from the Solapur district
  administration and the Mandir Samiti's published schedule.
- **Crowd density is simulated.** A hardcoded daily curve stands in for CCTV
  ingestion. It is not a trained model.
- **OTP codes are stored in plaintext.** Hashing is already implemented and
  unused — switch to it and widen the column before real numbers are handled.
- **`/api/voice/*` is not rate limited** and every call spends money on a
  third-party API. Put a limit or a bearer token in front of it before exposing it.
- **The app ships one hard-coded session id for anonymous users**, so anonymous
  transcripts interleave. Authenticated sessions are scoped per user and safe.
  The fix belongs in the client: one random id per install.

---

 
