import { useMemo, useState } from 'react';
import { ActionIcon, Button, Card, Group, Modal, Stack, Text, TextInput } from '@mantine/core';
import { IconChevronLeft, IconDevices, IconPencil, IconTrash } from '@tabler/icons-react';
import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
} from '../types';
import { resolveLockDisplay, resolveRelockTag } from '../features/locks/lockState';

const emptyForm = {
  name: '',
  host: '',
  port: '18862',
  username: 'root',
  password: 'root',
};

type DeviceListProps = {
  devices: Device[];
  statuses: Record<string, DeviceStatus | undefined>;
  lockIndicators: Record<string, LockIndicatorSnapshot | undefined>;
  autoRelockStates: Record<string, AutoRelockStatus | undefined>;
  autoRelockBusyKeys?: Record<string, boolean>;
  activeKeys: string[];
  canAddToGroup: boolean;
  onCollapse: () => void;
  onAddToGroup: (key: string) => void;
  onToggleAutoRelock: (key: string, enabled: boolean) => void;
  onAdd: (payload: Partial<Device>) => Promise<void>;
  onEdit: (key: string, payload: Partial<Device>) => Promise<void>;
  onDelete: (key: string) => Promise<void>;
  onStartServer: (key: string) => Promise<void>;
  onConnect: (key: string) => Promise<void>;
  onDisconnect: (key: string) => Promise<void>;
};

export function DeviceList({
  devices,
  statuses,
  lockIndicators,
  autoRelockStates,
  autoRelockBusyKeys,
  activeKeys,
  canAddToGroup,
  onCollapse,
  onAddToGroup,
  onToggleAutoRelock,
  onAdd,
  onEdit,
  onDelete,
  onStartServer,
  onConnect,
  onDisconnect,
}: DeviceListProps) {
  const [opened, setOpened] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [form, setForm] = useState({ ...emptyForm });
  const activeSet = useMemo(() => new Set(activeKeys), [activeKeys]);
  const connectedDeviceCount = useMemo(
    () => devices.reduce((count, device) => count + (statuses[device.key]?.connected ? 1 : 0), 0),
    [devices, statuses]
  );

  const openCreate = () => {
    setEditingKey(null);
    setForm({ ...emptyForm });
    setOpened(true);
  };

  const openEdit = (device: Device) => {
    setEditingKey(device.key);
    setForm({
      name: device.name,
      host: device.host,
      port: String(device.port ?? emptyForm.port),
      username: device.username,
      password: device.password,
    });
    setOpened(true);
  };

  const handleSubmit = async () => {
    const portValue = Number(form.port);
    const payload = {
      ...form,
      port: Number.isFinite(portValue) ? portValue : Number(emptyForm.port),
    };
    if (editingKey) {
      await onEdit(editingKey, payload);
    } else {
      await onAdd(payload);
    }
    setOpened(false);
  };

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="center">
        <Group gap="xs" align="center">
          <Text fw={600}>Devices</Text>
          <Button size="xs" color="orange" variant="light" onClick={openCreate}>
            Add
          </Button>
        </Group>
        <Group gap="xs" align="center">
          <Group gap={4} align="center" title="Connected (total devices)">
            <IconDevices size={16} />
            <Text size="sm" fw={700} c={connectedDeviceCount === 0 ? 'red' : 'green'}>
              {connectedDeviceCount}
            </Text>
            <Text size="sm" c="dimmed">
              ({devices.length})
            </Text>
          </Group>
          <ActionIcon
            size="sm"
            variant="subtle"
            color="gray"
            aria-label="Collapse devices panel"
            title="Collapse devices panel"
            onClick={onCollapse}
          >
            <IconChevronLeft size={14} />
          </ActionIcon>
        </Group>
      </Group>
      <Stack gap="xs">
        {devices.map((device) => {
          const status = statuses[device.key];
          const indicator = lockIndicators[device.key];
          const connected = status?.connected;
          const connecting = status?.connecting;
          const hasError = Boolean(status?.last_error);
          const state = hasError
            ? 'error'
            : connecting
              ? 'connecting'
              : connected
                ? 'connected'
                : 'disconnected';
          const tagLabel =
            state === 'error'
              ? 'Error'
              : state === 'connecting'
                ? 'Connecting'
                : state === 'connected'
                  ? 'Connected'
                  : 'Disconnected';
          const lockDisplay = resolveLockDisplay({
            connected: Boolean(connected),
            lockEnabled: status?.lock,
            indicator: indicator ?? null,
          });
          const autoRelock = autoRelockStates[device.key] ?? status?.auto_relock ?? undefined;
          const autoRelockDisplay = resolveRelockTag(autoRelock);
          const autoRelockBusy = Boolean(autoRelockBusyKeys?.[device.key]);
          const inActiveGroup = activeSet.has(device.key);
          return (
            <Card
              key={device.key}
              padding="sm"
              radius="md"
              withBorder
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData('text/linien-device-key', device.key);
                event.dataTransfer.effectAllowed = 'copy';
              }}
              style={{
                borderColor: inActiveGroup ? 'var(--tag-green-border)' : undefined,
                position: 'relative',
              }}
            >
              <Group justify="space-between" align="center">
                <div>
                  <Group gap={4} align="center" wrap="nowrap">
                    <Text fw={600}>{device.name || 'Unnamed device'}</Text>
                    <ActionIcon
                      size="xs"
                      variant="subtle"
                      color="orange"
                      aria-label={`Edit ${device.name || 'device'}`}
                      onClick={() => openEdit(device)}
                    >
                      <IconPencil size={12} />
                    </ActionIcon>
                  </Group>
                  <Text size="xs" c="dimmed">
                    {device.host}:{device.port}
                  </Text>
                  {status?.last_error ? (
                    <Text size="xs" c="red">{status.last_error}</Text>
                  ) : null}
                </div>
                <Group gap={6} align="center">
                  <div className={`device-tag status-${state}`}>{tagLabel}</div>
                  <div className={`device-tag status-lock-${lockDisplay.uiState}`}>
                    {lockDisplay.label}
                  </div>
                  <button
                    type="button"
                    className={`device-tag device-tag-button status-lock-${autoRelockDisplay.uiState}`}
                    onClick={() => onToggleAutoRelock(device.key, !autoRelockDisplay.enabled)}
                    disabled={autoRelockBusy}
                    style={{
                      cursor: autoRelockBusy ? 'default' : 'pointer',
                      opacity: autoRelockBusy ? 0.6 : 1,
                    }}
                    title="Toggle auto relock"
                  >
                    {autoRelockDisplay.label}
                  </button>
                </Group>
              </Group>
              <Group mt="sm" gap="xs" style={{ paddingRight: 34 }}>
                <Button
                  size="xs"
                  variant="light"
                  onClick={() => onAddToGroup(device.key)}
                  disabled={!canAddToGroup || inActiveGroup}
                >
                  {inActiveGroup ? 'In group' : 'Add to group'}
                </Button>
                <Button
                  size="xs"
                  variant="light"
                  color="blue"
                  onClick={() => onStartServer(device.key)}
                  disabled={connected}
                >
                  Start server
                </Button>
                {connected ? (
                  <Button size="xs" color="red" variant="light" onClick={() => onDisconnect(device.key)}>
                    Disconnect
                  </Button>
                ) : (
                  <Button size="xs" color="green" variant="light" onClick={() => onConnect(device.key)}>
                    Connect
                  </Button>
                )}
              </Group>
              <ActionIcon
                size="sm"
                color="red"
                variant="subtle"
                aria-label={`Remove ${device.name || 'device'}`}
                onClick={() => onDelete(device.key)}
                style={{ position: 'absolute', right: 8, bottom: 8 }}
              >
                <IconTrash size={14} />
              </ActionIcon>
            </Card>
          );
        })}
      </Stack>

      <Modal opened={opened} onClose={() => setOpened(false)} title={editingKey ? 'Edit device' : 'Add device'}>
        <Stack>
          <TextInput
            label="Name"
            value={form.name}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setForm((prev) => ({ ...prev, name: value }));
            }}
          />
          <TextInput
            label="Host"
            value={form.host}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setForm((prev) => ({ ...prev, host: value }));
            }}
          />
          <TextInput
            label="Port"
            value={form.port}
            inputMode="numeric"
            onChange={(event) => {
              const value = event.currentTarget.value;
              setForm((prev) => ({
                ...prev,
                port: value,
              }));
            }}
          />
          <TextInput
            label="Username"
            value={form.username}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setForm((prev) => ({ ...prev, username: value }));
            }}
          />
          <TextInput
            label="Password"
            value={form.password}
            type="password"
            onChange={(event) => {
              const value = event.currentTarget.value;
              setForm((prev) => ({ ...prev, password: value }));
            }}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpened(false)}>
              Cancel
            </Button>
            <Button color="orange" onClick={handleSubmit}>
              Save
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
