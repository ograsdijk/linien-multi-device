import { describe, expect, it } from 'vitest';
import { isDeviceStatus, parseStreamMessage } from './messageGuards';

const base = { connected: true, connecting: false };

describe('isDeviceStatus with telemetry', () => {
  it('accepts a status without any telemetry fields', () => {
    expect(isDeviceStatus(base)).toBe(true);
  });

  it('accepts a valid telemetry payload', () => {
    expect(
      isDeviceStatus({
        ...base,
        rp_temperature_c: 57.34,
        rp_temperature_sampled_at: 1700000000.5,
        rp_telemetry: {
          state: 'running',
          version: '1.0.0',
          bundled_version: '1.0.0',
          update_available: false,
          installed: true,
          port: 18864,
          error: null,
        },
      })
    ).toBe(true);
  });

  it('accepts null telemetry fields', () => {
    expect(
      isDeviceStatus({
        ...base,
        rp_temperature_c: null,
        rp_temperature_sampled_at: null,
        rp_telemetry: null,
      })
    ).toBe(true);
  });

  it('rejects a non-finite temperature', () => {
    expect(isDeviceStatus({ ...base, rp_temperature_c: Number.NaN })).toBe(false);
    expect(isDeviceStatus({ ...base, rp_temperature_c: Infinity })).toBe(false);
  });

  it('rejects a non-numeric temperature', () => {
    expect(isDeviceStatus({ ...base, rp_temperature_c: '57.3' })).toBe(false);
  });

  it('rejects a non-numeric sample timestamp', () => {
    expect(isDeviceStatus({ ...base, rp_temperature_sampled_at: 'now' })).toBe(false);
  });

  it('accepts an unrecognised telemetry state rather than dropping the device', () => {
    // A failed guard discards the WHOLE status, so rejecting a state this
    // browser has not heard of yet (gateway upgraded, bundle cached) would
    // freeze the entire card -- connection, lock, recovery and all.
    expect(isDeviceStatus({ ...base, rp_telemetry: { state: 'on_fire' } })).toBe(true);
  });

  it('still rejects a non-string telemetry state', () => {
    expect(isDeviceStatus({ ...base, rp_telemetry: { state: 7 } })).toBe(false);
  });

  it('rejects telemetry without a state', () => {
    expect(isDeviceStatus({ ...base, rp_telemetry: { version: '1.0.0' } })).toBe(false);
  });

  it('tolerates unexpected telemetry field types rather than dropping the device', () => {
    // Every assertion inside the nested object is a way for one odd field to
    // blank a whole card, because a failed guard discards the entire status.
    // The display layer copes; losing connection/lock/recovery does not.
    expect(
      isDeviceStatus({ ...base, rp_telemetry: { state: 'running', version: 1 } })
    ).toBe(true);
    expect(
      isDeviceStatus({ ...base, rp_telemetry: { state: 'running', installed: 'yes' } })
    ).toBe(true);
    expect(
      isDeviceStatus({ ...base, rp_telemetry: { state: 'running', port: 'x' } })
    ).toBe(true);
  });

  it('matches how the sibling nested status objects are validated', () => {
    // diagnosis/recovery/auto_relock are isObject-checked only; telemetry is
    // no longer the odd one out.
    expect(isDeviceStatus({ ...base, diagnosis: { category: 7 } })).toBe(true);
    expect(isDeviceStatus({ ...base, rp_telemetry: { state: 'running', junk: 7 } })).toBe(
      true
    );
  });

  it('rejects a telemetry array', () => {
    expect(isDeviceStatus({ ...base, rp_telemetry: ['running'] })).toBe(false);
  });
});

describe('parseStreamMessage', () => {
  it('accepts a status message carrying telemetry', () => {
    const message = parseStreamMessage({
      type: 'status',
      ...base,
      rp_temperature_c: 57.3,
      rp_telemetry: { state: 'running' },
    });
    expect(message).not.toBeNull();
    expect(message?.type).toBe('status');
  });

  it('keeps a status message whose telemetry state is simply unrecognised', () => {
    // Forward compatibility: a newer gateway state must not blank the device.
    expect(
      parseStreamMessage({
        type: 'status',
        ...base,
        rp_telemetry: { state: 'nonsense' },
      })
    ).not.toBeNull();
  });

  it('drops a status message whose telemetry is not an object at all', () => {
    expect(
      parseStreamMessage({ type: 'status', ...base, rp_telemetry: ['running'] })
    ).toBeNull();
    expect(
      parseStreamMessage({ type: 'status', ...base, rp_telemetry: { version: '1.0.0' } })
    ).toBeNull();
  });
});
