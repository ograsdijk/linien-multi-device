import { useState } from 'react';
import { api } from '../../api';
import type { AutoRelockStatus } from '../../types';
import { deviceStatesStore } from '../../state/deviceStatesStore';

const toErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

type UseLockActionsArgs = {
  appendUiErrorLog: (source: string, code: string, message: string, deviceKey?: string) => void;
};

export const useLockActions = ({ appendUiErrorLog }: UseLockActionsArgs) => {
  const [lockBusyKeys, setLockBusyKeys] = useState<Record<string, boolean>>({});
  const [autoLockBusyKeys, setAutoLockBusyKeys] = useState<Record<string, boolean>>({});
  const [autoRelockBusyKeys, setAutoRelockBusyKeys] = useState<Record<string, boolean>>({});

  const updateAutoRelockStatus = (
    deviceKey: string,
    autoRelock: AutoRelockStatus | null | undefined
  ) => {
    deviceStatesStore.updateDevice(deviceKey, (prev) => ({
      ...prev,
      plotFrame:
        prev.plotFrame == null
          ? prev.plotFrame
          : { ...prev.plotFrame, auto_relock: autoRelock ?? undefined },
      status: {
        ...(prev.status ?? {
          connected: true,
          connecting: false,
        }),
        auto_relock: autoRelock,
      },
    }));
  };

  const updateLockState = (deviceKey: string, lock: boolean | null | undefined) => {
    deviceStatesStore.updateDevice(deviceKey, (prev) => ({
      ...prev,
      plotFrame:
        prev.plotFrame == null || lock == null
          ? prev.plotFrame
          : { ...prev.plotFrame, lock: Boolean(lock) },
      status: {
        ...(prev.status ?? {
          connected: true,
          connecting: false,
        }),
        lock,
      },
    }));
  };

  const toggleAutoRelock = async (deviceKey: string, enabled: boolean) => {
    setAutoRelockBusyKeys((prev) => ({ ...prev, [deviceKey]: true }));
    try {
      const state = await api.setAutoRelockEnabled(deviceKey, enabled);
      updateAutoRelockStatus(deviceKey, state.status);
    } catch (error) {
      appendUiErrorLog(
        'auto_relock',
        'auto_relock_toggle_failed',
        toErrorMessage(error, 'Failed to toggle auto relock.'),
        deviceKey
      );
    } finally {
      setAutoRelockBusyKeys((prev) => ({ ...prev, [deviceKey]: false }));
    }
  };

  const disableLock = async (deviceKey: string) => {
    setLockBusyKeys((prev) => ({ ...prev, [deviceKey]: true }));
    try {
      await api.stopLock(deviceKey);
      updateLockState(deviceKey, false);
    } catch (error) {
      appendUiErrorLog(
        'lock',
        'disable_lock_failed',
        toErrorMessage(error, 'Failed to disable lock.'),
        deviceKey
      );
    } finally {
      setLockBusyKeys((prev) => ({ ...prev, [deviceKey]: false }));
    }
  };

  const startAutoLockFromHeader = async (deviceKey: string) => {
    setAutoLockBusyKeys((prev) => ({ ...prev, [deviceKey]: true }));
    try {
      const settings = await api.getAutoLockScanSettings(deviceKey);
      await api.autoLockFromScan(deviceKey, settings);
      updateLockState(deviceKey, true);
    } catch (error) {
      appendUiErrorLog(
        'auto_lock_scan',
        'auto_lock_scan_failed',
        toErrorMessage(error, 'Auto lock failed.'),
        deviceKey
      );
    } finally {
      setAutoLockBusyKeys((prev) => ({ ...prev, [deviceKey]: false }));
    }
  };

  return {
    lockBusyKeys,
    autoLockBusyKeys,
    autoRelockBusyKeys,
    toggleAutoRelock,
    disableLock,
    startAutoLockFromHeader,
  };
};
