// Vite config used only for the bundle-composition analysis run.
// Produces a JSON stats file (consumable from a shell) plus an HTML
// treemap for visual inspection. Not wired into the normal build.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { visualizer } from 'rollup-plugin-visualizer';

export default defineConfig({
  plugins: [
    react(),
    visualizer({
      filename: 'bench-stats/bundle.html',
      template: 'treemap',
      gzipSize: true,
      brotliSize: false,
      // Also emit a JSON-friendly raw size dump.
      emitFile: false,
    }),
    visualizer({
      filename: 'bench-stats/bundle.json',
      template: 'raw-data',
      gzipSize: true,
      brotliSize: false,
      emitFile: false,
    }),
  ],
});
