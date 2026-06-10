import { useRef, useSyncExternalStore } from 'react';
import type { DeviceStatus, PlotFrame } from '../types';

export type DeviceStateEntry = {
  params: Record<string, unknown>;
  plotFrame?: PlotFrame | null;
  status?: DeviceStatus | null;
};

export type DeviceStateUpdate = {
  deviceKey: string;
  prev: DeviceStateEntry | undefined;
  next: DeviceStateEntry;
};

type Listener = () => void;

const EMPTY_ENTRY: DeviceStateEntry = { params: {} };

// Source-of-truth external store for per-device runtime state (params,
// status, last plot frame). The previous design used a single React
// `useState<Record<string, DeviceStateEntry>>` at the App root, which
// forced the entire App component tree to re-render on every per-device
// flush. With dozens of param updates per second across 12 devices that
// was the dominant cost.
//
// This store routes updates through per-device listeners so each device
// card only re-renders when *its* slice changes. A separate aggregate
// channel is consumed by the lock-summary bookkeeper to maintain its
// aggregates incrementally.
class DeviceStatesStore {
  // Mutated in place — there is no root-level snapshot subscriber, so a
  // full `{ ...this.states }` clone on every change just to feed a
  // never-read snapshot reference is wasteful (it scales with device
  // count and dominates the plot-frame hot path at 12×30 fps).
  private states: Record<string, DeviceStateEntry> = {};
  private deviceListeners = new Map<string, Set<Listener>>();
  private aggregateListeners = new Set<(update: DeviceStateUpdate) => void>();

  getDeviceSnapshot = (deviceKey: string): DeviceStateEntry => {
    return this.states[deviceKey] ?? EMPTY_ENTRY;
  };

  subscribeDevice = (deviceKey: string, listener: Listener): (() => void) => {
    let set = this.deviceListeners.get(deviceKey);
    if (!set) {
      set = new Set();
      this.deviceListeners.set(deviceKey, set);
    }
    set.add(listener);
    return () => {
      const current = this.deviceListeners.get(deviceKey);
      if (!current) return;
      current.delete(listener);
      if (current.size === 0) {
        this.deviceListeners.delete(deviceKey);
      }
    };
  };

  // Per-update fine-grained subscription used by aggregators
  // (e.g. lock summary) that need to know exactly which device changed
  // so they can update O(1) bookkeeping instead of rebuilding maps.
  subscribeAggregate = (listener: (update: DeviceStateUpdate) => void): (() => void) => {
    this.aggregateListeners.add(listener);
    return () => {
      this.aggregateListeners.delete(listener);
    };
  };

  // Apply a transformer that returns either a new entry for `deviceKey`
  // or `undefined` to leave the entry unchanged. Notifies relevant
  // listeners exactly once.
  updateDevice = (
    deviceKey: string,
    updater: (prev: DeviceStateEntry) => DeviceStateEntry | undefined
  ): void => {
    const prev = this.states[deviceKey] ?? EMPTY_ENTRY;
    const next = updater(prev);
    if (!next || next === prev) {
      return;
    }
    // Mutate the per-key slot in place rather than cloning the full
    // record. Consumers see new identities on the entry itself (which is
    // what `useSyncExternalStore` compares against).
    this.states[deviceKey] = next;
    this.emitDevice(deviceKey);
    this.emitAggregate({ deviceKey, prev, next });
  };

  // Apply many per-device updates as a single notification batch. Each
  // device's listener fires once, the aggregate listener fires once per
  // changed device.
  batchUpdate = (
    entries: Array<{
      deviceKey: string;
      updater: (prev: DeviceStateEntry) => DeviceStateEntry | undefined;
    }>
  ): void => {
    if (entries.length === 0) return;
    const changed: DeviceStateUpdate[] = [];
    for (const { deviceKey, updater } of entries) {
      const prev = this.states[deviceKey] ?? EMPTY_ENTRY;
      const next = updater(prev);
      if (!next || next === prev) continue;
      this.states[deviceKey] = next;
      changed.push({ deviceKey, prev, next });
    }
    for (const update of changed) {
      this.emitDevice(update.deviceKey);
      this.emitAggregate(update);
    }
  };

  private emitDevice(deviceKey: string): void {
    const listeners = this.deviceListeners.get(deviceKey);
    if (!listeners) return;
    for (const listener of listeners) listener();
  }

  private emitAggregate(update: DeviceStateUpdate): void {
    for (const listener of this.aggregateListeners) listener(update);
  }
}

export const deviceStatesStore = new DeviceStatesStore();

const EMPTY_PARAMS_ENTRY: DeviceStateEntry = EMPTY_ENTRY;

// Hook for components that need exactly one device's state slice.
// Re-renders only when that device's entry changes by reference.
export function useDeviceStateEntry(deviceKey: string): DeviceStateEntry {
  return useSyncExternalStore(
    (listener) => deviceStatesStore.subscribeDevice(deviceKey, listener),
    () => deviceStatesStore.getDeviceSnapshot(deviceKey),
    () => EMPTY_PARAMS_ENTRY
  );
}

// Narrow subscription: re-render only when `selector(entry)` returns
// a value that differs (per `isEqual`, default Object.is) from the
// last call. Used by card components that read only a couple of
// fields off the entry -- without this, every param-batch write
// forces a card re-render because the entry's top-level ref changes
// even though the fields the card reads (lock, status.connected)
// did not.
//
// IMPORTANT: pass selector/isEqual that are stable across renders
// (define them at module scope or wrap in useCallback). The hook
// captures the first-render selector via refs to avoid resubscribing
// per render, so a mid-render selector swap would not take effect
// until the next remount.
export function useDeviceStateSlice<T>(
  deviceKey: string,
  selector: (entry: DeviceStateEntry) => T,
  isEqual: (a: T, b: T) => boolean = Object.is,
): T {
  const selectorRef = useRef(selector);
  const isEqualRef = useRef(isEqual);
  // Refs are read inside the subscribe + getSnapshot callbacks below.
  // We deliberately do NOT re-subscribe when these change; that would
  // tear down the store listener and lose the cached lastValue.
  selectorRef.current = selector;
  isEqualRef.current = isEqual;
  // The cached last selected value is kept in a ref so React's
  // getSnapshot can return a stable reference between calls. Without
  // this, React detects "new value" every render and re-renders even
  // when nothing changed.
  const lastValueRef = useRef<{ has: false } | { has: true; value: T }>({
    has: false,
  });
  return useSyncExternalStore(
    (listener) =>
      deviceStatesStore.subscribeDevice(deviceKey, () => {
        const entry = deviceStatesStore.getDeviceSnapshot(deviceKey);
        const next = selectorRef.current(entry);
        const last = lastValueRef.current;
        if (last.has && isEqualRef.current(last.value, next)) return;
        lastValueRef.current = { has: true, value: next };
        listener();
      }),
    () => {
      const entry = deviceStatesStore.getDeviceSnapshot(deviceKey);
      const next = selectorRef.current(entry);
      const last = lastValueRef.current;
      if (last.has && isEqualRef.current(last.value, next)) {
        return last.value;
      }
      lastValueRef.current = { has: true, value: next };
      return next;
    },
    () => {
      const next = selectorRef.current(EMPTY_PARAMS_ENTRY);
      lastValueRef.current = { has: true, value: next };
      return next;
    },
  );
}
