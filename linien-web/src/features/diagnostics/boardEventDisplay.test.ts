import { describe, expect, it } from 'vitest';
import type { BoardEvent } from '../../types';
import { countReboots, resolveBoardEventDisplay } from './boardEventDisplay';

const event = (kind: string): BoardEvent =>
  ({ ts: 1, device_key: 'dev-1', kind, detail: '' }) as BoardEvent;

describe('resolveBoardEventDisplay', () => {
  it.each([
    ['reboot_detected', 'Rebooted', 'red'],
    ['disconnected', 'Connection lost', 'red'],
    ['telemetry_recovered', 'Telemetry back', 'green'],
    ['persistent_log_enabled', 'Persistent logs on', 'green'],
  ])('%s reads as "%s"', (kind, label, tone) => {
    const display = resolveBoardEventDisplay(event(kind));
    expect(display.label).toBe(label);
    expect(display.tone).toBe(tone);
  });

  it('falls back to the raw kind rather than rendering nothing', () => {
    // A gateway newer than the UI must not produce blank timeline rows.
    const display = resolveBoardEventDisplay(event('something_new'));
    expect(display.label).toBe('something_new');
    expect(display.tone).toBe('dimmed');
  });
});

describe('countReboots', () => {
  it('is the headline number a raw list buries', () => {
    const events = [
      event('reboot_detected'),
      event('disconnected'),
      event('reboot_detected'),
      event('diagnosis'),
    ];
    expect(countReboots(events)).toBe(2);
  });

  it('is zero for a quiet board', () => {
    expect(countReboots([event('telemetry_recovered')])).toBe(0);
    expect(countReboots([])).toBe(0);
  });
});
