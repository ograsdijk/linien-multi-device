import { useState } from 'react';
import { api } from '../../api';
import type { AutoRelockStatus, DeviceStatus, PlotFrame } from '../../types';

const toErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

type DeviceStateLike = {
  params: Record<string, unknown>;
  plotFrame?: PlotFrame | null;
  status?: DeviceStatus | null;
};

type UseLockActionsArgs = {
  setDeviceStates: React.Dispatch<React.SetStateAction<Record<string, DeviceStateLike>>>;
  appendUiErrorLog: (source: string, code: string, message: string, deviceKey?: string) => void;
};

export const useLockActions = ({ setDeviceStates, appendUiErrorLog }: UseLockActionsArgs) => {
  const [lockBusyKeys, setLockBusyKeys] = useState<Record<string, boolean>>({});
  const [autoLockBusyKeys, setAutoLockBusyKeys] = useState<Record<string, boolean>>({});
  const [autoRelockBusyKeys, setAutoRelockBusyKeys] = useState<Record<string, boolean>>({});

  const updateAutoRelockStatus = (
    deviceKey: string,
    autoRelock: AutoRelockStatus | null | undefined
  ) => {
    setDeviceStates((prev) => {
      const current = prev[deviceKey] || { params: {} };
      return {
        ...prev,
        [deviceKey]: {
          ...current,
          plotFrame:
            current.plotFrame == null
              ? current.plotFrame
              : { ...current.plotFrame, auto_relock: autoRelock ?? undefined },
          status: {
            ...(current.status ?? {
              connected: true,
              connecting: false,
            }),
            auto_relock: autoRelock,
          },
        },
      };
    });
  };

  const updateLockState = (deviceKey: string, lock: boolean | null | undefined) => {
    setDeviceStates((prev) => {
      const current = prev[deviceKey] || { params: {} };
      return {
        ...prev,
        [deviceKey]: {
          ...current,
          plotFrame:
            current.plotFrame == null || lock == null
              ? current.plotFrame
              : { ...current.plotFrame, lock: Boolean(lock) },
          status: {
            ...(current.status ?? {
              connected: true,
              connecting: false,
            }),
            lock,
          },
        },
      };
    });
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
