import { describe, expect, it } from 'vitest';
import type { DeviceStatus } from '../../types';
import { sameDeviceStatus } from './useDeviceStatusPolling';

const status = (overrides: Partial<DeviceStatus> = {}): DeviceStatus => ({
  connected: true,
  connecting: false,
  last_error: null,
  last_plot: 1,
  logging_active: false,
  lock: false,
  rp_temperature_c: 57.3,
  rp_temperature_sampled_at: 1700,
  rp_telemetry: { state: 'running', version: '1.0.0', installed: true },
  ...overrides,
});

describe('sameDeviceStatus', () => {
  it('treats identical statuses as unchanged', () => {
    expect(sameDeviceStatus(status(), status())).toBe(true);
  });

  it('notices a temperature change', () => {
    expect(sameDeviceStatus(status(), status({ rp_temperature_c: 58.1 }))).toBe(false);
  });

  it('ignores a refreshed sample timestamp when nothing else moved', () => {
    // The gateway refreshes sampled_at on every successful poll and excludes it
    // from its own change signature; nothing in the UI reads it. Comparing it
    // would mark every device changed every 30 s for no visible difference.
    expect(
      sameDeviceStatus(status(), status({ rp_temperature_sampled_at: 1730 }))
    ).toBe(true);
  });

  it('still notices a temperature change that arrives with a new timestamp', () => {
    expect(
      sameDeviceStatus(
        status(),
        status({ rp_temperature_c: 58.1, rp_temperature_sampled_at: 1730 })
      )
    ).toBe(false);
  });

  it('notices a running -> stale transition at the same temperature', () => {
    expect(
      sameDeviceStatus(
        status(),
        status({
          rp_temperature_sampled_at: 1730,
          rp_telemetry: { state: 'stale', version: '1.0.0', installed: true },
        })
      )
    ).toBe(false);
  });

  it('notices a telemetry state change', () => {
    expect(
      sameDeviceStatus(
        status(),
        status({ rp_telemetry: { state: 'offline', version: '1.0.0', installed: true } })
      )
    ).toBe(false);
  });

  it('notices telemetry appearing or disappearing', () => {
    expect(sameDeviceStatus(status({ rp_telemetry: null }), status())).toBe(false);
    expect(sameDeviceStatus(status(), status({ rp_telemetry: null }))).toBe(false);
  });

  it('notices the gateway bundling a newer telemetry build', () => {
    // Upgrading the gateway changes only bundled_version: state, version,
    // installed and update_available all stay put. The card renders it
    // ("0.9.0 installed, 1.1.0 available"), so it has to count as a change.
    expect(
      sameDeviceStatus(
        status({
          rp_telemetry: {
            state: 'running',
            version: '0.9.0',
            bundled_version: '1.0.0',
            update_available: true,
            installed: true,
          },
        }),
        status({
          rp_telemetry: {
            state: 'running',
            version: '0.9.0',
            bundled_version: '1.1.0',
            update_available: true,
            installed: true,
          },
        })
      )
    ).toBe(false);
  });

  it('notices an update becoming available', () => {
    expect(
      sameDeviceStatus(
        status(),
        status({
          rp_telemetry: {
            state: 'running',
            version: '1.0.0',
            installed: true,
            update_available: true,
          },
        })
      )
    ).toBe(false);
  });

  it('notices a telemetry error change', () => {
    expect(
      sameDeviceStatus(
        status({ rp_telemetry: { state: 'error', error: 'a' } }),
        status({ rp_telemetry: { state: 'error', error: 'b' } })
      )
    ).toBe(false);
  });

  it('still notices the pre-existing fields', () => {
    expect(sameDeviceStatus(status(), status({ connected: false }))).toBe(false);
    expect(sameDeviceStatus(status(), status({ lock: true }))).toBe(false);
    expect(sameDeviceStatus(status(), status({ last_error: 'boom' }))).toBe(false);
  });

  it('treats a missing previous status as changed', () => {
    expect(sameDeviceStatus(undefined, status())).toBe(false);
    expect(sameDeviceStatus(null, status())).toBe(false);
  });
});
