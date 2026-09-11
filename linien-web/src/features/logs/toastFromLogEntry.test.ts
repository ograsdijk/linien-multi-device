import { describe, expect, it } from 'vitest';
import type { UiLogEntry } from '../../types';
import { toastFromLogEntry } from './useLogsController';

const entry = (overrides: Partial<UiLogEntry>): UiLogEntry => ({
  id: '1',
  ts: 0,
  level: 30,
  level_name: 'warning',
  source: 'rp_telemetry',
  message: 'Red Pitaya telemetry unavailable (offline).',
  details: {},
  ...overrides,
});

describe('toastFromLogEntry — Red Pitaya telemetry', () => {
  it('warns on sustained telemetry loss', () => {
    // Emitted once per outage by the 30 s poll loop, which the operator never
    // triggered — without a toast it only appears in the logs modal.
    const toast = toastFromLogEntry(entry({ code: 'rp_telemetry_unavailable' }), 'Laser A');
    expect(toast?.level).toBe('warning');
    expect(toast?.title).toContain('Laser A');
  });

  it('warns on a version mismatch and on a failed Influx write', () => {
    expect(toastFromLogEntry(entry({ code: 'rp_telemetry_version_mismatch' }))?.level).toBe(
      'warning'
    );
    expect(
      toastFromLogEntry(entry({ code: 'rp_telemetry_influx_write_failed' }))?.level
    ).toBe('warning');
  });

  it('reports recovery as info', () => {
    expect(toastFromLogEntry(entry({ code: 'rp_telemetry_recovered' }))?.level).toBe('info');
    expect(
      toastFromLogEntry(entry({ code: 'rp_telemetry_influx_write_recovered' }))?.level
    ).toBe('info');
  });

  it('does not toast operator-triggered failures twice', () => {
    // useTelemetryActions already toasts these from the HTTP error; listing
    // them here as well would show two toasts for one failed click.
    expect(
      toastFromLogEntry(
        entry({ code: 'rp_telemetry_install_failed', level: 40, level_name: 'error' })
      )
    ).toBeNull();
    expect(
      toastFromLogEntry(
        entry({
          code: 'rp_telemetry_service_action_failed',
          level: 40,
          level_name: 'error',
        })
      )
    ).toBeNull();
  });

  it('stays quiet for routine telemetry bookkeeping', () => {
    expect(
      toastFromLogEntry(entry({ code: 'rp_telemetry_installed', level: 20, level_name: 'info' }))
    ).toBeNull();
  });
});
