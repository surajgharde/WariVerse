import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api to the core API so the browser sees one origin.
// That keeps CORS out of local development entirely, and means the production
// nginx config (one origin, two upstreams) is the shape we developed against
// rather than a surprise at deploy time.
const CORE_API = process.env.VITE_CORE_API_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: CORE_API,
        changeOrigin: true,
        // The command centre's live feed is a WebSocket at /api/v1/ws/crowd.
        ws: true,
      },
    },
  },
  build: {
    // MapLibre is large and rarely changes. Splitting it keeps a console
    // redeploy from invalidating a 900 kB chunk on every operator's machine —
    // which matters on temple wifi during the Wari.
    rollupOptions: {
      output: {
        manualChunks: { maplibre: ['maplibre-gl'] },
      },
    },
    // The maplibre chunk is ~800 kB and that is the point of splitting it out.
    // Warning on the chunk we deliberately created teaches people to ignore
    // the warning, so the limit is raised to just above it — a *new* oversized
    // chunk still trips it.
    chunkSizeWarningLimit: 850,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
