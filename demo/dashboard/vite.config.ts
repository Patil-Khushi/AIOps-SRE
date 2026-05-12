import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// Served by FastAPI at /dashboard/ in production; `npm run dev` runs at /
// and proxies API + WebSocket calls to uvicorn on :8765.
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/dashboard/' : '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://localhost:8765',
      '/ws': { target: 'ws://localhost:8765', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
}));
