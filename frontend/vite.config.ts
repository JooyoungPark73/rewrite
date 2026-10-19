// Vite configuration for the rewrite frontend.
//
// - The React plugin gives JSX + fast refresh; the Tailwind plugin compiles
//   Tailwind v4 (and the DaisyUI plugin declared in src/index.css).
// - The dev server binds to 127.0.0.1 only (loopback is the security boundary,
//   see PLAN.md) and proxies /api (HTTP) and /ws (WebSocket) to the FastAPI
//   backend, so the browser only ever talks to one origin.
// - REWRITE_BACKEND overrides the proxy target. The e2e tests use it to
//   point the dev server at the mock backend in mock/server.mjs instead of the
//   real one on port 8000.
// - `npm run build` writes to frontend/dist, which the backend serves.
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const backend = process.env.REWRITE_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: backend },
      // ws: true makes Vite forward the WebSocket upgrade for /ws/events.
      '/ws': { target: backend, ws: true },
    },
  },
  preview: {
    host: '127.0.0.1',
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // pdf.js + CodeMirror make one ~1 MB bundle. The app is served from
    // loopback only, so bundle size does not matter; silence the warning.
    chunkSizeWarningLimit: 1500,
  },
})
