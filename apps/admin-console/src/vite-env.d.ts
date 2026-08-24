/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional basemap style URL. Unset means the flat, offline-safe style. */
  readonly VITE_MAP_STYLE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
