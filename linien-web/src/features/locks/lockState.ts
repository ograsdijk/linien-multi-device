import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
} from '../../types';

export type LockUiState = 'unknown' | 'locked' | 'marginal' | 'lost';
export type LockUiColor = 'dimmed' | 'green' | 'orange' | 'red';

export type LockDisplay = {
  uiState: LockUiState;
  label: string;
  color: LockUiColor;
  effectiveLocked: boolean;
};

export type LockHealthSummary = {
  considered: number;
  locked: number;
  lost: number;
  marginalOrUnknown: number;
};

export const isLockIndicatorEnabled = (
  snapshot?: LockIndicatorSnapshot | null
): boolean => {
  const reasons = snapshot?.reasons ?? [];
  return Boolean(snapshot) && !reasons.includes('disabled');
};

export const resolveLockDisplay = (args: {
  connected: boolean;
  lockEnabled?: boolean | null;
  indicator?: LockIndicatorSnapshot | null;
}): LockDisplay => {
  const { connected, lockEnabled, indicator } = args;
  if (!connected) {
    return { uiState: 'unknown', label: 'Lock: --', color: 'dimmed', effectiveLocked: false };
  }
  if (lockEnabled !== true) {
    return { uiState: 'unknown', label: 'Lock: off', color: 'dimmed', effectiveLocked: false };
  }
  const indicatorEnabled = isLockIndicatorEnabled(indicator);
  if (!indicatorEnabled) {
    return { uiState: 'unknown', label: 'Lock: on', color: 'dimmed', effectiveLocked: true };
  }
  const state = indicator?.state ?? 'unknown';
  if (state === 'locked') {
    return { uiState: 'locked', label: 'Lock: locked', color: 'green', effectiveLocked: true };
  }
  if (state === 'marginal') {
    return {
      uiState: 'marginal',
      label: 'Lock: marginal',
      color: 'orange',
      effectiveLocked: false,
    };
  }
  if (state === 'lost') {
    return { uiState: 'lost', label: 'Lock: lost', color: 'red', effectiveLocked: false };
  }
  return { uiState: 'unknown', label: 'Lock: unknown', color: 'dimmed', effectiveLocked: false };
};

export const resolveRelockTag = (
  autoRelock?: AutoRelockStatus | null
): { uiState: LockUiState; label: string; enabled: boolean } => {
  const enabled = Boolean(autoRelock?.enabled);
  const state = autoRelock?.state ?? 'idle';
  if (!enabled) {
    return { uiState: 'unknown', label: 'Relock: off', enabled: false };
  }
  if (state === 'cooldown' || autoRelock?.last_error) {
    return { uiState: 'marginal', label: `Relock: on (${state})`, enabled: true };
  }
  return { uiState: 'locked', label: `Relock: on (${state})`, enabled: true };
};

export const deriveLockStateFromStatus = (status?: DeviceStatus | null): boolean | undefined => {
  if (!status || typeof status.lock !== 'boolean') return undefined;
  return status.lock;
};

export const computeLockHealthSummary = (args: {
  devices: Device[];
  statusByKey: Record<string, DeviceStatus | undefined>;
  lockByKey: Record<string, boolean | undefined>;
  indicatorByKey: Record<string, LockIndicatorSnapshot | undefined>;
}): LockHealthSummary => {
  const { devices, statusByKey, lockByKey, indicatorByKey } = args;
  let considered = 0;
  let locked = 0;
  let lost = 0;
  let marginalOrUnknown = 0;
  devices.forEach((device) => {
    const connected = Boolean(statusByKey[device.key]?.connected);
    if (!connected) return;
    const lockEnabled = lockByKey[device.key] === true;
    if (!lockEnabled) return;
    considered += 1;
    const display = resolveLockDisplay({
      connected,
      lockEnabled,
      indicator: indicatorByKey[device.key],
    });
    if (display.effectiveLocked) {
      locked += 1;
    } else if (display.uiState === 'lost') {
      lost += 1;
    } else {
      marginalOrUnknown += 1;
    }
  });
  return { considered, locked, lost, marginalOrUnknown };
};
