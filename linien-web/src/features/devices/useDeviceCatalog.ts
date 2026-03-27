import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import type { Device, DeviceGroup } from '../../types';

export const OVERVIEW_KEY = '__overview__';
const DEVICE_ORDER_KEY = 'linien.deviceOrder';

const arraysEqual = (a: string[], b: string[]): boolean =>
  a.length === b.length && a.every((value, index) => value === b[index]);

const readStoredDeviceOrder = (): string[] => {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(DEVICE_ORDER_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === 'string');
  } catch {
    return [];
  }
};

export const useDeviceCatalog = () => {
  const [devices, setDevices] = useState<Device[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [activeTabKey, setActiveTabKey] = useState<string | null>(OVERVIEW_KEY);
  const [groupModalOpen, setGroupModalOpen] = useState(false);
  const [groupNameDraft, setGroupNameDraft] = useState('');
  const [editingGroupKey, setEditingGroupKey] = useState<string | null>(null);
  const [deviceOrder, setDeviceOrder] = useState<string[]>(() => readStoredDeviceOrder());

  const loadDevices = useCallback(async () => {
    const list = await api.listDevices();
    setDevices(list);
  }, []);

  const loadGroups = useCallback(async () => {
    const list = await api.listGroups();
    setGroups(list);
    if (list.length === 0) {
      setActiveTabKey(OVERVIEW_KEY);
      return;
    }
    if (!activeTabKey) {
      setActiveTabKey(OVERVIEW_KEY);
      return;
    }
    if (activeTabKey !== OVERVIEW_KEY && !list.find((group) => group.key === activeTabKey)) {
      setActiveTabKey(OVERVIEW_KEY);
    }
  }, [activeTabKey]);

  useEffect(() => {
    loadDevices();
    loadGroups();
  }, [loadDevices, loadGroups]);

  const normalizeOrderKeys = useCallback((candidate: string[]): string[] => {
    const deviceKeys = devices.map((device) => device.key);
    const valid = new Set(deviceKeys);
    const deduped: string[] = [];
    for (const key of candidate) {
      if (!valid.has(key) || deduped.includes(key)) {
        continue;
      }
      deduped.push(key);
    }
    for (const key of deviceKeys) {
      if (!deduped.includes(key)) {
        deduped.push(key);
      }
    }
    return deduped;
  }, [devices]);

  useEffect(() => {
    setDeviceOrder((prev) => {
      const next = normalizeOrderKeys(prev);
      if (arraysEqual(next, prev)) {
        return prev;
      }
      return next;
    });
  }, [normalizeOrderKeys]);

  useEffect(() => {
    try {
      window.localStorage.setItem(DEVICE_ORDER_KEY, JSON.stringify(deviceOrder));
    } catch {
      // Ignore persistence failures; ordering still works for current session.
    }
  }, [deviceOrder]);

  const isOverview = activeTabKey === OVERVIEW_KEY;
  const activeGroup = !isOverview
    ? groups.find((group) => group.key === activeTabKey) || null
    : null;
  const activeDeviceKeys = activeGroup?.device_keys ?? [];

  const openCreateGroup = () => {
    setEditingGroupKey(null);
    setGroupNameDraft('');
    setGroupModalOpen(true);
  };

  const openRenameGroup = (group: DeviceGroup) => {
    setEditingGroupKey(group.key);
    setGroupNameDraft(group.name);
    setGroupModalOpen(true);
  };

  const saveGroup = async () => {
    const trimmed = groupNameDraft.trim();
    if (!trimmed) return;
    if (editingGroupKey) {
      const updated = await api.updateGroup(editingGroupKey, { name: trimmed });
      setGroups((prev) => prev.map((group) => (group.key === updated.key ? updated : group)));
    } else {
      const created = await api.createGroup({ name: trimmed, device_keys: [] });
      setGroups((prev) => [...prev, created]);
      setActiveTabKey(created.key);
    }
    setGroupModalOpen(false);
  };

  const addDeviceToGroup = async (group: DeviceGroup, deviceKey: string) => {
    if (group.device_keys.includes(deviceKey)) return;
    const nextKeys = [...group.device_keys, deviceKey];
    const updated = await api.updateGroup(group.key, { device_keys: nextKeys });
    setGroups((prev) => prev.map((item) => (item.key === updated.key ? updated : item)));
  };

  const removeDeviceFromGroup = async (group: DeviceGroup, deviceKey: string) => {
    if (!group.device_keys.includes(deviceKey)) return;
    const nextKeys = group.device_keys.filter((key) => key !== deviceKey);
    const updated = await api.updateGroup(group.key, { device_keys: nextKeys });
    setGroups((prev) => prev.map((item) => (item.key === updated.key ? updated : item)));
  };

  const openDeviceGroup = useCallback((deviceKey: string) => {
    const match = groups.find((group) => group.device_keys.includes(deviceKey));
    if (match) {
      setActiveTabKey(match.key);
    }
  }, [groups]);

  const reorderDevices = useCallback((activeKey: string, overKey: string) => {
    if (activeKey === overKey) return;
    setDeviceOrder((prev) => {
      const oldIndex = prev.indexOf(activeKey);
      const newIndex = prev.indexOf(overKey);
      if (oldIndex < 0 || newIndex < 0 || oldIndex === newIndex) {
        return prev;
      }
      const next = [...prev];
      const [moved] = next.splice(oldIndex, 1);
      next.splice(newIndex, 0, moved);
      return next;
    });
  }, []);

  const setDeviceOrderKeys = useCallback((nextOrderKeys: string[]) => {
    setDeviceOrder((prev) => {
      const normalized = normalizeOrderKeys(nextOrderKeys);
      return arraysEqual(normalized, prev) ? prev : normalized;
    });
  }, [normalizeOrderKeys]);

  const orderedDevices = useMemo(() => {
    const byKey = new Map(devices.map((device) => [device.key, device]));
    const ordered: Device[] = [];
    for (const key of deviceOrder) {
      const device = byKey.get(key);
      if (device) {
        ordered.push(device);
        byKey.delete(key);
      }
    }
    for (const device of devices) {
      if (byKey.has(device.key)) {
        ordered.push(device);
        byKey.delete(device.key);
      }
    }
    return ordered;
  }, [deviceOrder, devices]);

  const groupDevicesMap = useMemo(() => {
    const byKey = new Map(devices.map((device) => [device.key, device]));
    return new Map(
      groups.map((group) => [
        group.key,
        group.device_keys
          .map((key) => byKey.get(key))
          .filter((device): device is Device => Boolean(device)),
      ])
    );
  }, [devices, groups]);

  return {
    devices,
    orderedDevices,
    groups,
    activeTabKey,
    setActiveTabKey,
    isOverview,
    activeGroup,
    activeDeviceKeys,
    loadDevices,
    loadGroups,
    groupModalOpen,
    setGroupModalOpen,
    groupNameDraft,
    setGroupNameDraft,
    editingGroupKey,
    openCreateGroup,
    openRenameGroup,
    saveGroup,
    addDeviceToGroup,
    removeDeviceFromGroup,
    openDeviceGroup,
    reorderDevices,
    setDeviceOrderKeys,
    groupDevicesMap,
  };
};
