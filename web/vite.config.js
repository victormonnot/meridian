import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const root = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root,
  base: './',
  build: {
    rollupOptions: {
      input: {
        explorer: fileURLToPath(new URL('index.html', import.meta.url)),
        workspace: fileURLToPath(new URL('workspace.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api/v1': process.env.MERIDIAN_API_PROXY || 'http://127.0.0.1:8000' },
    fs: {
      strict: true,
      allow: [root],
      deny: ['.env', '.env.*', '**/.git/**', '**/.personal/**', '**/AGENTS.md'],
    },
  },
  preview: { port: 4173, strictPort: true },
});
