import { startTransition, useCallback } from 'react';
import type { DeviceStatus, PlotFrame, StreamMessage } from '../../types';

type DeviceStateLike = {
  params: Record<string, unknown>;
  plotFrame?: PlotFrame | null;
  status?: DeviceStatus | null;
};

export const useDeviceStateUpdater = (
  setDeviceStates: React.Dispatch<React.SetStateAction<Record<string, DeviceStateLike>>>
) => {
  return useCallback((deviceKey: string, message: StreamMessage) => {
    if (message.type === 'plot_frame') {
      startTransition(() => {
        setDeviceStates((prev) => {
          const current = prev[deviceKey] || { params: {} };
          return {
            ...prev,
            [deviceKey]: {
              ...current,
              plotFrame: message,
            },
          };
        });
      });
      return;
    }

    setDeviceStates((prev) => {
      const current = prev[deviceKey] || { params: {} };
      if (message.type === 'param_update') {
        return {
          ...prev,
          [deviceKey]: {
            ...current,
            params: { ...current.params, [message.name]: message.value },
          },
        };
      }
      if (message.type === 'status') {
        return {
          ...prev,
          [deviceKey]: {
            ...current,
            status: message as DeviceStatus,
          },
        };
      }
      return prev;
    });
  }, [setDeviceStates]);
};
