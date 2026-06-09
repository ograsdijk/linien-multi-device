import { useEffect, useState } from 'react';
import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
} from '../../types';
import { computeLockHealthSummary, resolveLockDisplay } from './lockState';
import {
  deviceStatesStore,
  type DeviceStateEntry,
} from '../../state/deviceStatesStore';

export type LockSummaryMaps = {
  deviceStatusMap: Record<string, DeviceStatus | undefined>;
  lockIndicatorMap: Record<string, LockIndicatorSnapshot | undefined>;
  autoRelockMap: Record<string, AutoRelockStatus | undefined>;
  lockStateMap: Record<string, boolean | undefined>;
  effectiveLockStateMap: Record<string, boolean | undefined>;
  lockHealthSummary: ReturnType<typeof computeLockHealthSummary>;
  connectedDeviceCount: number;
  lockedDeviceCount: number;
  connectedRelockEnabledCount: number;
};

const deriveEntry = (entry: DeviceStateEntry | undefined) => {
  const status = entry?.status ?? undefined;
  const indicator = entry?.plotFrame?.lock_indicator ?? undefined;
  const autoRelock =
    entry?.plotFrame?.auto_relock ?? entry?.status?.auto_relock ?? undefined;
  const fromPlot = typeof entry?.plotFrame?.lock === 'boolean' ? entry.plotFrame.lock : undefined;
  const fromStatus = typeof entry?.status?.lock === 'boolean' ? entry.status.lock : undefined;
  const lockState = fromPlot ?? fromStatus;
  const display = resolveLockDisplay({
    connected: Boolean(status?.connected),
    lockEnabled: lockState,
    indicator,
  });
  return { status, indicator, autoRelock, lockState, effectiveLocked: display.effectiveLocked };
};

// Incremental lock summary store. Replaces the previous design where every
// `useLockSummary` consumer re-ran 5 full-map `useMemo` rebuilds plus a
// summary computation on every device state change. Now we maintain
// in-place maps and aggregate counters, defer snapshot rebuilds to a
// microtask so bursts of updates only produce one notification, and only
// stay subscribed to the device-states store while at least one consumer
// is mounted.
class LockSummaryBookkeeper {
  // Maps are mutated in place. The published snapshot wraps them via
  // shallow copies so React consumers see new identities on change.
  private statusByKey: Record<string, DeviceStatus | undefined> = {};
  private indicatorByKey: Record<string, LockIndicatorSnapshot | undefined> = {};
  private autoRelockByKey: Record<string, AutoRelockStatus | undefined> = {};
  private lockByKey: Record<string, boolean | undefined> = {};
  private effectiveLockByKey: Record<string, boolean | undefined> = {};
  private flags = new Map<
    string,
    { connected: boolean; effectiveLocked: boolean; relockEnabled: boolean }
  >();
  private connectedCount = 0;
  private lockedCount = 0;
  private relockEnabledCount = 0;
  private devices: Device[] = [];
  private deviceKeySignature = '';
  private listeners = new Set<() => void>();
  // Cached snapshot returned to React. Rebuilt only when work is flushed.
  private cachedSnapshot: LockSummaryMaps;
  // Coalescing: aggregate updates set `dirty = true` and schedule a
  // microtask. The microtask flushes once per tick, regardless of how
  // many devices changed.
  private dirty = false;
  private flushScheduled = false;
  // Subscription token from the device-states store. Held only while
  // listener count > 0 so the bookkeeper can stop doing per-update work
  // when nothing is observing the summary.
  private aggregateUnsubscribe: (() => void) | null = null;

  constructor() {
    this.cachedSnapshot = this.buildSnapshot();
  }

  private buildSnapshot(): LockSummaryMaps {
    // Shallow-copy the underlying maps once per flush so React consumers
    // see new identities, while per-update mutations stay cheap.
    const statusCopy = { ...this.statusByKey };
    const indicatorCopy = { ...this.indicatorByKey };
    const autoRelockCopy = { ...this.autoRelockByKey };
    const lockCopy = { ...this.lockByKey };
    const effectiveLockCopy = { ...this.effectiveLockByKey };
    const lockHealthSummary = computeLockHealthSummary({
      devices: this.devices,
      statusByKey: statusCopy,
      lockByKey: lockCopy,
      indicatorByKey: indicatorCopy,
    });
    return {
      deviceStatusMap: statusCopy,
      lockIndicatorMap: indicatorCopy,
      autoRelockMap: autoRelockCopy,
      lockStateMap: lockCopy,
      effectiveLockStateMap: effectiveLockCopy,
      lockHealthSummary,
      connectedDeviceCount: this.connectedCount,
      lockedDeviceCount: this.lockedCount,
      connectedRelockEnabledCount: this.relockEnabledCount,
    };
  }

  private markDirty(): void {
    this.dirty = true;
    if (this.flushScheduled) return;
    // No consumers means no listener will read the snapshot; defer
    // rebuilding it altogether so per-event work stays minimal.
    if (this.listeners.size === 0) return;
    this.flushScheduled = true;
    queueMicrotask(() => {
      this.flushScheduled = false;
      if (!this.dirty) return;
      this.dirty = false;
      this.cachedSnapshot = this.buildSnapshot();
      for (const l of this.listeners) l();
    });
  }

  setDevices = (devices: Device[]): void => {
    // Cheap signature comparison: only rebuild when the device-key set
    // actually changed. Reordering or addition/removal both qualify
    // because `lockHealthSummary` iterates the devices array directly.
    const nextSignature = devices.map((d) => d.key).join('\u0001');
    const signatureChanged = nextSignature !== this.deviceKeySignature;
    this.devices = devices;
    this.deviceKeySignature = nextSignature;
    if (!signatureChanged) return;

    const validKeys = new Set(devices.map((d) => d.key));
    for (const key of Object.keys(this.statusByKey)) {
      if (!validKeys.has(key)) {
        this.removeKey(key);
      }
    }
    this.markDirty();
  };

  applyEntry = (deviceKey: string, entry: DeviceStateEntry | undefined): void => {
    const derived = deriveEntry(entry);
    const prevFlags = this.flags.get(deviceKey);
    const prevConnected = prevFlags?.connected ?? false;
    const prevEffectiveLocked = prevFlags?.effectiveLocked ?? false;
    const prevRelockEnabled = prevFlags?.relockEnabled ?? false;

    const nextStatus = derived.status;
    const nextIndicator = derived.indicator;
    const nextAutoRelock = derived.autoRelock;
    const nextLockState = derived.lockState;
    const nextEffectiveLocked = Boolean(nextStatus?.connected) && derived.effectiveLocked;

    let mapsChanged = false;
    if (this.statusByKey[deviceKey] !== nextStatus) {
      this.statusByKey[deviceKey] = nextStatus;
      mapsChanged = true;
    }
    if (this.indicatorByKey[deviceKey] !== nextIndicator) {
      this.indicatorByKey[deviceKey] = nextIndicator;
      mapsChanged = true;
    }
    if (this.autoRelockByKey[deviceKey] !== nextAutoRelock) {
      this.autoRelockByKey[deviceKey] = nextAutoRelock;
      mapsChanged = true;
    }
    if (this.lockByKey[deviceKey] !== nextLockState) {
      this.lockByKey[deviceKey] = nextLockState;
      mapsChanged = true;
    }
    if (this.effectiveLockByKey[deviceKey] !== nextEffectiveLocked) {
      this.effectiveLockByKey[deviceKey] = nextEffectiveLocked;
      mapsChanged = true;
    }

    const nextConnected = Boolean(nextStatus?.connected);
    const nextRelockEnabled = Boolean(nextAutoRelock?.enabled);
    const nextLockedForCount = nextConnected && nextEffectiveLocked;
    const prevLockedForCount = prevConnected && prevEffectiveLocked;

    if (nextConnected !== prevConnected) {
      this.connectedCount += nextConnected ? 1 : -1;
    }
    if (nextLockedForCount !== prevLockedForCount) {
      this.lockedCount += nextLockedForCount ? 1 : -1;
    }
    const prevRelockCounted = prevConnected && prevRelockEnabled;
    const nextRelockCounted = nextConnected && nextRelockEnabled;
    if (nextRelockCounted !== prevRelockCounted) {
      this.relockEnabledCount += nextRelockCounted ? 1 : -1;
    }
    this.flags.set(deviceKey, {
      connected: nextConnected,
      effectiveLocked: nextEffectiveLocked,
      relockEnabled: nextRelockEnabled,
    });

    if (mapsChanged) {
      this.markDirty();
    }
  };

  private removeKey(deviceKey: string): void {
    const flags = this.flags.get(deviceKey);
    if (flags) {
      if (flags.connected) this.connectedCount -= 1;
      if (flags.connected && flags.effectiveLocked) this.lockedCount -= 1;
      if (flags.connected && flags.relockEnabled) this.relockEnabledCount -= 1;
      this.flags.delete(deviceKey);
    }
    if (deviceKey in this.statusByKey) delete this.statusByKey[deviceKey];
    if (deviceKey in this.indicatorByKey) delete this.indicatorByKey[deviceKey];
    if (deviceKey in this.autoRelockByKey) delete this.autoRelockByKey[deviceKey];
    if (deviceKey in this.lockByKey) delete this.lockByKey[deviceKey];
    if (deviceKey in this.effectiveLockByKey) delete this.effectiveLockByKey[deviceKey];
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      // First consumer mounted — start receiving aggregate updates from
      // the device-states store and reseed with whatever is already in
      // the store. While no consumer is mounted we don't pay this cost.
      this.aggregateUnsubscribe = deviceStatesStore.subscribeAggregate(
        ({ deviceKey, next }) => {
          this.applyEntry(deviceKey, next);
        }
      );
      this.reseedFromStore();
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && this.aggregateUnsubscribe) {
        this.aggregateUnsubscribe();
        this.aggregateUnsubscribe = null;
      }
    };
  };

  // Reseed bookkeeping from the current store snapshot for every device
  // we know about. Called once when the first consumer mounts; defers
  // notification to a single microtask via `markDirty`.
  reseedFromStore = (): void => {
    for (const device of this.devices) {
      const entry = deviceStatesStore.getDeviceSnapshot(device.key);
      this.applyEntry(device.key, entry);
    }
  };

  getSnapshot = (): LockSummaryMaps => this.cachedSnapshot;
}

const lockSummaryStore = new LockSummaryBookkeeper();

export const useLockSummary = (devices: Device[]): LockSummaryMaps => {
  // Keep the bookkeeper informed about which devices exist. `setDevices`
  // is no-op when the key signature hasn't changed.
  useEffect(() => {
    lockSummaryStore.setDevices(devices);
  }, [devices]);

  // When the device list changes, reseed bookkeeping for the new set so
  // brand-new devices pick up any state already present in the store.
  // Runs in an effect (not during render) so it's safe under concurrent
  // rendering / StrictMode double-invoke.
  useEffect(() => {
    lockSummaryStore.reseedFromStore();
  }, [devices]);

  const [snapshot, setSnapshot] = useState<LockSummaryMaps>(() =>
    lockSummaryStore.getSnapshot()
  );
  useEffect(() => {
    return lockSummaryStore.subscribe(() => {
      setSnapshot(lockSummaryStore.getSnapshot());
    });
  }, []);
  return snapshot;
};
