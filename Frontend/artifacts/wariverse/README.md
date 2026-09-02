# 🚩 WariVerse App Package

WariVerse is a mobile-first, multilingual conversational assistant and interactive navigation platform for pilgrims participating in the Pandharpur Wari pilgrimage.

## 🌟 Features Overview

- **Conversational AI Chat**: Natural multi-turn chat supporting **Marathi (मराठी)**, **Hindi (हिंदी)**, and **English**.
- **Persistent Quick Suggestion Bar**: One-tap question chips available at all times while chatting.
- **Interactive Leaflet.js Map**: Real-time tile map centered on Pandharpur with live crowd badges (Gate 2, Gate 3, Vitthal Temple, River Ghat) and route polylines.
- **OTP Sign Up & Login**: Mobile phone authentication with OTP verification and post-OTP preference setup.
- **Rich Tool Widgets**: Crowd density badges, route guidance, nearby facilities, temple timings, Lost & Found, and Emergency SOS.
- **Voice UI**: Speech-to-text recording UI and text-to-speech read aloud capabilities.

## 🚀 Running Locally

```bash
# Run Expo dev server
pnpm run dev
```

Server URL: `http://localhost:8081`

## 🧪 Typechecking

```bash
pnpm run typecheck
```

## 🏗️ Architecture

```
app/                 Expo Router screens and tab navigation (index, map, help, settings, auth)
components/          MapCanvas (Leaflet.js), ChatMessage, WidgetCards, BrandMark
constants/           Color tokens and localized copy (mr, hi, en)
services/            Mock API router, TTS, and SpeechService abstractions
store/               AppContext handling messages, location, auth user, and AsyncStorage persistence
types/               Domain contracts (User, Message, ToolWidgets)
```