import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // ws: true 让 /api/v1/notifications/stream 的 WebSocket 握手在开发环境也能代理到后端。
  server: { port: 5173, proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true } } },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          antd: ['antd', '@ant-design/icons'],
          charts: ['echarts/core', 'echarts-for-react/lib/core'],
          sql: ['highlight.js', 'sql-formatter'],
        },
      },
    },
  },
});
