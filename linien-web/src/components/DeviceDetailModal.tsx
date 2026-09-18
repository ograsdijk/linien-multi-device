import {
  ActionIcon,
  Alert,
  Button,
  Group,
  Menu,
  Modal,
  Stack,
  Text,
} from '@mantine/core';
import { IconPencil, IconTemperature, IconTrash } from '@tabler/icons-react';
import type {
  AutoRelockStatus,
  Device,
  DeviceStatus,
  LockIndicatorSnapshot,
  StreamMessage,
} from '../types';
import { resolveConnectionDisplay } from '../features/connection/connectionState';
import { resolveLockDisplay, resolveRelockTag } from '../features/locks/lockState';
import { usePlotStream } from '../features/devices/usePlotStream';
import { OverviewPlotPanel } from './OverviewPlotPanel';
import { ThrottledStatusRow } from './ThrottledStatusRow';
import { RpTemperatureLine } from './RpTemperatureLine';
import { RpHostMetricsLine } from './RpHostMetricsLine';
import type { TelemetryCommand } from './DeviceList';

export type DeviceDetailModalProps = {
  device: Device;
  status?: DeviceStatus;
  indicator?: LockIndicatorSnapshot;
  autoRelock?: AutoRelockStatus;
  autoRelockBusy: boolean;
  telemetryBusy: boolean;
  inActiveGroup: boolean;
  canAddToGroup: boolean;
  onClose: () => void;
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
  onStateUpdate?: (deviceKey: string, message: StreamMessage) => void;
};

// Separate component so the plot stream mounts and unmounts with the modal --
// opening a WS for a device nobody is looking at would defeat the point.
function DeviceDetailBody({
  device,
  connected,
  lockEnabled,
  onStateUpdate,
}: {
  device: Device;
  connected: boolean;
  lockEnabled?: boolean;
  onStateUpdate?: (deviceKey: string, message: StreamMessage) => void;
}) {
  const { panelRef, latestFrameRef, lockIndicator } = usePlotStream({
    deviceKey: device.key,
    enabled: connected,
    maxFps: 10,
    onStateUpdate,
  });
  return (
    <>
      <OverviewPlotPanel ref={panelRef} lockState={lockEnabled} initActive />
      <ThrottledStatusRow
        frameRef={latestFrameRef}
        intervalMs={500}
        lockIndicator={lockIndicator}
        connected={connected}
        lockEnabled={lockEnabled}
      />
    </>
  );
}

export function DeviceDetailModal({
  device,
  status,
  indicator,
  autoRelock,
  autoRelockBusy,
  telemetryBusy,
  inActiveGroup,
  canAddToGroup,
  onClose,
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
  onStateUpdate,
}: DeviceDetailModalProps) {
  const connected = Boolean(status?.connected);
  const connecting = Boolean(status?.connecting);
  const lockDisplay = resolveLockDisplay({
    connected,
    lockEnabled: status?.lock,
    indicator: indicator ?? null,
  });
  const relockDisplay = resolveRelockTag(autoRelock);
  const connectionDisplay = resolveConnectionDisplay(status);
  const telemetryInstalled = Boolean(status?.rp_telemetry?.installed);
  const recovery = status?.recovery;
  const recoveryActive = Boolean(
    recovery && !['completed', 'failed', 'cancelled'].includes(recovery.phase)
  );
  // Reasons are the "why" behind a marginal or lost lock; the list row has no
  // room for them, which is half the reason this modal exists.
  const reasons = indicator?.reasons ?? [];

  return (
    <Modal
      opened
      onClose={onClose}
      title={device.name || device.key || 'Device'}
      size="clamp(40rem, 80vw, 64rem)"
      centered
      zIndex={400}
    >
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <div>
            <Group gap={4} align="center" wrap="nowrap">
              <Text size="sm" c="dimmed">
                {device.host}:{device.port}
              </Text>
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
            <RpTemperatureLine
              status={status}
              deviceKey={device.key}
              busy={telemetryBusy}
              onAction={(action) => {
                void onTelemetryCommand(device.key, action === 'update' ? 'install' : action);
              }}
            />
            <RpHostMetricsLine status={status} deviceKey={device.key} />
          </div>
          <Group gap={6} align="center">
            <div
              className={`device-tag status-${
                status?.last_error
                  ? 'error'
                  : connecting
                    ? 'connecting'
                    : connected
                      ? 'connected'
                      : 'disconnected'
              }`}
            >
              {status?.last_error
                ? 'Error'
                : connecting
                  ? 'Connecting'
                  : connected
                    ? 'Connected'
                    : 'Disconnected'}
            </div>
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
              className={`device-tag device-tag-button status-lock-${relockDisplay.uiState}`}
              onClick={() => onToggleAutoRelock(device.key, !relockDisplay.enabled)}
              disabled={autoRelockBusy}
              style={{
                cursor: autoRelockBusy ? 'default' : 'pointer',
                opacity: autoRelockBusy ? 0.6 : 1,
              }}
              title="Toggle auto relock"
            >
              {relockDisplay.label}
            </button>
          </Group>
        </Group>

        {status?.last_error ? (
          <Alert color="red" variant="light">
            {status.last_error}
          </Alert>
        ) : null}
        {connectionDisplay.show ? (
          <Text size="sm" c="dimmed">
            {connectionDisplay.tooltip}
          </Text>
        ) : null}
        {recovery && recovery.phase !== 'completed' ? (
          <Text size="sm" c={recovery.phase === 'failed' ? 'red' : 'orange'}>
            Reboot: {recovery.phase}
            {recovery.error ? ` — ${recovery.error}` : ''}
          </Text>
        ) : null}
        {reasons.length > 0 ? (
          <Text size="sm" c="dimmed">
            Lock indicator: {reasons.join(', ')}
          </Text>
        ) : null}

        {connected ? (
          <DeviceDetailBody
            device={device}
            connected={connected}
            lockEnabled={status?.lock ?? undefined}
            onStateUpdate={onStateUpdate}
          />
        ) : (
          <Text size="sm" c="dimmed">
            Connect the device to see its live signal.
          </Text>
        )}

        <Group gap="xs">
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
            disabled={connected || connecting || recoveryActive}
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
                disabled={recoveryActive}
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
              disabled={connecting || recoveryActive}
            >
              Connect
            </Button>
          )}
          <Button
            size="xs"
            color="red"
            variant="outline"
            onClick={() => onRequestReboot(device)}
            disabled={connecting || recoveryActive}
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
          <Menu shadow="md" position="bottom-start" withinPortal zIndex={500}>
            <Menu.Target>
              <Button
                size="xs"
                variant="subtle"
                color="gray"
                leftSection={<IconTemperature size={14} />}
                loading={telemetryBusy}
              >
                Telemetry
              </Button>
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
          <ActionIcon
            size="sm"
            color="red"
            variant="subtle"
            aria-label={`Remove ${device.name || 'device'}`}
            onClick={() => {
              onClose();
              void onDelete(device.key);
            }}
            disabled={recoveryActive}
            ml="auto"
          >
            <IconTrash size={14} />
          </ActionIcon>
        </Group>
      </Stack>
    </Modal>
  );
}
