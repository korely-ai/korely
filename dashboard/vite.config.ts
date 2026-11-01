import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// The backend URL, forwarded from KORELY_BACKEND_URL at build time.
//
// The default is EMPTY, and that matters. It used to be "http://localhost:8000",
// which is a sensible value for a developer running the dev server and a
// catastrophic one for an install: `define` substitutes a literal, so
// `import.meta.env.VITE_BACKEND_URL` was always a truthy string and
// `config/backendUrl.ts` never reached its `window.location.origin` fallback.
// The dashboard served from a customer's machine asked THEIR browser to call
// localhost:8000, which is that person's own laptop.
//
// Empty means "wherever this page came from", which is right for every build
// that ships. A developer running `npm run dev` on 5174 against an API on 8000
// sets KORELY_BACKEND_URL and gets the old behaviour back.
export default defineConfig({
  plugins: [react()],
  define: {
    "import.meta.env.VITE_BACKEND_URL": JSON.stringify(
      process.env.KORELY_BACKEND_URL || process.env.VITE_BACKEND_URL || "",
    ),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "0.0.0.0",
    // 5174, not Vite's default 5173, so it does not collide with another
    // Vite project running on the same machine.
    port: 5174,
  },
  // No source maps in the build that ships (audit of 17 Oct 2026):
  // they doubled what the image carried and the application served (a 1.8 MB
  // map next to a 0.5 MB page), for nobody. `npm run dev` has its own.
  build: {
    sourcemap: false,
  },
});
