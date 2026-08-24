# Admin Console — Temple Command Center

Phase 4. Section 4/M3 plus Section 10.

A single-screen operations console: six KPIs across the top, a "what changed"
strip under them, the zone roster on the left, the live density map in the
centre, and a prioritised alert feed on the right.

## Run it

Against a running core API (`docker compose up -d db redis core-api`):

```bash
npm install
npm run dev          # http://localhost:5173
```

The dev server proxies `/api` to `http://localhost:8000`, including the
WebSocket, so the browser sees one origin and CORS never enters the picture.
Point it elsewhere with `VITE_CORE_API_URL`.

Sign in with any seeded staff account — `9000000003` / `wari-demo-2026-change-me`
is the Security Officer, which has `crowd:view_detail` and needs no MFA. The
Administrator (`9000000002`) works too and will prompt for a TOTP code.

To see the map do anything, the AI engine must be publishing:

```bash
docker compose up -d ai-engine       # CROWD_SOURCE=sim by default
```

In Docker the console runs behind its own nginx on
`http://localhost:${ADMIN_CONSOLE_PORT:-5173}`:

```bash
docker compose up -d --build admin-console
```

## Checks

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

## What it consumes

| Endpoint | Used for |
| --- | --- |
| `GET /command/config` | escalation thresholds, staleness window, density bands, crowd source |
| `GET /command/kpis` | the six-card top strip |
| `GET /command/changes` | the 15-minute catch-up strip |
| `GET /command/replay` | the scrubber's frames |
| `GET /zones` | map polygons |
| `GET /crowd/live` | zone state on load, and the polling fallback |
| `GET /cameras` | camera roster and the map's status dots |
| `GET /alerts`, `POST /alerts/{id}/ack` | the right rail |
| `WS /ws/crowd` | live density, alert and camera events |

## Decisions worth knowing about

**No basemap tiles by default.** The map draws our own zone polygons on a flat
background and needs no network beyond the API. A control room during the Wari
is exactly where a map that blocks on a tile CDN fails. Set `VITE_MAP_STYLE` to
a style URL if a deployment has a basemap it can reach reliably.

**No webfont fetch.** Section 10 names Mukta and Inter; the console uses system
Devanagari and UI stacks instead. A console that blocks first paint on a font
CDN renders in a fallback face on the day the uplink is saturated.

**`null` is never `0`.** The server returns `null` for anything it is not
measuring, and every formatter in `src/lib/format.ts` renders that as an em
dash. `src/lib/format.test.ts` is that rule, written down — a well-meaning
`?? 0` would undo it in a one-line diff that looks like a null-safety fix.

**Staleness runs on a clock, not on arrival.** `LiveProvider` ticks once a
second and re-derives every reading's age. A zone goes grey ninety seconds
after its last reading whether or not anything new arrives — especially when
nothing does, since that is the case that matters.

**Layout deviation from the spec, taken deliberately.** Section 4/M3 puts the
live map in the left rail and offers it again as a centre tab. Rendering the
same map twice spends the largest area on the screen duplicating something
already visible, and Section 10 is explicit that the density map is the
product. So the map is the centre's default tab at full size, and the left rail
carries the zone roster — every zone, fixed order, with reading, staleness and
flow. The rail answers "what is the state of zone C" without a click, which is
what the rail-map was for.

## Not here yet

Incident pins (Phase 5) and the Palkhi marker (Phase 9) are absent because
those modules do not exist. They are map layers, not redesigns — `ZoneMap`
gains two GeoJSON sources when those phases land. The alert card's **Dispatch**
button is present and disabled, with a tooltip saying why; a button that
silently does nothing is worse than one that admits what it needs.

The forecast tab named in Section 4/M3 waits on Phase 8.
