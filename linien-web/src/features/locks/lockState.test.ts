import { describe, expect, it } from 'vitest';
import type { LockIndicatorSnapshot } from '../../types';
import { resolveLockDisplay } from './lockState';

const indicator = (
  state: string,
  reasons: string[] = []
): LockIndicatorSnapshot => ({ state, reasons }) as unknown as LockIndicatorSnapshot;

describe('resolveLockDisplay', () => {
  // An engaged lock with no indicator used to report 'unknown'/dimmed, which is
  // exactly what a disengaged one reports. Three surfaces draw this tag -- the
  // device row, the overview card and the header lock popover -- so on and off
  // looked identical in all of them and only the text told them apart.
  it('separates an engaged lock from a disengaged one without reading the text', () => {
    const on = resolveLockDisplay({ connected: true, lockEnabled: true });
    const off = resolveLockDisplay({ connected: true, lockEnabled: false });

    expect(on.uiState).not.toBe(off.uiState);
    expect(on.color).not.toBe(off.color);
    expect(on.effectiveLocked).toBe(true);
    expect(off.effectiveLocked).toBe(false);
  });

  it('keeps a confirmed lock distinct from one merely reported on', () => {
    const on = resolveLockDisplay({ connected: true, lockEnabled: true });
    const confirmed = resolveLockDisplay({
      connected: true,
      lockEnabled: true,
      indicator: indicator('locked'),
    });

    expect(confirmed.color).toBe('green');
    expect(on.color).not.toBe(confirmed.color);
  });

  it('still reports a disconnected device as having no lock state at all', () => {
    const display = resolveLockDisplay({ connected: false, lockEnabled: true });
    expect(display.uiState).toBe('unknown');
    expect(display.color).toBe('dimmed');
    expect(display.effectiveLocked).toBe(false);
  });

  it('lets a lost or marginal indicator override the engaged tone', () => {
    expect(
      resolveLockDisplay({
        connected: true,
        lockEnabled: true,
        indicator: indicator('lost'),
      }).color
    ).toBe('red');
    expect(
      resolveLockDisplay({
        connected: true,
        lockEnabled: true,
        indicator: indicator('marginal'),
      }).color
    ).toBe('orange');
  });
});
