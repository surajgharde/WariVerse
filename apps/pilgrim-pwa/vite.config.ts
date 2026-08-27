import { fileURLToPath, URL } from 'node:url'

import preact from '@preact/preset-vite'
import { defineConfig } from 'vite'

const CORE_API = process.env.VITE_CORE_API_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [preact()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5174,
    proxy: { '/api': { target: CORE_API, changeOrigin: true } },
  },
  build: {
    // Section 4/M7: "first load < 200 KB gzipped JS". `npm run budget` asserts
    // it after every build and fails loudly — a performance budget nobody
    // measures is a performance budget that has already been exceeded.
    //
    // ES2020 rather than ESNext: the target device is a 2016 Android, which
    // means Chrome 55-ish at worst through WebView. Optional chaining and
    // nullish coalescing get transpiled; the rest is native.
    target: 'es2020',
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        // One chunk. Code-splitting trades a smaller first paint for extra
        // round trips, and on 2G a round trip costs more than the bytes it
        // saves. The whole app is small enough to send at once.
        manualChunks: undefined,
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
