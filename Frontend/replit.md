# WariVerse

An accessible multilingual conversational companion for pilgrims taking part in the Wari pilgrimage.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm --filter @workspace/wariverse run dev` — run the Expo mobile app
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9, Expo SDK 54
- API: Express 5
- Mobile: Expo Router, React Native, AsyncStorage, React Context
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `artifacts/wariverse/app/` — onboarding, chat, map, help, and settings screens
- `artifacts/wariverse/components/` — brand, chat message, widget cards, and map presentation
- `artifacts/wariverse/store/AppContext.tsx` — session, chat, preferences, location, and local persistence
- `artifacts/wariverse/services/mockApi.ts` — development response contract and demo routing
- `artifacts/wariverse/types/domain.ts` — stable conversation and widget types

## Architecture decisions

- Chat is the primary product surface; tool responses render as discriminated widgets beneath assistant messages.
- The first build is frontend-only and uses AsyncStorage plus a mock service so it runs without backend credentials.
- Location is requested contextually from Map and Help rather than during launch.
- SOS requires explicit confirmation and only shows activation after the mock service confirms it.
- Speech and text-to-speech stay behind service interfaces for later provider integration.

## Product

- Supports Marathi, Hindi, and English chat with localized onboarding, labels, and suggestions.
- Demonstrates crowd density, forecast, route, facility, temple, Lost & Found, volunteer, and SOS widgets.
- Includes outdoor-friendly colors, large controls, readable status indicators, local conversation history, and offline/error messaging.

## User preferences

No project-specific preferences recorded yet.

## Gotchas

- Keep backend credentials and provider SDKs out of the mobile app. Replace `services/mockApi.ts` behind the same typed contracts when the FastAPI service is ready.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
