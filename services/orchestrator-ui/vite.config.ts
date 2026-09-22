import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Local `npm run dev` convenience only -- nginx.conf does the equivalent proxying in the
    // built image, so src/api.ts can always assume same-origin /api and /ws.
    proxy: {
      '/api': { target: 'http://localhost:8090', changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, '') },
      '/ws': { target: 'ws://localhost:8090', ws: true },
    },
  },
})
