import { useEffect } from 'react';
import { api } from '../../api';
import type { Device, DeviceStatus, PlotFrame } from '../../types';
import { isDeviceStatus } from '../runtime/messageGuards';

type DeviceStateLike = {
  params: Record<string, unknown>;
  plotFrame?: PlotFrame | null;
  status?: DeviceStatus | null;
};

type UseDeviceStatusPollingArgs = {
  devices: Device[];
  setDeviceStates: React.Dispatch<React.SetStateAction<Record<string, DeviceStateLike>>>;
  intervalMs?: number;
  skipDeviceKeys?: ReadonlySet<string>;
};

const sameAutoRelock = (
  a: DeviceStatus['auto_relock'] | null | undefined,
  b: DeviceStatus['auto_relock'] | null | undefined
) => {
  if (a === b) return true;
  if (!a || !b) return !a && !b;
  return (
    a.enabled === b.enabled &&
    a.state === b.state &&
    a.attempts === b.attempts &&
    a.max_attempts === b.max_attempts &&
    a.cooldown_remaining_s === b.cooldown_remaining_s &&
    a.last_trigger_at === b.last_trigger_at &&
    a.last_attempt_at === b.last_attempt_at &&
    a.last_success_at === b.last_success_at &&
    a.last_failure_at === b.last_failure_at &&
    a.last_error === b.last_error
  );
};

const sameDeviceStatus = (a: DeviceStatus | null | undefined, b: DeviceStatus) => {
  if (!a) return false;
  return (
    a.connected === b.connected &&
    a.connecting === b.connecting &&
    a.last_error === b.last_error &&
    a.last_plot === b.last_plot &&
    a.logging_active === b.logging_active &&
    a.lock === b.lock &&
    sameAutoRelock(a.auto_relock, b.auto_relock)
  );
};

export const useDeviceStatusPolling = ({
  devices,
  setDeviceStates,
  intervalMs = 5000,
  skipDeviceKeys,
}: UseDeviceStatusPollingArgs) => {
  useEffect(() => {
    const interval = setInterval(() => {
      devices.forEach((device) => {
        if (skipDeviceKeys?.has(device.key)) {
          return;
        }
        api.getStatus(device.key)
          .then((status) => {
            if (!isDeviceStatus(status)) return;
            setDeviceStates((prev) => {
              const current = prev[device.key] || { params: {} };
              if (sameDeviceStatus(current.status, status)) {
                return prev;
              }
              return {
                ...prev,
                [device.key]: { ...current, status },
              };
            });
          })
          .catch(() => null);
      });
    }, intervalMs);
    return () => clearInterval(interval);
  }, [devices, intervalMs, setDeviceStates, skipDeviceKeys]);
};
