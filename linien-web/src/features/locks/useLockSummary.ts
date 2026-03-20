import { useMemo } from 'react';
import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
  PlotFrame,
} from '../../types';
import { computeLockHealthSummary, resolveLockDisplay } from './lockState';

type DeviceStateLike = {
  params: Record<string, unknown>;
  plotFrame?: PlotFrame | null;
  status?: DeviceStatus | null;
};

export const useLockSummary = (
  devices: Device[],
  deviceStates: Record<string, DeviceStateLike>
) => {
  const deviceStatusMap = useMemo(() => {
    const map: Record<string, DeviceStatus | undefined> = {};
    devices.forEach((device) => {
      map[device.key] = deviceStates[device.key]?.status ?? undefined;
    });
    return map;
  }, [devices, deviceStates]);

  const lockIndicatorMap = useMemo(() => {
    const map: Record<string, LockIndicatorSnapshot | undefined> = {};
    devices.forEach((device) => {
      map[device.key] = deviceStates[device.key]?.plotFrame?.lock_indicator;
    });
    return map;
  }, [devices, deviceStates]);

  const autoRelockMap = useMemo(() => {
    const map: Record<string, AutoRelockStatus | undefined> = {};
    devices.forEach((device) => {
      const state = deviceStates[device.key];
      map[device.key] = state?.plotFrame?.auto_relock ?? state?.status?.auto_relock ?? undefined;
    });
    return map;
  }, [devices, deviceStates]);

  const lockStateMap = useMemo(() => {
    const map: Record<string, boolean | undefined> = {};
    devices.forEach((device) => {
      const state = deviceStates[device.key];
      const fromPlot = typeof state?.plotFrame?.lock === 'boolean' ? state.plotFrame.lock : undefined;
      const fromStatus = typeof state?.status?.lock === 'boolean' ? state.status.lock : undefined;
      map[device.key] = fromPlot ?? fromStatus;
    });
    return map;
  }, [devices, deviceStates]);

  const effectiveLockStateMap = useMemo(() => {
    const map: Record<string, boolean | undefined> = {};
    devices.forEach((device) => {
      const status = deviceStatusMap[device.key];
      const display = resolveLockDisplay({
        connected: Boolean(status?.connected),
        lockEnabled: lockStateMap[device.key],
        indicator: lockIndicatorMap[device.key],
      });
      map[device.key] = display.effectiveLocked;
    });
    return map;
  }, [devices, deviceStatusMap, lockStateMap, lockIndicatorMap]);

  const lockHealthSummary = useMemo(() => {
    return computeLockHealthSummary({
      devices,
      statusByKey: deviceStatusMap,
      lockByKey: lockStateMap,
      indicatorByKey: lockIndicatorMap,
    });
  }, [devices, deviceStatusMap, lockStateMap, lockIndicatorMap]);

  const connectedDeviceCount = useMemo(
    () => devices.reduce((count, device) => count + (deviceStatusMap[device.key]?.connected ? 1 : 0), 0),
    [devices, deviceStatusMap]
  );

  const lockedDeviceCount = useMemo(
    () =>
      devices.reduce(
        (count, device) =>
          count +
          (deviceStatusMap[device.key]?.connected && effectiveLockStateMap[device.key] === true ? 1 : 0),
        0
      ),
    [devices, deviceStatusMap, effectiveLockStateMap]
  );

  const connectedRelockEnabledCount = useMemo(
    () =>
      devices.reduce((count, device) => {
        if (!deviceStatusMap[device.key]?.connected) return count;
        return count + (autoRelockMap[device.key]?.enabled ? 1 : 0);
      }, 0),
    [devices, deviceStatusMap, autoRelockMap]
  );

  return {
    deviceStatusMap,
    lockIndicatorMap,
    autoRelockMap,
    lockStateMap,
    effectiveLockStateMap,
    lockHealthSummary,
    connectedDeviceCount,
    lockedDeviceCount,
    connectedRelockEnabledCount,
  };
};
