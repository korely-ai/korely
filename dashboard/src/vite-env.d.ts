/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Replaced at build time by vite.config.ts, from KORELY_BACKEND_URL. Empty
  // in every build that ships: the page calls the address it came from.
  readonly VITE_BACKEND_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
