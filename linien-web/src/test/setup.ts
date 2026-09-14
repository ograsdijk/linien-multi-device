// Mantine components query `matchMedia` and `ResizeObserver`, neither of which
// jsdom implements. Minimal stubs are enough for the components under test.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

if (!(globalThis as Record<string, unknown>).ResizeObserver) {
  (globalThis as Record<string, unknown>).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// Testing Library only auto-cleans up when vitest globals are enabled; this
// project keeps globals off, so unmount explicitly between tests.
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});
