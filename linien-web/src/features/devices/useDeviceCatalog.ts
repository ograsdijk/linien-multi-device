import { useCallback, useEffect, useMemo, useState } from 'react';
import type { DragEvent } from 'react';
import { api } from '../../api';
import type { Device, DeviceGroup } from '../../types';

export const OVERVIEW_KEY = '__overview__';

export const useDeviceCatalog = () => {
  const [devices, setDevices] = useState<Device[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [activeTabKey, setActiveTabKey] = useState<string | null>(OVERVIEW_KEY);
  const [groupModalOpen, setGroupModalOpen] = useState(false);
  const [groupNameDraft, setGroupNameDraft] = useState('');
  const [editingGroupKey, setEditingGroupKey] = useState<string | null>(null);
  const [dragOverGroupKey, setDragOverGroupKey] = useState<string | null>(null);

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

  const openDeviceGroup = (deviceKey: string) => {
    const match = groups.find((group) => group.device_keys.includes(deviceKey));
    if (match) {
      setActiveTabKey(match.key);
    }
  };

  const handleDrop = async (group: DeviceGroup, event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOverGroupKey(null);
    const deviceKey = event.dataTransfer.getData('text/linien-device-key');
    if (!deviceKey) return;
    await addDeviceToGroup(group, deviceKey);
  };

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
    dragOverGroupKey,
    setDragOverGroupKey,
    openCreateGroup,
    openRenameGroup,
    saveGroup,
    addDeviceToGroup,
    removeDeviceFromGroup,
    openDeviceGroup,
    handleDrop,
    groupDevicesMap,
  };
};
