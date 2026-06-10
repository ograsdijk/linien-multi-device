import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    strictPort: true,
  },
  build: {
    rollupOptions: {
      output: {
        // Split heavy third-party code into separate chunks. Doesn't
        // reduce total bytes, but enables parallel HTTP/2 downloads
        // and keeps vendor cache stable when only app code changes.
        // Bundle composition measured Jun 2026 (M4 bench):
        //   @mantine/core         374 KB  - UI primitives, every panel
        //   uplot                 142 KB  - plot rendering, every card
        //   @dnd-kit/*            114 KB  - drag-and-drop, App root
        //   react-dom + react     140 KB  - React runtime
        //   @floating-ui/*         50 KB  - popover positioning
        //   react-number-format    55 KB  - numeric inputs
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('/uplot/')) return 'vendor-uplot';
          if (id.includes('/@mantine/') || id.includes('/@floating-ui/')) return 'vendor-mantine';
          if (id.includes('/@dnd-kit/')) return 'vendor-dnd';
          if (id.includes('/react-number-format/')) return 'vendor-number-format';
          if (id.includes('/react/') || id.includes('/react-dom/') || id.includes('/scheduler/')) {
            return 'vendor-react';
          }
          return undefined;
        },
      },
    },
  },
});
