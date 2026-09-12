import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/console/',
  plugins: [react()],
  build: { outDir: '../src/ravn/static/console', emptyOutDir: true },
});
