import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import type { Device, DeviceStatus, InfluxCredentialsEntry } from '../../types';
import { useInfluxController } from './useInfluxController';

const device = (key: string): Device =>
  ({ key, name: key, host: '10.0.0.1', port: 18862 }) as Device;

const devices = [device('dev-1'), device('dev-2'), device('dev-3')];

const credentials = (measurement: string) => ({
  url: 'http://influx:8086',
  org: 'lab',
  token: 'secret',
  bucket: 'linien',
  measurement,
});

const entry = (measurement: string): InfluxCredentialsEntry => ({
  connected: true,
  credentials: credentials(measurement),
  error: null,
});

const statusMap: Record<string, DeviceStatus> = {
  'dev-1': { connected: true, logging_active: true } as DeviceStatus,
  'dev-2': { connected: true, logging_active: false } as DeviceStatus,
  'dev-3': { connected: false, logging_active: false } as DeviceStatus,
};

const setup = () =>
  renderHook(() =>
    useInfluxController({
      devices,
      activeDeviceKeys: [],
      deviceStatusMap: statusMap,
      onLoggingStateChange: vi.fn(),
    })
  );

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getParamMeta').mockResolvedValue([]);
  vi.spyOn(api, 'loggingGetAllCredentials').mockResolvedValue({
    'dev-1': entry('dev-1'),
    'dev-2': entry('dev-2'),
    'dev-3': { connected: false, credentials: null, error: null },
  });
});

describe('useInfluxController', () => {
  it('loads every device with one request when the panel opens', async () => {
    const perDevice = vi.spyOn(api, 'loggingGetCredentials');
    const hook = setup();

    act(() => hook.result.current.setInfluxPopoverOpen(true));

    await waitFor(() => expect(hook.result.current.influxFleet[1].bucket).toBe('linien'));
    expect(api.loggingGetAllCredentials).toHaveBeenCalledTimes(1);
    // The per-device endpoint is what forced the click-through; it must not be
    // how the panel loads any more.
    expect(perDevice).not.toHaveBeenCalled();
  });

  it('switching devices issues no further request', async () => {
    const hook = setup();
    act(() => hook.result.current.setInfluxPopoverOpen(true));
    await waitFor(() =>
      expect(api.loggingGetAllCredentials).toHaveBeenCalledTimes(1)
    );

    act(() => hook.result.current.setInfluxDeviceKey('dev-2'));

    await waitFor(() =>
      expect(hook.result.current.influxCredentials.measurement).toBe('dev-2')
    );
    expect(api.loggingGetAllCredentials).toHaveBeenCalledTimes(1);
  });

  it('summarises the whole fleet, including boards that are offline', async () => {
    const hook = setup();
    act(() => hook.result.current.setInfluxPopoverOpen(true));

    await waitFor(() => expect(hook.result.current.influxFleet).toHaveLength(3));
    const [first, , third] = hook.result.current.influxFleet;
    expect(first).toMatchObject({
      deviceKey: 'dev-1',
      connected: true,
      loggingActive: true,
      measurement: 'dev-1',
    });
    // An offline board still gets a row -- "no settings because it is offline"
    // is exactly what the operator needs to see.
    expect(third).toMatchObject({
      deviceKey: 'dev-3',
      connected: false,
      measurement: null,
    });
  });

  it('keeps the fleet map in step with a save', async () => {
    vi.spyOn(api, 'loggingUpdateCredentials').mockResolvedValue({
      success: true,
      message: 'Saved.',
    });
    const hook = setup();
    act(() => hook.result.current.setInfluxPopoverOpen(true));
    await waitFor(() => expect(hook.result.current.influxFleet[0].bucket).toBe('linien'));

    act(() => hook.result.current.updateInfluxCredential('bucket', 'other-bucket'));
    await act(async () => {
      await hook.result.current.saveInfluxCredentials();
    });

    // Without this the summary row and a later reselect would still show the
    // pre-save bucket.
    expect(hook.result.current.influxFleet[0].bucket).toBe('other-bucket');
  });

  it('surfaces a per-device error from the bulk response', async () => {
    vi.spyOn(api, 'loggingGetAllCredentials').mockResolvedValue({
      'dev-1': { connected: true, credentials: null, error: 'RPyC exploded' },
      'dev-2': entry('dev-2'),
      'dev-3': entry('dev-3'),
    });
    const hook = setup();

    act(() => hook.result.current.setInfluxPopoverOpen(true));

    await waitFor(() =>
      expect(hook.result.current.influxFleet[0].error).toBe('RPyC exploded')
    );
  });
});
