import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api';
import { deviceStatesStore } from '../../state/deviceStatesStore';
import { useTelemetryActions } from './useTelemetryActions';

const setup = () => {
  const appendUiErrorLog = vi.fn();
  const pushToast = vi.fn();
  const hook = renderHook(() => useTelemetryActions({ appendUiErrorLog, pushToast }));
  return { hook, appendUiErrorLog, pushToast };
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getStatus').mockResolvedValue({ connected: true, connecting: false });
});

describe('useTelemetryActions', () => {
  it.each([
    ['install', 'installTelemetry'],
    ['start', 'startTelemetry'],
    ['stop', 'stopTelemetry'],
    ['restart', 'restartTelemetry'],
    ['uninstall', 'uninstallTelemetry'],
  ] as const)('%s calls the matching API endpoint', async (command, method) => {
    const spy = vi.spyOn(api, method).mockResolvedValue({ ok: true } as never);
    const { hook, pushToast } = setup();

    await act(async () => {
      await hook.result.current.runTelemetryCommand('dev-1', command);
    });

    expect(spy).toHaveBeenCalledWith('dev-1');
    expect(pushToast).toHaveBeenCalledTimes(1);
  });

  it('marks the device busy while an action is in flight', async () => {
    let release!: () => void;
    vi.spyOn(api, 'installTelemetry').mockReturnValue(
      new Promise((resolve) => {
        release = () => resolve({ ok: true, version: '1.0.0', temperature_c: 50 });
      })
    );
    const { hook } = setup();

    let pending!: Promise<void>;
    act(() => {
      pending = hook.result.current.runTelemetryCommand('dev-1', 'install');
    });
    await waitFor(() => expect(hook.result.current.telemetryBusyKeys['dev-1']).toBe(true));

    await act(async () => {
      release();
      await pending;
    });
    expect(hook.result.current.telemetryBusyKeys['dev-1']).toBe(false);
  });

  it('warns when a start is accepted but the service is not running', async () => {
    // The gateway reports `systemctl start` succeeding while the unit died
    // immediately as ok:true, active:false. A plain "started" toast there would
    // claim success for a service that is not running.
    vi.spyOn(api, 'startTelemetry').mockResolvedValue({
      ok: true,
      active: false,
      state: 'failed',
    });
    const { hook, pushToast } = setup();

    await act(async () => {
      await hook.result.current.runTelemetryCommand('dev-1', 'start');
    });

    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({ level: 'warning' })
    );
  });

  it('reports a genuinely successful restart as success', async () => {
    vi.spyOn(api, 'restartTelemetry').mockResolvedValue({
      ok: true,
      active: true,
      state: 'active',
    });
    const { hook, pushToast } = setup();

    await act(async () => {
      await hook.result.current.runTelemetryCommand('dev-1', 'restart');
    });

    expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({ level: 'info' }));
  });

  it('stays busy until the refreshed status has landed', async () => {
    // Clearing busy before the refresh would render the card non-busy with its
    // pre-action state (e.g. still "not installed" right after a good install).
    // Hold the refresh open and assert the card is still busy while it runs.
    vi.spyOn(api, 'installTelemetry').mockResolvedValue({
      ok: true,
      version: '1.0.0',
      temperature_c: 50,
    });
    let releaseRefresh!: () => void;
    vi.spyOn(api, 'getStatus').mockReturnValue(
      new Promise((resolve) => {
        releaseRefresh = () => resolve({ connected: true, connecting: false });
      })
    );
    const { hook } = setup();

    let pending!: Promise<void>;
    act(() => {
      pending = hook.result.current.runTelemetryCommand('dev-1', 'install');
    });

    // The action has resolved and the refresh is in flight...
    await waitFor(() => expect(api.getStatus).toHaveBeenCalledWith('dev-1'));
    expect(hook.result.current.telemetryBusyKeys['dev-1']).toBe(true);

    await act(async () => {
      releaseRefresh();
      await pending;
    });
    expect(hook.result.current.telemetryBusyKeys['dev-1']).toBe(false);
  });

  it('reports a failed action without throwing', async () => {
    vi.spyOn(api, 'installTelemetry').mockRejectedValue(
      new Error('No bundled rp-telemetry binary')
    );
    const { hook, appendUiErrorLog, pushToast } = setup();

    await act(async () => {
      await hook.result.current.runTelemetryCommand('dev-1', 'install');
    });

    expect(appendUiErrorLog).toHaveBeenCalledWith(
      'rp_telemetry',
      'telemetry_install_failed',
      'No bundled rp-telemetry binary',
      'dev-1'
    );
    // Also surfaced as a toast: an SSH timeout takes ~20 s and the operator
    // should not have to open the logs modal to learn it failed.
    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({
        level: 'error',
        message: 'No bundled rp-telemetry binary',
      })
    );
  });

  it('refreshes the device status afterwards so the card updates immediately', async () => {
    vi.spyOn(api, 'restartTelemetry').mockResolvedValue({
      ok: true,
      active: true,
      state: 'active',
    });
    const status = {
      connected: true,
      connecting: false,
      rp_temperature_c: 61.2,
      rp_telemetry: { state: 'running' as const },
    };
    vi.spyOn(api, 'getStatus').mockResolvedValue(status);
    const { hook } = setup();

    await act(async () => {
      await hook.result.current.runTelemetryCommand('dev-9', 'restart');
    });

    expect(api.getStatus).toHaveBeenCalledWith('dev-9');
    expect(deviceStatesStore.getDeviceSnapshot('dev-9').status?.rp_temperature_c).toBe(
      61.2
    );
  });

  it('ignores a malformed status on refresh', async () => {
    vi.spyOn(api, 'startTelemetry').mockResolvedValue({
      ok: true,
      active: true,
      state: 'active',
    });
    vi.spyOn(api, 'getStatus').mockResolvedValue({ nonsense: true } as never);
    const { hook } = setup();

    await act(async () => {
      await hook.result.current.runTelemetryCommand('dev-bad', 'start');
    });

    expect(deviceStatesStore.getDeviceSnapshot('dev-bad').status).toBeUndefined();
  });

  it('installs on many devices and reports per-device failures', async () => {
    const spy = vi.spyOn(api, 'installTelemetryMany').mockResolvedValue({
      installed: ['a'],
      failed: { b: 'ssh timed out' },
    });
    const { hook, appendUiErrorLog, pushToast } = setup();

    await act(async () => {
      await hook.result.current.installTelemetryAll(['a', 'b']);
    });

    expect(spy).toHaveBeenCalledWith(['a', 'b']);
    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({ level: 'warning' })
    );
    expect(appendUiErrorLog).toHaveBeenCalledWith(
      'rp_telemetry',
      'telemetry_install_failed',
      'ssh timed out',
      'b'
    );
  });

  it('reports a clean bulk install', async () => {
    vi.spyOn(api, 'installTelemetryMany').mockResolvedValue({
      installed: ['a', 'b'],
      failed: {},
    });
    const { hook, pushToast, appendUiErrorLog } = setup();

    await act(async () => {
      await hook.result.current.installTelemetryAll(['a', 'b']);
    });

    expect(pushToast).toHaveBeenCalledWith(expect.objectContaining({ level: 'info' }));
    expect(appendUiErrorLog).not.toHaveBeenCalled();
  });

  it('marks every target busy for the duration of a bulk install', async () => {
    // Otherwise the cards stay clickable and a second install against the same
    // board races the first over the shared remote upload path.
    let release!: () => void;
    vi.spyOn(api, 'installTelemetryMany').mockReturnValue(
      new Promise((resolve) => {
        release = () => resolve({ installed: ['a', 'b'], failed: {} });
      })
    );
    const { hook } = setup();

    let pending!: Promise<void>;
    act(() => {
      pending = hook.result.current.installTelemetryAll(['a', 'b']);
    });
    await waitFor(() => expect(hook.result.current.telemetryBusyKeys['a']).toBe(true));
    expect(hook.result.current.telemetryBusyKeys['b']).toBe(true);

    await act(async () => {
      release();
      await pending;
    });
    expect(hook.result.current.telemetryBusyKeys['a']).toBe(false);
    expect(hook.result.current.telemetryBusyKeys['b']).toBe(false);
  });

  it('surfaces a failed bulk install as a toast, not just a log entry', async () => {
    vi.spyOn(api, 'installTelemetryMany').mockRejectedValue(new Error('gateway down'));
    const { hook, appendUiErrorLog, pushToast } = setup();

    await act(async () => {
      await hook.result.current.installTelemetryAll(['a', 'b']);
    });

    expect(appendUiErrorLog).toHaveBeenCalledWith(
      'rp_telemetry',
      'telemetry_install_failed',
      'gateway down'
    );
    expect(pushToast).toHaveBeenCalledWith(
      expect.objectContaining({ level: 'error', message: 'gateway down' })
    );
  });

  it('does nothing for an empty device list', async () => {
    const spy = vi.spyOn(api, 'installTelemetryMany');
    const { hook } = setup();

    await act(async () => {
      await hook.result.current.installTelemetryAll([]);
    });

    expect(spy).not.toHaveBeenCalled();
  });
});
