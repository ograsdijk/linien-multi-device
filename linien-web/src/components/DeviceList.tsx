import { useMemo, useRef, useState, type CSSProperties } from 'react';
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useHotkeys } from '@mantine/hooks';
import {
  ActionIcon,
  Button,
  Card,
  Chip,
  Collapse,
  Group,
  Menu,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconChevronDown,
  IconChevronLeft,
  IconChevronRight,
  IconDevices,
  IconLayoutSidebarRightExpand,
  IconLoader2,
  IconLock,
  IconLockOpen,
  IconPencil,
  IconPlugConnected,
  IconPlugConnectedX,
  IconSearch,
  IconTemperature,
  IconTrash,
  IconX,
} from '@tabler/icons-react';
import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
  StreamMessage,
} from '../types';
import { toDeviceListDragId } from '../features/devices/dragIds';
import { resolveConnectionDisplay } from '../features/connection/connectionState';
import { resolveLockDisplay, resolveRelockTag } from '../features/locks/lockState';
import {
  DEVICE_FILTER_TAGS,
  filterDevices,
  resolveFilterAlias,
  type DeviceFilterTag,
} from '../features/devices/deviceFilter';
import { DeviceDetailModal } from './DeviceDetailModal';
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
  onStateUpdate?: (deviceKey: string, message: StreamMessage) => void;
  telemetryBusyKeys?: Record<string, boolean>;
};

type DeviceRowProps = {
  device: Device;
  status?: DeviceStatus;
  indicator?: LockIndicatorSnapshot;
  autoRelock?: AutoRelockStatus;
  autoRelockBusy: boolean;
  inActiveGroup: boolean;
  canAddToGroup: boolean;
  sortable: boolean;
  expanded: boolean;
  onToggleExpanded: (key: string) => void;
  onOpenDetail: (device: Device) => void;
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

// One line per device when collapsed. Everything the old card showed below the
// title is behind the expander: at fleet size the panel was one device tall,
// which made finding a board a scrolling exercise.
function DeviceRow({
  device,
  status,
  indicator,
  autoRelock,
  autoRelockBusy,
  inActiveGroup,
  canAddToGroup,
  sortable,
  expanded,
  onToggleExpanded,
  onOpenDetail,
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
}: DeviceRowProps) {
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
  // dnd-kit's 6px activation constraint lets a click and a drag share the same
  // element, but it does not swallow the click that follows a drop -- without
  // this the row would toggle open every time it was reordered.
  const pressOriginRef = useRef<{ x: number; y: number } | null>(null);
  const handleHeadPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    pressOriginRef.current = { x: event.clientX, y: event.clientY };
  };
  const handleHeadClick = (event: React.MouseEvent<HTMLDivElement>) => {
    const origin = pressOriginRef.current;
    pressOriginRef.current = null;
    if (origin) {
      const moved = Math.hypot(event.clientX - origin.x, event.clientY - origin.y);
      if (moved > 6) return;
    }
    onToggleExpanded(device.key);
  };
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
        padding={6}
        radius="md"
        withBorder
        style={cardStyle}
        className="device-card-sortable device-row"
        data-dragging={isDragging ? 'true' : undefined}
        data-sortable={sortable ? 'true' : 'false'}
        data-expanded={expanded ? 'true' : undefined}
      >
        {/* Drag listeners live on the collapsed header only, so the buttons
            revealed below never compete with the sortable sensor. */}
        <Group
          gap={6}
          wrap="nowrap"
          align="center"
          className="device-row-head"
          onPointerDownCapture={handleHeadPointerDown}
          onClick={handleHeadClick}
          {...(sortable ? attributes : {})}
          {...(sortable ? listeners : {})}
        >
          <ActionIcon
            size="xs"
            variant="subtle"
            color="gray"
            aria-label={expanded ? `Collapse ${device.name || 'device'}` : `Expand ${device.name || 'device'}`}
          >
            {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
          </ActionIcon>
          <div className="device-row-title">
            <Text fw={600} size="sm" truncate>
              {device.name || 'Unnamed device'}
            </Text>
            <Text size="xs" c="dimmed" truncate>
              {device.host}:{device.port}
            </Text>
          </div>
          <span
            className={`device-tag device-tag-glyph status-${state}`}
            title={tagLabel}
            role="img"
            aria-label={tagLabel}
          >
            {state === 'error' ? (
              <IconAlertTriangle size={13} />
            ) : state === 'connecting' ? (
              <IconLoader2 size={13} />
            ) : state === 'connected' ? (
              <IconPlugConnected size={13} />
            ) : (
              <IconPlugConnectedX size={13} />
            )}
          </span>
          <span
            className={`device-tag device-tag-glyph status-lock-${lockDisplay.uiState}`}
            title={lockDisplay.label}
            role="img"
            aria-label={lockDisplay.label}
          >
            {lockDisplay.uiState === 'locked' || lockDisplay.uiState === 'marginal' ? (
              <IconLock size={13} />
            ) : (
              <IconLockOpen size={13} />
            )}
          </span>
          <ActionIcon
            size="sm"
            variant="subtle"
            color="gray"
            aria-label={`Details for ${device.name || 'device'}`}
            title="Device overview"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              onOpenDetail(device);
            }}
          >
            <IconLayoutSidebarRightExpand size={14} />
          </ActionIcon>
        </Group>

        <Collapse in={expanded}>
          <div className="device-row-body">
            <Group justify="space-between" align="flex-start" wrap="nowrap">
              <div style={{ minWidth: 0 }}>
                <Group gap={4} align="center" wrap="nowrap">
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
                  <Text size="xs" c="dimmed">
                    Edit device
                  </Text>
                </Group>
                <RpTemperatureLine
                  status={status}
                  deviceKey={device.key}
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
              <Group gap={6} align="center" wrap="wrap" justify="flex-end">
                <div className={`device-tag status-${state}`}>{tagLabel}</div>
                <div className={`device-tag status-lock-${lockDisplay.uiState}`}>
                  {lockDisplay.label}
                </div>
                {connectionDisplay.show ? (
                  <div
                    className={`device-tag diag-${connectionDisplay.color}`}
                    title={connectionDisplay.tooltip}
                  >
                    {connectionDisplay.label}
                  </div>
                ) : null}
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
            <Group mt="xs" gap="xs" style={{ paddingRight: 34 }}>
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
          </div>
        </Collapse>
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
  onStateUpdate,
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
  const [searchText, setSearchText] = useState('');
  const [filterTags, setFilterTags] = useState<DeviceFilterTag[]>([]);
  // One row open at a time: letting them all expand would restore the very
  // scrolling this panel was collapsed to avoid.
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [detailKey, setDetailKey] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const activeSet = useMemo(() => new Set(activeKeys), [activeKeys]);
  const filterActive = searchText.trim().length > 0 || filterTags.length > 0;
  // Dropping a card into a filtered subset would persist an order derived from
  // rows that are not on screen, so manual reordering waits for a clear list.
  const sortable = sortMode === 'manual' && !filterActive;
  const visibleDevices = useMemo(
    () =>
      filterDevices(devices, searchText, filterTags, {
        statuses,
        lockIndicators,
        autoRelockStates,
      }),
    [autoRelockStates, devices, filterTags, lockIndicators, searchText, statuses]
  );
  const detailDevice = useMemo(
    () => (detailKey ? devices.find((device) => device.key === detailKey) ?? null : null),
    [detailKey, devices]
  );

  useHotkeys([['mod+K', () => searchRef.current?.focus()]]);

  const addFilterTag = (tag: DeviceFilterTag) => {
    setFilterTags((prev) => (prev.includes(tag) ? prev : [...prev, tag]));
  };

  const clearFilters = () => {
    setSearchText('');
    setFilterTags([]);
  };

  // A typed status word becomes a chip rather than staying loose text, so the
  // chip row is always the full truth about what is being filtered.
  const handleSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      clearFilters();
      event.currentTarget.blur();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      const tag = resolveFilterAlias(searchText);
      if (tag) {
        event.preventDefault();
        addFilterTag(tag);
        setSearchText('');
      }
      return;
    }
    if (event.key === 'Backspace' && searchText === '' && filterTags.length > 0) {
      event.preventDefault();
      setFilterTags((prev) => prev.slice(0, -1));
    }
  };

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
  // Drives the menu labels. `stopped` is deliberately narrow: a board that is
  // offline or was never installed is not something "Start on all devices"
  // can fix, so counting it would promise an action that cannot work.
  const telemetryInstalledCount = useMemo(
    () =>
      devices.reduce(
        (count, device) =>
          count + (statuses[device.key]?.rp_telemetry?.installed ? 1 : 0),
        0
      ),
    [devices, statuses]
  );
  const telemetryStoppedCount = useMemo(
    () =>
      devices.reduce(
        (count, device) =>
          count + (statuses[device.key]?.rp_telemetry?.state === 'stopped' ? 1 : 0),
        0
      ),
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
          {/* Both fleet-wide telemetry actions live behind one menu: they are
              administrative (an install is a one-off or an upgrade, a start
              follows a reboot), so they do not belong at the same weight as
              Connect all. The counts answer "do I need this?" without opening
              anything. Mirrors the per-device telemetry menu above. */}
          <Menu shadow="md" position="bottom-start" withinPortal>
            <Menu.Target>
              <Button
                size="xs"
                color="gray"
                variant="subtle"
                leftSection={<IconTemperature size={14} />}
                rightSection={<IconChevronDown size={12} />}
                loading={telemetryAllBusy || telemetryStartAllBusy}
                disabled={devices.length === 0}
                title="Red Pitaya telemetry actions for every device"
              >
                Telemetry
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Red Pitaya telemetry (all devices)</Menu.Label>
              <Menu.Item onClick={() => setTelemetryAllOpen(true)}>
                {telemetryInstalledCount === devices.length
                  ? 'Update / reinstall on all devices'
                  : 'Install / update on all devices'}
              </Menu.Item>
              {/* No confirmation, unlike install-all: starting an already-running
                  service is a no-op and cannot damage a board, whereas install
                  rewrites the binary on every device. */}
              <Menu.Item
                disabled={telemetryStoppedCount === 0}
                onClick={() => {
                  setTelemetryStartAllBusy(true);
                  onStartTelemetryAll(devices.map((device) => device.key))
                    .catch(() => null)
                    .finally(() => setTelemetryStartAllBusy(false));
                }}
              >
                {telemetryStoppedCount > 0
                  ? `Start on all devices — ${telemetryStoppedCount} stopped`
                  : 'Start on all devices — none stopped'}
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
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

      <TextInput
        ref={searchRef}
        size="xs"
        placeholder="Search devices…"
        aria-label="Search devices"
        value={searchText}
        onChange={(event) => setSearchText(event.currentTarget.value)}
        onKeyDown={handleSearchKeyDown}
        leftSection={<IconSearch size={14} />}
        rightSection={
          filterActive ? (
            <ActionIcon
              size="xs"
              variant="subtle"
              color="gray"
              aria-label="Clear filters"
              onClick={clearFilters}
            >
              <IconX size={12} />
            </ActionIcon>
          ) : (
            <Text size="xs" c="dimmed" pr={4}>
              ⌘K
            </Text>
          )
        }
      />
      <Chip.Group
        multiple
        value={filterTags}
        onChange={(value) => setFilterTags(value as DeviceFilterTag[])}
      >
        <Group gap={4}>
          {DEVICE_FILTER_TAGS.map(({ tag, label }) => (
            <Chip key={tag} value={tag} size="xs" variant="outline">
              {label}
            </Chip>
          ))}
        </Group>
      </Chip.Group>

      <Group justify="space-between" align="flex-end" gap="xs" wrap="nowrap">
        <Select
          size="xs"
          label="Sort"
          value={sortMode}
          data={DEVICE_SORT_OPTIONS}
          onChange={(value) => {
            if (value) onSortModeChange(value as DeviceSortMode);
          }}
          style={{ flex: '0 0 130px' }}
        />
        {filterActive ? (
          <Text size="xs" c="dimmed" ta="right">
            {visibleDevices.length} of {devices.length} shown
            {sortMode === 'manual' ? ' · clear filters to reorder' : ''}
          </Text>
        ) : null}
      </Group>

      <Stack gap="xs" className="device-list-scroll">
        <SortableContext
          items={visibleDevices.map((device) => toDeviceListDragId(device.key))}
          strategy={verticalListSortingStrategy}
        >
          {visibleDevices.map((device) => (
            <DeviceRow
              key={device.key}
              device={device}
              status={statuses[device.key]}
              indicator={lockIndicators[device.key]}
              autoRelock={autoRelockStates[device.key] ?? statuses[device.key]?.auto_relock ?? undefined}
              autoRelockBusy={Boolean(autoRelockBusyKeys?.[device.key])}
              inActiveGroup={activeSet.has(device.key)}
              canAddToGroup={canAddToGroup}
              sortable={sortable}
              expanded={expandedKey === device.key}
              onToggleExpanded={(key) =>
                setExpandedKey((prev) => (prev === key ? null : key))
              }
              onOpenDetail={(target) => setDetailKey(target.key)}
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
          {visibleDevices.length === 0 ? (
            <Text size="xs" c="dimmed" ta="center" py="sm">
              {devices.length === 0
                ? 'No devices configured.'
                : 'No devices match the current filters.'}
            </Text>
          ) : null}
        </SortableContext>
      </Stack>

      {detailDevice ? (
        <DeviceDetailModal
          device={detailDevice}
          status={statuses[detailDevice.key]}
          indicator={lockIndicators[detailDevice.key]}
          autoRelock={
            autoRelockStates[detailDevice.key] ??
            statuses[detailDevice.key]?.auto_relock ??
            undefined
          }
          autoRelockBusy={Boolean(autoRelockBusyKeys?.[detailDevice.key])}
          telemetryBusy={Boolean(telemetryBusyKeys?.[detailDevice.key])}
          inActiveGroup={activeSet.has(detailDevice.key)}
          canAddToGroup={canAddToGroup}
          onClose={() => setDetailKey(null)}
          onEdit={openEdit}
          onDelete={onDelete}
          onAddToGroup={onAddToGroup}
          onToggleAutoRelock={onToggleAutoRelock}
          onStartServer={onStartServer}
          onConnect={onConnect}
          onDisconnect={onDisconnect}
          onRequestShutdown={setShutdownDevice}
          onRequestReboot={setRebootDevice}
          // Diagnostics is its own full-width modal; close this one first so the
          // two never stack.
          onRequestDiagnostics={(target) => {
            setDetailKey(null);
            onRequestDiagnostics(target);
          }}
          onTelemetryCommand={onTelemetryCommand}
          onStateUpdate={onStateUpdate}
        />
      ) : null}

      <Modal opened={opened} onClose={() => setOpened(false)} title={editingKey ? 'Edit device' : 'Add device'} zIndex={440}>
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
      {/* Above the detail modal (400): a destructive confirmation raised from
          inside it must stay readable and on top. */}
      <Modal
        opened={rebootDevice !== null}
        onClose={closeRebootModal}
        title="Reboot Red Pitaya?"
        centered
        zIndex={440}
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
        zIndex={440}
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
