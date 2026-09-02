# 🚩 WariVerse Frontend

> **WariVerse** is a modern, mobile-first conversational AI and navigation assistant designed for pilgrims participating in the Pandharpur Wari pilgrimage.

---

## 🌟 Key Features & Capabilities

### 💬 1. Conversational Multilingual AI Assistant
- **Multilingual Support**: Switch seamlessly between **मराठी (Marathi)**, **हिंदी (Hindi)**, and **English**.
- **Voice Input & Read Aloud**: Tap to speak with animated voice feedback and TTS read-aloud support.
- **Persistent Quick Suggestion Bar**: Horizontally scrollable question chips (*Check crowd*, *Show route*, *Nearby facility*, *Temple info*, *Get help*) accessible at all times while chatting.
- **Rich Interactive Cards**: Structured cards for crowd density, routes, facility locator, lost & found, emergency SOS, and temple schedules.

### 🗺️ 2. Live Interactive Leaflet.js Map
- **Pandharpur Centered Map**: Dynamic map tiles centered at Pandharpur (`17.6778° N, 75.3260° E`).
- **Live Crowd Badges**: Real-time crowd density badges for **Vitthal Temple**, **Gate 2**, **Gate 3**, and **Bhima River Ghat**.
- **Route Guidance**: Live polyline navigation for pilgrim routes.
- **One-Tap Recenter**: Smooth animated re-centering back to user location / main temple.

### 🔐 3. Mobile OTP Sign Up & Authentication
- **2-Step Mobile Auth**: Enter +91 10-digit phone number -> receive and verify 6-digit OTP.
- **Post-OTP Onboarding Setup**: Immediate preference setup for language, profile name, and location permissions.
- **Session Persistence**: Persistent user profile state backed by `@react-native-async-storage/async-storage`.

---

## 🚀 Getting Started

### Prerequisites
- Node.js >= 18
- pnpm / npm

### Installation & Run

```bash
# 1. Install dependencies across the workspace
pnpm install

# 2. Start the WariVerse frontend dev server
pnpm --filter @workspace/wariverse run dev
```

The Metro server will run on `http://localhost:8081`.

### Verification & Typecheck

To run strict workspace typechecking across all TypeScript projects:

```bash
npx pnpm run typecheck
```

---

## 📂 Project Architecture

```
Frontend/
├── artifacts/
│   └── wariverse/
│       ├── app/                # Expo Router screens & tab navigation
│       │   ├── (tabs)/
│       │   │   ├── index.tsx   # Conversational Chat Screen & Suggestion Bar
│       │   │   ├── map.tsx     # Live Leaflet Map Screen
│       │   │   ├── help.tsx    # Help & Emergency SOS Portal
│       │   │   └── settings.tsx# Preferences & User Account Screen
│       │   ├── auth.tsx        # OTP Sign Up / Login & Profile Setup Modal
│       │   └── _layout.tsx     # App Root Layout & Navigation Provider
│       ├── components/         # MapCanvas (Leaflet), ChatMessage, WidgetCards
│       ├── constants/          # Design tokens (colors) & Multilingual Copy
│       ├── services/           # SpeechService & MockConversationApi
│       ├── store/              # AppContext (Auth state & Persistence)
│       └── types/              # Domain types (User, Message, ToolWidgets)
└── package.json
```

---

## 🔒 Security & Best Practices

- **Zero Credentials on Client**: API keys, LLM credentials, and emergency dispatch logic are managed exclusively on the backend.
- **Strict Accessibility**: Clean focus styles, high-contrast touch targets, and full screen-reader support.
