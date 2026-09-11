import { describe, expect, it } from 'vitest';
import type { DeviceStatus } from '../../types';
import {
  TEMPERATURE_CRITICAL_C,
  TEMPERATURE_WARN_C,
  resolveTelemetryDisplay,
  resolveTemperatureTone,
} from './telemetryDisplay';

const status = (overrides: Partial<DeviceStatus>): DeviceStatus => ({
  connected: true,
  connecting: false,
  ...overrides,
});

describe('resolveTelemetryDisplay', () => {
  it('shows a live reading while running', () => {
    const display = resolveTelemetryDisplay(
      status({ rp_temperature_c: 57.34, rp_telemetry: { state: 'running' } })
    );
    expect(display.available).toBe(true);
    expect(display.value).toBe('57.3 °C');
    expect(display.detail).toBeNull();
    expect(display.action).toBeNull();
  });

  it('never presents a cached value as current once stale', () => {
    const display = resolveTelemetryDisplay(
      status({ rp_temperature_c: 57.3, rp_telemetry: { state: 'stale' } })
    );
    expect(display.available).toBe(false);
    expect(display.value).toBe('unavailable');
    expect(display.detail).toMatch(/no recent reading/i);
  });

  it('offers no action when staleness points at the gateway, not the board', () => {
    // A failed poll sets offline/stopped/error, so `stale` can only mean the
    // gateway stopped polling. Restarting the daemon would change nothing.
    const display = resolveTelemetryDisplay(status({ rp_telemetry: { state: 'stale' } }));
    expect(display.action).toBeNull();
    expect(display.detail).toMatch(/gateway/i);
  });

  it('offers Install when telemetry is not installed', () => {
    const display = resolveTelemetryDisplay(
      status({ rp_telemetry: { state: 'not_installed' } })
    );
    expect(display.value).toBe('unavailable');
    expect(display.detail).toBe('Telemetry not installed');
    expect(display.action).toBe('install');
    expect(display.actionLabel).toBe('Install');
  });

  it('offers Start when the service is stopped', () => {
    const display = resolveTelemetryDisplay(status({ rp_telemetry: { state: 'stopped' } }));
    expect(display.detail).toMatch(/stopped/i);
    expect(display.action).toBe('start');
  });

  it('offers no action when the board itself is unreachable', () => {
    // Restart is an SSH round trip to the same host that just failed to answer
    // TCP: the button would spin for the SSH timeout and then error.
    const display = resolveTelemetryDisplay(status({ rp_telemetry: { state: 'offline' } }));
    expect(display.detail).toMatch(/unreachable/i);
    expect(display.action).toBeNull();
    expect(display.actionLabel).toBeNull();
  });

  it('includes the daemon error text in the error state', () => {
    const display = resolveTelemetryDisplay(
      status({ rp_telemetry: { state: 'error', error: 'daemon error: XADC' } })
    );
    expect(display.detail).toContain('XADC');
  });

  it('reports a protocol mismatch', () => {
    const display = resolveTelemetryDisplay(
      status({ rp_telemetry: { state: 'version_mismatch' } })
    );
    expect(display.detail).toMatch(/protocol/i);
    expect(display.action).toBe('install');
  });

  it('offers Update when the board runs an older build', () => {
    const display = resolveTelemetryDisplay(
      status({
        rp_temperature_c: 50,
        rp_telemetry: {
          state: 'running',
          version: '0.9.0',
          bundled_version: '1.0.0',
          update_available: true,
        },
      })
    );
    // The reading is still live...
    expect(display.available).toBe(true);
    expect(display.value).toBe('50.0 °C');
    // ...and the update is offered alongside it.
    expect(display.action).toBe('update');
    expect(display.detail).toContain('0.9.0');
    expect(display.detail).toContain('1.0.0');
  });

  it('treats a missing status as not yet polled', () => {
    const display = resolveTelemetryDisplay(undefined);
    expect(display.value).toBe('unavailable');
    expect(display.detail).toMatch(/not polled/i);
    expect(display.action).toBeNull();
  });

  it('ignores a non-finite temperature', () => {
    const display = resolveTelemetryDisplay(
      status({ rp_temperature_c: Number.NaN, rp_telemetry: { state: 'running' } })
    );
    expect(display.available).toBe(false);
  });

  it('explains a running service that has not reported yet', () => {
    // Reachable right after a successful Install/Start: the gateway marks the
    // service running as soon as systemd reports it active, before the first
    // poll produces a reading. Must not be a bare dead-end "unavailable".
    const display = resolveTelemetryDisplay(
      status({ rp_temperature_c: null, rp_telemetry: { state: 'running' } })
    );
    expect(display.available).toBe(false);
    expect(display.value).toBe('unavailable');
    expect(display.detail).toBe('Waiting for the first reading');
    // Nothing for the operator to do but wait, so no remedy button.
    expect(display.action).toBeNull();
  });
});

describe('resolveTemperatureTone', () => {
  it('is neutral in the normal range', () => {
    expect(resolveTemperatureTone(57.3)).toBe('normal');
    expect(resolveTemperatureTone(TEMPERATURE_WARN_C - 0.1)).toBe('normal');
  });

  it('warns at the warning threshold', () => {
    expect(resolveTemperatureTone(TEMPERATURE_WARN_C)).toBe('warn');
    expect(resolveTemperatureTone(TEMPERATURE_CRITICAL_C - 0.1)).toBe('warn');
  });

  it('is critical at the rated maximum junction temperature', () => {
    expect(resolveTemperatureTone(TEMPERATURE_CRITICAL_C)).toBe('critical');
    expect(resolveTemperatureTone(95)).toBe('critical');
  });
});
