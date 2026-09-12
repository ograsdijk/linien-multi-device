import { useMemo, useState, type CSSProperties } from 'react';
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  ActionIcon,
  Button,
  Card,
  Group,
  Menu,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import {
  IconChevronLeft,
  IconDevices,
  IconPencil,
  IconTemperature,
  IconTrash,
} from '@tabler/icons-react';
import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
} from '../types';
import { toDeviceListDragId } from '../features/devices/dragIds';
import { resolveConnectionDisplay } from '../features/connection/connectionState';
import { resolveLockDisplay, resolveRelockTag } from '../features/locks/lockState';
import { RpTemperatureLine } from './RpTemperatureLine';

// Operator actions on the Red Pitaya telemetry service. `install` also enables
// the unit at boot, starts it, and verifies the TCP protocol answers.
export type TelemetryCommand = 'install' | 'start' | 'stop' | 'restart' | 'uninstall';

const emptyForm = {
  name: '',
  host: '',
  port: '18862',
  username: 'root',
  password: 'root',
};

export type DeviceSortMode = 'manual' | 'name' | 'host' | 'connected' | 'lock';

const DEVICE_SORT_OPTIONS: { value: DeviceSortMode; label: string }[] = [
  { value: 'manual', label: 'Manual' },
  { value: 'name', label: 'Name' },
  { value: 'host', label: 'Host/IP' },
  { value: 'connected', label: 'Connected' },
  { value: 'lock', label: 'Lock state' },
];

type DeviceListProps = {
  devices: Device[];
  statuses: Record<string, DeviceStatus | undefined>;
  lockIndicators: Record<string, LockIndicatorSnapshot | undefined>;
  autoRelockStates: Record<string, AutoRelockStatus | undefined>;
  autoRelockBusyKeys?: Record<string, boolean>;
  activeKeys: string[];
  canAddToGroup: boolean;
  sortMode: DeviceSortMode;
  onSortModeChange: (mode: DeviceSortMode) => void;
  onCollapse: () => void;
  onAddToGroup: (key: string) => void;
  onToggleAutoRelock: (key: string, enabled: boolean) => void;
  onAdd: (payload: Partial<Device>) => Promise<void>;
  onEdit: (key: string, payload: Partial<Device>) => Promise<void>;
  onDelete: (key: string) => Promise<void>;
  onStartServer: (key: string) => Promise<void>;
  onConnect: (key: string) => Promise<void>;
  onDisconnect: (key: string) => Promise<void>;
  onShutdownServer: (key: string) => Promise<void>;
  onRebootDevice: (key: string) => Promise<void>;
  onTelemetryCommand: (key: string, command: TelemetryCommand) => Promise<void>;
  onInstallTelemetryAll: (keys: string[]) => Promise<void>;
  onStartTelemetryAll: (keys: string[]) => Promise<void>;
  onRequestDiagnostics: (device: Device) => void;
  telemetryBusyKeys?: Record<string, boolean>;
};

type SortableDeviceCardProps = {
  device: Device;
  status?: DeviceStatus;
  indicator?: LockIndicatorSnapshot;
  autoRelock?: AutoRelockStatus;
  autoRelockBusy: boolean;
  inActiveGroup: boolean;
  canAddToGroup: boolean;
  sortable: boolean;
  onEdit: (device: Device) => void;
  onDelete: (key: string) => Promise<void>;
  onAddToGroup: (key: string) => void;
  onToggleAutoRelock: (key: string, enabled: boolean) => void;
  onStartServer: (key: string) => Promise<void>;
  onConnect: (key: string) => Promise<void>;
  onDisconnect: (key: string) => Promise<void>;
  onRequestShutdown: (device: Device) => void;
  onRequestReboot: (device: Device) => void;
  onRequestDiagnostics: (device: Device) => void;
  onTelemetryCommand: (key: string, command: TelemetryCommand) => Promise<void>;
  telemetryBusy: boolean;
};

function SortableDeviceCard({
  device,
  status,
  indicator,
  autoRelock,
  autoRelockBusy,
  inActiveGroup,
  canAddToGroup,
  sortable,
  onEdit,
  onDelete,
  onAddToGroup,
  onToggleAutoRelock,
  onStartServer,
  onConnect,
  onDisconnect,
  onRequestShutdown,
  onRequestReboot,
  onRequestDiagnostics,
  onTelemetryCommand,
  telemetryBusy,
}: SortableDeviceCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: toDeviceListDragId(device.key),
    disabled: !sortable,
    transition: {
      duration: 180,
      easing: 'cubic-bezier(0.2, 0, 0, 1)',
    },
  });
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
  const autoRelockDisplay = resolveRelockTag(autoRelock);
  const connectionDisplay = resolveConnectionDisplay(status);
  const telemetryInstalled = Boolean(status?.rp_telemetry?.installed);
  const recovery = status?.recovery;
  const recoveryActive = Boolean(
    recovery && !['completed', 'failed', 'cancelled'].includes(recovery.phase)
  );
  const recoveryLabel = recoveryActive
    ? recovery?.phase === 'host_online'
      ? 'Board online'
      : recovery?.phase === 'waiting_for_boot'
        ? 'Rebooting'
        : 'Sending reboot'
    : recovery?.phase === 'failed'
      ? `Reboot failed: ${recovery.error ?? 'unknown error'}`
      : null;
  const wrapperStyle: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };
  const cardStyle: CSSProperties = {
    borderColor: inActiveGroup ? 'var(--tag-green-border)' : undefined,
    position: 'relative',
  };

  return (
    <div
      ref={setNodeRef}
      style={wrapperStyle}
      className="device-card-sortable-wrapper"
      data-dragging={isDragging ? 'true' : undefined}
    >
      <Card
        padding="sm"
        radius="md"
        withBorder
        style={cardStyle}
        className="device-card-sortable"
        data-dragging={isDragging ? 'true' : undefined}
        data-sortable={sortable ? 'true' : 'false'}
        {...(sortable ? attributes : {})}
        {...(sortable ? listeners : {})}
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
                onClick={() => onEdit(device)}
                disabled={recoveryActive}
              >
                <IconPencil size={12} />
              </ActionIcon>
            </Group>
            <Text size="xs" c="dimmed">
              {device.host}:{device.port}
            </Text>
            <RpTemperatureLine
              status={status}
              busy={telemetryBusy}
              onAction={(action) => {
                // 'update' is an install of the newer bundled binary.
                void onTelemetryCommand(device.key, action === 'update' ? 'install' : action);
              }}
            />
            {connectionDisplay.show ? (
              <Text size="xs" c="dimmed">{connectionDisplay.tooltip}</Text>
            ) : status?.last_error ? (
              <Text size="xs" c="red">{status.last_error}</Text>
            ) : null}
            {recoveryLabel ? (
              <Text size="xs" c={recovery?.phase === 'failed' ? 'red' : 'orange'}>
                {recoveryLabel}
              </Text>
            ) : null}
          </div>
          <Group gap={6} align="center">
            <div className={`device-tag status-${state}`}>{tagLabel}</div>
            {connectionDisplay.show ? (
              <div
                className={`device-tag diag-${connectionDisplay.color}`}
                title={connectionDisplay.tooltip}
              >
                {connectionDisplay.label}
              </div>
            ) : null}
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
            <Menu shadow="md" position="bottom-end" withinPortal>
              <Menu.Target>
                <ActionIcon
                  size="sm"
                  variant="subtle"
                  color="gray"
                  loading={telemetryBusy}
                  aria-label={`Telemetry actions for ${device.name || 'device'}`}
                  title="Red Pitaya telemetry"
                >
                  <IconTemperature size={14} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>Red Pitaya telemetry</Menu.Label>
                <Menu.Item onClick={() => void onTelemetryCommand(device.key, 'install')}>
                  {telemetryInstalled ? 'Update / reinstall' : 'Install'}
                </Menu.Item>
                <Menu.Item onClick={() => void onTelemetryCommand(device.key, 'start')}>
                  Start
                </Menu.Item>
                <Menu.Item onClick={() => void onTelemetryCommand(device.key, 'stop')}>
                  Stop
                </Menu.Item>
                <Menu.Item onClick={() => void onTelemetryCommand(device.key, 'restart')}>
                  Restart
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item
                  color="red"
                  onClick={() => void onTelemetryCommand(device.key, 'uninstall')}
                >
                  Uninstall
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
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
            onClick={() => {
              void onStartServer(device.key);
            }}
            disabled={Boolean(connected) || Boolean(connecting) || recoveryActive}
          >
            Start server
          </Button>
          {connected ? (
            <>
              <Button
                size="xs"
                color="red"
                variant="light"
                onClick={() => {
                  void onDisconnect(device.key);
                }}
              >
                Disconnect
              </Button>
              <Button
                size="xs"
                color="red"
                variant="subtle"
                onClick={() => onRequestShutdown(device)}
                disabled={!connected || recoveryActive}
              >
                Shutdown
              </Button>
            </>
          ) : (
            <Button
              size="xs"
              color="green"
              variant="light"
              onClick={() => {
                void onConnect(device.key);
              }}
              disabled={Boolean(connecting) || recoveryActive}
            >
              Connect
            </Button>
          )}
          <Button
            size="xs"
            color="red"
            variant="outline"
            onClick={() => onRequestReboot(device)}
            disabled={Boolean(connecting) || recoveryActive}
          >
            {recoveryActive ? 'Rebooting' : 'Reboot board'}
          </Button>
          <Button
            size="xs"
            variant="light"
            color="gray"
            onClick={() => onRequestDiagnostics(device)}
            title="Why did this board reset, or why did linien-server stop?"
          >
            Diagnostics
          </Button>
        </Group>
        <ActionIcon
          size="sm"
          color="red"
          variant="subtle"
          aria-label={`Remove ${device.name || 'device'}`}
          onClick={() => {
            void onDelete(device.key);
          }}
          disabled={recoveryActive}
          style={{ position: 'absolute', right: 8, bottom: 8 }}
        >
          <IconTrash size={14} />
        </ActionIcon>
      </Card>
    </div>
  );
}

export function DeviceList({
  devices,
  statuses,
  lockIndicators,
  autoRelockStates,
  autoRelockBusyKeys,
  activeKeys,
  canAddToGroup,
  sortMode,
  onSortModeChange,
  onCollapse,
  onAddToGroup,
  onToggleAutoRelock,
  onAdd,
  onEdit,
  onDelete,
  onStartServer,
  onConnect,
  onDisconnect,
  onShutdownServer,
  onRebootDevice,
  onTelemetryCommand,
  onInstallTelemetryAll,
  onStartTelemetryAll,
  onRequestDiagnostics,
  telemetryBusyKeys,
}: DeviceListProps) {
  const [opened, setOpened] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [form, setForm] = useState({ ...emptyForm });
  const [shutdownDevice, setShutdownDevice] = useState<Device | null>(null);
  const [rebootDevice, setRebootDevice] = useState<Device | null>(null);
  const [rebootSubmitting, setRebootSubmitting] = useState(false);
  const [rebootError, setRebootError] = useState<string | null>(null);
  const [telemetryAllBusy, setTelemetryAllBusy] = useState(false);
  const [telemetryStartAllBusy, setTelemetryStartAllBusy] = useState(false);
  const [telemetryAllOpen, setTelemetryAllOpen] = useState(false);
  const activeSet = useMemo(() => new Set(activeKeys), [activeKeys]);
  const sortable = sortMode === 'manual';
  const connectableDevices = useMemo(
    () => devices.filter((device) => {
      const status = statuses[device.key];
      const recovery = status?.recovery;
      const recoveryActive = Boolean(
        recovery && !['completed', 'failed', 'cancelled'].includes(recovery.phase)
      );
      return !status?.connected && !status?.connecting && !recoveryActive;
    }),
    [devices, statuses]
  );
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

  const confirmShutdown = async () => {
    if (!shutdownDevice) return;
    const key = shutdownDevice.key;
    setShutdownDevice(null);
    await onShutdownServer(key);
  };

  const confirmReboot = async () => {
    if (!rebootDevice || rebootSubmitting) return;
    const key = rebootDevice.key;
    setRebootSubmitting(true);
    setRebootError(null);
    try {
      await onRebootDevice(key);
      setRebootDevice(null);
    } catch (error) {
      setRebootError(error instanceof Error ? error.message : 'Failed to request reboot');
    } finally {
      setRebootSubmitting(false);
    }
  };

  const closeRebootModal = () => {
    if (rebootSubmitting) return;
    setRebootDevice(null);
    setRebootError(null);
  };

  const connectAll = async () => {
    await Promise.allSettled(connectableDevices.map((device) => onConnect(device.key)));
  };

  return (
    <Stack gap="sm" className="device-list-shell">
      <Group justify="space-between" align="center">
        <Group gap="xs" align="center">
          <Text fw={600}>Devices</Text>
          <Button size="xs" color="orange" variant="light" onClick={openCreate}>
            Add
          </Button>
          <Button
            size="xs"
            color="green"
            variant="light"
            onClick={() => {
              connectAll().catch(() => null);
            }}
            disabled={connectableDevices.length === 0}
          >
            Connect all
          </Button>
          <Button
            size="xs"
            color="gray"
            variant="light"
            loading={telemetryAllBusy}
            disabled={devices.length === 0}
            title="Install or update the Red Pitaya telemetry service on every device"
            onClick={() => setTelemetryAllOpen(true)}
          >
            Telemetry: install all
          </Button>
          {/* No confirmation, unlike install-all: starting an already-running
              service is a no-op and cannot damage a board, whereas install
              rewrites the binary on every device. */}
          <Button
            size="xs"
            color="gray"
            variant="light"
            loading={telemetryStartAllBusy}
            disabled={devices.length === 0}
            title="Start the Red Pitaya telemetry service on every device"
            onClick={() => {
              setTelemetryStartAllBusy(true);
              onStartTelemetryAll(devices.map((device) => device.key))
                .catch(() => null)
                .finally(() => setTelemetryStartAllBusy(false));
            }}
          >
            Telemetry: start all
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
      <Select
        size="xs"
        label="Sort"
        value={sortMode}
        data={DEVICE_SORT_OPTIONS}
        onChange={(value) => {
          if (value) onSortModeChange(value as DeviceSortMode);
        }}
      />
      <Stack gap="xs" className="device-list-scroll">
        <SortableContext
          items={devices.map((device) => toDeviceListDragId(device.key))}
          strategy={verticalListSortingStrategy}
        >
          {devices.map((device) => (
            <SortableDeviceCard
              key={device.key}
              device={device}
              status={statuses[device.key]}
              indicator={lockIndicators[device.key]}
              autoRelock={autoRelockStates[device.key] ?? statuses[device.key]?.auto_relock ?? undefined}
              autoRelockBusy={Boolean(autoRelockBusyKeys?.[device.key])}
              inActiveGroup={activeSet.has(device.key)}
              canAddToGroup={canAddToGroup}
              sortable={sortable}
              onEdit={openEdit}
              onDelete={onDelete}
              onAddToGroup={onAddToGroup}
              onToggleAutoRelock={onToggleAutoRelock}
              onStartServer={onStartServer}
              onConnect={onConnect}
              onDisconnect={onDisconnect}
              onRequestShutdown={setShutdownDevice}
              onRequestReboot={setRebootDevice}
              onRequestDiagnostics={onRequestDiagnostics}
              onTelemetryCommand={onTelemetryCommand}
              telemetryBusy={Boolean(telemetryBusyKeys?.[device.key])}
            />
          ))}
        </SortableContext>
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
      <Modal
        opened={telemetryAllOpen}
        onClose={() => setTelemetryAllOpen(false)}
        title="Install telemetry on all devices?"
        centered
      >
        <Stack>
          <Text size="sm">
            This uploads the telemetry binary over SSH and restarts the telemetry
            service on all <strong>{devices.length}</strong> device(s), including any
            that are already running it. It does not affect the Linien server or any
            lock currently held.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setTelemetryAllOpen(false)}>
              Cancel
            </Button>
            <Button
              color="orange"
              onClick={() => {
                setTelemetryAllOpen(false);
                setTelemetryAllBusy(true);
                onInstallTelemetryAll(devices.map((device) => device.key))
                  .catch(() => null)
                  .finally(() => setTelemetryAllBusy(false));
              }}
            >
              Install on all
            </Button>
          </Group>
        </Stack>
      </Modal>
      <Modal
        opened={rebootDevice !== null}
        onClose={closeRebootModal}
        title="Reboot Red Pitaya?"
        centered
      >
        <Stack>
          <Text size="sm">
            This will reboot <strong>{rebootDevice?.name || rebootDevice?.key || 'this device'}</strong> and
            destroy any lock currently held by the FPGA. Linien server will not be started automatically.
          </Text>
          {rebootDevice && ['locked', 'likely_held'].includes(
            statuses[rebootDevice.key]?.diagnosis?.lock_state ?? ''
          ) ? (
            <Text size="sm" c="red" fw={700}>
              The FPGA may still be holding the lock. Rebooting will lose it.
            </Text>
          ) : null}
          {rebootError ? <Text size="sm" c="red">{rebootError}</Text> : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={closeRebootModal} disabled={rebootSubmitting}>
              Cancel
            </Button>
            <Button
              color="red"
              onClick={() => {
                void confirmReboot();
              }}
              loading={rebootSubmitting}
            >
              Reboot board
            </Button>
          </Group>
        </Stack>
      </Modal>
      <Modal
        opened={shutdownDevice !== null}
        onClose={() => setShutdownDevice(null)}
        title="Shutdown server?"
        centered
      >
        <Stack>
          <Text size="sm">
            This will shut down the server for{' '}
            <strong>{shutdownDevice?.name || shutdownDevice?.key || 'this device'}</strong>.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setShutdownDevice(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              onClick={() => {
                confirmShutdown().catch(() => null);
              }}
            >
              Shutdown server
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
