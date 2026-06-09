import { useCallback, useEffect, useRef } from 'react';
import type { DeviceStatus, StreamMessage } from '../../types';
import { deviceStatesStore } from '../../state/deviceStatesStore';

// Coalesce frequent param_update messages from multiple devices into a
// single store update per animation frame. Without this, a busy system
// with many devices can fire hundreds of writes per second, each
// notifying every subscriber.
type PendingParams = Map<string, Map<string, unknown>>;

export const useDeviceStateUpdater = () => {
  const pendingParamsRef = useRef<PendingParams>(new Map());
  const flushScheduledRef = useRef(false);
  const rafIdRef = useRef<number | null>(null);
  const timeoutIdRef = useRef<number | null>(null);

  const flushPendingParams = useCallback(() => {
    flushScheduledRef.current = false;
    rafIdRef.current = null;
    timeoutIdRef.current = null;
    const pending = pendingParamsRef.current;
    if (pending.size === 0) return;
    pendingParamsRef.current = new Map();
    const entries: Array<{
      deviceKey: string;
      updater: (
        prev: Parameters<typeof deviceStatesStore.updateDevice>[1] extends (p: infer P) => unknown
          ? P
          : never
      ) => ReturnType<Parameters<typeof deviceStatesStore.updateDevice>[1]>;
    }> = [];
    for (const [deviceKey, paramMap] of pending) {
      if (paramMap.size === 0) continue;
      entries.push({
        deviceKey,
        updater: (prev) => {
          let hasDiff = false;
          for (const [name, value] of paramMap) {
            if (prev.params[name] !== value) {
              hasDiff = true;
              break;
            }
          }
          if (!hasDiff) return undefined;
          const nextParams = { ...prev.params };
          for (const [name, value] of paramMap) {
            nextParams[name] = value;
          }
          return { ...prev, params: nextParams };
        },
      });
    }
    if (entries.length > 0) {
      deviceStatesStore.batchUpdate(entries);
    }
  }, []);

  const schedule = useCallback(() => {
    if (flushScheduledRef.current) return;
    flushScheduledRef.current = true;
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      rafIdRef.current = window.requestAnimationFrame(flushPendingParams);
    } else {
      timeoutIdRef.current = window.setTimeout(flushPendingParams, 16);
    }
  }, [flushPendingParams]);

  useEffect(() => {
    return () => {
      if (rafIdRef.current !== null && typeof window !== 'undefined') {
        window.cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      if (timeoutIdRef.current !== null && typeof window !== 'undefined') {
        window.clearTimeout(timeoutIdRef.current);
        timeoutIdRef.current = null;
      }
    };
  }, []);

  return useCallback(
    (deviceKey: string, message: StreamMessage) => {
      if (message.type === 'plot_frame') {
        deviceStatesStore.updateDevice(deviceKey, (prev) => ({
          ...prev,
          plotFrame: message,
        }));
        return;
      }

      if (message.type === 'param_update') {
        let deviceMap = pendingParamsRef.current.get(deviceKey);
        if (!deviceMap) {
          deviceMap = new Map();
          pendingParamsRef.current.set(deviceKey, deviceMap);
        }
        deviceMap.set(message.name, message.value);
        schedule();
        return;
      }

      if (message.type === 'status') {
        deviceStatesStore.updateDevice(deviceKey, (prev) => ({
          ...prev,
          status: message as DeviceStatus,
        }));
      }
    },
    [schedule]
  );
};
