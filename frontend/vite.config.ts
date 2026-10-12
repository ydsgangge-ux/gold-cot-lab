import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev server talks to the local FastAPI process; in production the API and
    // the built assets are served by that same process on one origin.
    proxy: { '/api': 'http://127.0.0.1:9527' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})