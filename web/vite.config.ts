import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

const apiProxyTarget = process.env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000';
const proxy = {
  '/api/v1': {
    target: apiProxyTarget,
    changeOrigin: true,
  },
};

export default defineConfig({
  root: resolve(import.meta.dirname),
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['pwa-icon.svg', 'icons/courier-192.png', 'icons/courier-512.png'],
      manifest: {
        lang: 'ru',
        name: 'LabTrack — курьер',
        short_name: 'Курьер',
        description: 'Офлайн-учёт забора и доставки лабораторных отправок в LabTrack.',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#f4f6f4',
        theme_color: '#123c3a',
        icons: [
          {
            src: '/icons/courier-192.png',
            sizes: '192x192',
            type: 'image/png',
            purpose: 'any',
          },
          {
            src: '/icons/courier-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any',
          },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{html,js,css,svg,webmanifest}'],
        navigateFallbackDenylist: [/^\/api\/v1(?:\/|$)/],
        runtimeCaching: [],
      },
      devOptions: { enabled: false },
    }),
  ],
  server: { proxy },
  preview: { proxy },
});
