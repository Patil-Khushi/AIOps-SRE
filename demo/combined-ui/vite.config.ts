import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Mounted at /combined/ on the FastAPI server, so all built asset URLs
// need that prefix. Dev mode proxies /api to the running uvicorn.
export default defineConfig({
  base: '/combined/',
  plugins: [react()],
  server: {
    port: 5175,
    proxy: {
      '/api': 'http://localhost:8765',
    },
  },
});
