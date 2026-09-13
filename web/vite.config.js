import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const root = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root,
  base: './',
  server: {
    port: 5173,
    strictPort: true,
    fs: {
      strict: true,
      allow: [root],
      deny: ['.env', '.env.*', '**/.git/**', '**/.personal/**', '**/AGENTS.md'],
    },
  },
  preview: { port: 4173, strictPort: true },
});
