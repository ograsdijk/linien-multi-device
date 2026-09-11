import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Unit tests for the pure logic (status guards, equality, telemetry display)
// plus a few light component/hook tests under jsdom. Kept separate from
// vite.config.ts so the production build config stays untouched.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
