import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Mounted at /hitl/ on the FastAPI server, so all built asset URLs need
// that prefix. Dev mode (`npm run dev`) proxies /api to the running uvicorn.
export default defineConfig({
    base: '/hitl/',
    plugins: [react()],
    server: {
        port: 5175,
        proxy: {
            '/api': 'http://localhost:8765',
        },
    },
});
