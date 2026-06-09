import { useEffect, useMemo, useRef } from 'react';
import { api } from '../../api';
import type { Device, DeviceStatus } from '../../types';
import { isDeviceStatus } from '../runtime/messageGuards';
import { deviceStatesStore } from '../../state/deviceStatesStore';

type UseDeviceStatusPollingArgs = {
  devices: Device[];
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
  intervalMs = 5000,
  skipDeviceKeys,
}: UseDeviceStatusPollingArgs) => {
  // Keep the set of websocket-active device keys in a ref so frequent
  // open/close churn (every plot frame can change `streamingDeviceKeys`
  // upstream) does not tear down and restart the polling interval, which
  // would otherwise re-issue a /statuses request immediately on every
  // stream lifecycle event.
  const skipDeviceKeysRef = useRef<ReadonlySet<string> | undefined>(skipDeviceKeys);
  useEffect(() => {
    skipDeviceKeysRef.current = skipDeviceKeys;
  }, [skipDeviceKeys]);

  const devicesRef = useRef<Device[]>(devices);
  useEffect(() => {
    devicesRef.current = devices;
  }, [devices]);

  // Stable poller bound to the latest setter via ref so we can call it
  // both from the periodic interval and from a one-shot effect that
  // fires when the device-key set changes (e.g. devices arrive after the
  // initial render).
  const pollerRef = useRef<() => void>(() => {});

  useEffect(() => {
    let cancelled = false;

    const pollStatuses = () => {
      api.listStatuses()
        .then((statuses) => {
          if (cancelled || !statuses || typeof statuses !== 'object') {
            return;
          }
          const skip = skipDeviceKeysRef.current;
          const currentDevices = devicesRef.current;
          const entries: Array<{
            deviceKey: string;
            updater: (prev: { params: Record<string, unknown>; status?: DeviceStatus | null }) =>
              | { params: Record<string, unknown>; status?: DeviceStatus | null }
              | undefined;
          }> = [];
          for (const device of currentDevices) {
            if (skip?.has(device.key)) {
              continue;
            }
            const status = statuses[device.key];
            if (!isDeviceStatus(status)) {
              continue;
            }
            entries.push({
              deviceKey: device.key,
              updater: (prev) => {
                if (sameDeviceStatus(prev.status, status)) return undefined;
                return { ...prev, status };
              },
            });
          }
          if (entries.length > 0) {
            deviceStatesStore.batchUpdate(entries);
          }
        })
        .catch(() => null);
    };

    pollerRef.current = pollStatuses;
    pollStatuses();
    const interval = setInterval(() => {
      pollStatuses();
    }, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(interval);
      pollerRef.current = () => {};
    };
    // Only restart the polling loop when the cadence changes. Device list
    // and skip-set updates are picked up via refs above and do not need
    // to recreate the interval.
  }, [intervalMs]);

  // Fire an immediate poll whenever the set of device keys changes so the
  // first /statuses round trip after devices appear doesn't wait for the
  // next interval tick.
  const deviceKeysSignature = useMemo(
    () =>
      devices
        .map((device) => device.key)
        .sort()
        .join('\u0001'),
    [devices]
  );
  useEffect(() => {
    if (!deviceKeysSignature) return;
    pollerRef.current();
  }, [deviceKeysSignature]);
};
