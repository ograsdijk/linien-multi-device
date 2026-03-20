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
};

export const useDeviceStatusPolling = ({
  devices,
  setDeviceStates,
  intervalMs = 5000,
}: UseDeviceStatusPollingArgs) => {
  useEffect(() => {
    const interval = setInterval(() => {
      devices.forEach((device) => {
        api.getStatus(device.key)
          .then((status) => {
            if (!isDeviceStatus(status)) return;
            setDeviceStates((prev) => {
              const next = { ...prev };
              const current = next[device.key] || { params: {} };
              next[device.key] = { ...current, status };
              return next;
            });
          })
          .catch(() => null);
      });
    }, intervalMs);
    return () => clearInterval(interval);
  }, [devices, intervalMs, setDeviceStates]);
};
