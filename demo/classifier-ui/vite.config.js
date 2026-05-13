import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Mounted at /classifier/ on the FastAPI server, so all built asset URLs
// need that prefix. Dev mode proxies /api to the running uvicorn.
export default defineConfig({
    base: '/classifier/',
    plugins: [react()],
    server: {
        port: 5174,
        proxy: {
            '/api': 'http://localhost:8765',
        },
    },
});
