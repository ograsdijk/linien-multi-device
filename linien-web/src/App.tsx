import { useCallback, useEffect, useMemo, useState } from 'react';
import type { DragEvent } from 'react';
import {
  ActionIcon,
  AppShell,
  Button,
  Group,
  Modal,
  SegmentedControl,
  Select,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core';
import { IconMoonStars, IconPencil, IconPlus, IconSun, IconX } from '@tabler/icons-react';
import { useMantineColorScheme } from '@mantine/core';
import { api } from './api';
import type { Device, DeviceGroup, DeviceStatus, StreamMessage } from './types';
import { DeviceList } from './components/DeviceList';
import { DeviceWorkspace, DeviceState } from './components/DeviceWorkspace';
import { DeviceOverviewCard } from './components/DeviceOverviewCard';

const OVERVIEW_KEY = '__overview__';

export function App() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [activeTabKey, setActiveTabKey] = useState<string | null>(OVERVIEW_KEY);
  const [deviceStates, setDeviceStates] = useState<Record<string, DeviceState>>({});
  const [groupModalOpen, setGroupModalOpen] = useState(false);
  const [groupNameDraft, setGroupNameDraft] = useState('');
  const [editingGroupKey, setEditingGroupKey] = useState<string | null>(null);
  const [dragOverGroupKey, setDragOverGroupKey] = useState<string | null>(null);
  const [overviewFps, setOverviewFps] = useState<number>(0);
  const { colorScheme, setColorScheme } = useMantineColorScheme();

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

  useEffect(() => {
    const interval = setInterval(() => {
      devices.forEach((device) => {
        api.getStatus(device.key)
          .then((status) => {
            setDeviceStates((prev) => {
              const next = { ...prev };
              const current = next[device.key] || { params: {} };
              next[device.key] = { ...current, status };
              return next;
            });
          })
          .catch(() => null);
      });
    }, 5000);
    return () => clearInterval(interval);
  }, [devices]);

  const updateState = useCallback((deviceKey: string, message: StreamMessage) => {
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
      if (message.type === 'plot_frame') {
        return {
          ...prev,
          [deviceKey]: {
            ...current,
            plotFrame: message,
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
  }, []);

  const deviceStatusMap = useMemo(() => {
    const map: Record<string, DeviceStatus | undefined> = {};
    devices.forEach((device) => {
      map[device.key] = deviceStates[device.key]?.status;
    });
    return map;
  }, [devices, deviceStates]);

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
      setActiveGroupKey(created.key);
    }
    setGroupModalOpen(false);
  };

  const addDeviceToGroup = async (group: DeviceGroup, deviceKey: string) => {
    if (group.device_keys.includes(deviceKey)) return;
    const nextKeys = [...group.device_keys, deviceKey];
    const updated = await api.updateGroup(group.key, { device_keys: nextKeys });
    setGroups((prev) => prev.map((item) => (item.key === updated.key ? updated : item)));
  };

  const openDeviceGroup = (deviceKey: string) => {
    const match = groups.find((group) => group.device_keys.includes(deviceKey));
    if (match) {
      setActiveTabKey(match.key);
    }
  };

  const removeDeviceFromGroup = async (group: DeviceGroup, deviceKey: string) => {
    if (!group.device_keys.includes(deviceKey)) return;
    const nextKeys = group.device_keys.filter((key) => key !== deviceKey);
    const updated = await api.updateGroup(group.key, { device_keys: nextKeys });
    setGroups((prev) => prev.map((item) => (item.key === updated.key ? updated : item)));
  };

  const handleDrop = async (group: DeviceGroup, event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOverGroupKey(null);
    const deviceKey = event.dataTransfer.getData('text/linien-device-key');
    if (!deviceKey) return;
    await addDeviceToGroup(group, deviceKey);
  };

  return (
    <AppShell padding="md" navbar={{ width: 320, breakpoint: 'sm' }} header={{ height: 60 }}>
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Text fw={700}>Linien Multi-Device</Text>
          <SegmentedControl
            size="xs"
            value={colorScheme}
            onChange={(value) => setColorScheme(value as 'light' | 'dark' | 'auto')}
            data={[
              { value: 'light', label: <IconSun size={14} /> },
              { value: 'dark', label: <IconMoonStars size={14} /> },
              { value: 'auto', label: 'Auto' },
            ]}
          />
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="md">
        <DeviceList
          devices={devices}
          statuses={deviceStatusMap}
          activeKeys={activeDeviceKeys}
          canAddToGroup={!isOverview && Boolean(activeGroup)}
          onAddToGroup={(key) => {
            if (!activeGroup) return;
            addDeviceToGroup(activeGroup, key).catch(() => null);
          }}
          onAdd={async (payload) => {
            await api.createDevice(payload);
            await loadDevices();
            await loadGroups();
          }}
          onEdit={async (key, payload) => {
            await api.updateDevice(key, payload);
            await loadDevices();
            await loadGroups();
          }}
          onDelete={async (key) => {
            await api.deleteDevice(key);
            await loadDevices();
            await loadGroups();
          }}
          onStartServer={async (key) => {
            await api.startServer(key);
          }}
          onConnect={async (key) => {
            await api.connectDevice(key);
          }}
          onDisconnect={async (key) => {
            await api.disconnectDevice(key);
          }}
        />
      </AppShell.Navbar>
      <AppShell.Main>
        <Tabs value={activeTabKey ?? OVERVIEW_KEY} onChange={(value) => setActiveTabKey(value)}>
          <Group justify="space-between" align="center" mb="sm">
            <Tabs.List>
              <Tabs.Tab value={OVERVIEW_KEY}>
                <Text size="sm">Overview</Text>
              </Tabs.Tab>
              {groups.map((group) => (
                <Tabs.Tab key={group.key} value={group.key}>
                  <Group gap={6} wrap="nowrap">
                    <Text size="sm">{group.name}</Text>
                    <ActionIcon
                      size="xs"
                      variant="subtle"
                      onClick={(event) => {
                        event.stopPropagation();
                        openRenameGroup(group);
                      }}
                    >
                      <IconPencil size={12} />
                    </ActionIcon>
                  </Group>
                </Tabs.Tab>
              ))}
            </Tabs.List>
            <Group gap="xs">
              {isOverview ? (
                <Select
                  size="xs"
                  w={110}
                  value={String(overviewFps)}
                  onChange={(value) => {
                    if (value == null) return;
                    setOverviewFps(Number(value));
                  }}
                  data={[
                    { value: '0', label: 'Full' },
                    { value: '10', label: '10 Hz' },
                    { value: '5', label: '5 Hz' },
                    { value: '2', label: '2 Hz' },
                  ]}
                />
              ) : null}
              <Button size="xs" variant="light" leftSection={<IconPlus size={14} />} onClick={openCreateGroup}>
                New group
              </Button>
            </Group>
          </Group>
          <Tabs.Panel value={OVERVIEW_KEY}>
            <div className="group-grid overview-grid">
              {devices.length === 0 ? (
                <div className="empty-group">No devices configured.</div>
              ) : null}
              {devices.map((device) => (
                <div key={device.key} className="device-card">
                  <DeviceOverviewCard
                    device={device}
                    state={deviceStates[device.key] || { params: {} }}
                    active={activeTabKey === OVERVIEW_KEY}
                    onStateUpdate={updateState}
                    onOpenInGroup={() => openDeviceGroup(device.key)}
                    maxFps={overviewFps || undefined}
                  />
                </div>
              ))}
            </div>
          </Tabs.Panel>
          {groups.map((group) => {
            const groupDevices = group.device_keys
              .map((key) => devices.find((device) => device.key === key))
              .filter((device): device is Device => Boolean(device));
            const dropActive = dragOverGroupKey === group.key;
            return (
              <Tabs.Panel key={group.key} value={group.key}>
                <div
                  className={dropActive ? 'group-grid drop-active' : 'group-grid'}
                  onDragOver={(event) => {
                    event.preventDefault();
                    setDragOverGroupKey(group.key);
                  }}
                  onDragLeave={() => {
                    if (dragOverGroupKey === group.key) {
                      setDragOverGroupKey(null);
                    }
                  }}
                  onDrop={(event) => {
                    handleDrop(group, event).catch(() => null);
                  }}
                >
                  {groupDevices.length === 0 ? (
                    <div className="empty-group">Drop devices here to build this view.</div>
                  ) : null}
                  {groupDevices.map((device) => (
                    <div key={device.key} className="device-card">
                      <Group justify="space-between" align="center" mb="xs">
                        <div>
                          <Text fw={600}>{device.name || 'Unnamed device'}</Text>
                          <Text size="xs" c="dimmed">
                            {device.host}:{device.port}
                          </Text>
                        </div>
                        <Group gap="xs">
                          <ActionIcon
                            size="sm"
                            variant="subtle"
                            color="red"
                            onClick={() => removeDeviceFromGroup(group, device.key)}
                            title="Remove from group"
                          >
                            <IconX size={14} />
                          </ActionIcon>
                        </Group>
                      </Group>
                      <DeviceWorkspace
                        device={device}
                        state={deviceStates[device.key] || { params: {} }}
                        active={group.key === activeTabKey}
                        onStateUpdate={updateState}
                      />
                    </div>
                  ))}
                </div>
              </Tabs.Panel>
            );
          })}
        </Tabs>
      </AppShell.Main>

      <Modal
        opened={groupModalOpen}
        onClose={() => setGroupModalOpen(false)}
        title={editingGroupKey ? 'Rename group' : 'New group'}
      >
        <TextInput
          label="Group name"
          value={groupNameDraft}
          onChange={(event) => setGroupNameDraft(event.currentTarget.value)}
        />
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={() => setGroupModalOpen(false)}>
            Cancel
          </Button>
          <Button color="orange" onClick={saveGroup}>
            Save
          </Button>
        </Group>
      </Modal>
    </AppShell>
  );
}
