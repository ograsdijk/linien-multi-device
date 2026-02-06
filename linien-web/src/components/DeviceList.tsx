import { useMemo, useState } from 'react';
import { Button, Card, Group, Modal, Stack, Text, TextInput } from '@mantine/core';
import type { Device, DeviceStatus } from '../types';

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
  activeKeys: string[];
  canAddToGroup: boolean;
  onAddToGroup: (key: string) => void;
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
  activeKeys,
  canAddToGroup,
  onAddToGroup,
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
      <Group justify="space-between">
        <Text fw={600}>Devices</Text>
        <Button size="xs" color="orange" variant="light" onClick={openCreate}>
          Add
        </Button>
      </Group>
      <Stack gap="xs">
        {devices.map((device) => {
          const status = statuses[device.key];
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
              }}
            >
              <Group justify="space-between" align="center">
                <div>
                  <Text fw={600}>{device.name || 'Unnamed device'}</Text>
                  <Text size="xs" c="dimmed">
                    {device.host}:{device.port}
                  </Text>
                  {status?.last_error ? (
                    <Text size="xs" c="red">{status.last_error}</Text>
                  ) : null}
                </div>
                <div className={`device-tag status-${state}`}>{tagLabel}</div>
              </Group>
              <Group mt="sm" gap="xs">
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
              <Group mt={6} gap="xs">
                <Button size="xs" variant="default" onClick={() => openEdit(device)}>
                  Edit
                </Button>
                <Button size="xs" color="red" variant="outline" onClick={() => onDelete(device.key)}>
                  Remove
                </Button>
              </Group>
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
