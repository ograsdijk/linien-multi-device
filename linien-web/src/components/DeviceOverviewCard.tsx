import { memo, useCallback, useRef } from 'react';
import { Button, Group, Text } from '@mantine/core';
import type {
  Device,
  DeviceDiagnosis,
  RpTelemetryState,
  StreamMessage,
} from '../types';
import { api } from '../api';
import { useInViewport } from '../hooks/useInViewport';
import { OverviewPlotPanel } from './OverviewPlotPanel';
import { ThrottledStatusRow } from './ThrottledStatusRow';
import { resolveConnectionDisplay } from '../features/connection/connectionState';
import { usePlotStream } from '../features/devices/usePlotStream';
import { resolveLockDisplay } from '../features/locks/lockState';
import { RpTemperatureLine } from './RpTemperatureLine';
import { type DeviceStateEntry, useDeviceStateSlice } from '../state/deviceStatesStore';

// Narrow slice this card actually consumes: the lock primitive and a
// few status fields. The previous useDeviceStateEntry subscription
// fired a card re-render on EVERY param batch (since the entry's
// top-level ref changes whenever any param updates), even though
// nothing the card displays had moved. This selector ignores
// param updates that don't touch `lock`.
type CardSlice = {
  lockFromParams: boolean | undefined;
  connected: boolean;
  connecting: boolean;
  lockFromStatus: boolean | undefined;
  diagnosis: DeviceDiagnosis | null;
  // Only the two fields the temperature line reads, so the card still ignores
  // param traffic and only re-renders when the reading actually moves.
  temperatureC: number | null;
  telemetryState: RpTelemetryState | null;
};

const selectCardSlice = (entry: DeviceStateEntry): CardSlice => {
  const lockRaw = entry.params.lock;
  const lockFromParams = typeof lockRaw === 'boolean' ? lockRaw : undefined;
  const s = entry.status;
  return {
    lockFromParams,
    connected: Boolean(s?.connected),
    connecting: Boolean(s?.connecting),
    lockFromStatus: typeof s?.lock === 'boolean' ? s.lock : undefined,
    diagnosis: s?.diagnosis ?? null,
    temperatureC: typeof s?.rp_temperature_c === 'number' ? s.rp_temperature_c : null,
    telemetryState: s?.rp_telemetry?.state ?? null,
  };
};

const cardSliceEqual = (a: CardSlice, b: CardSlice): boolean => {
  return (
    a.lockFromParams === b.lockFromParams &&
    a.connected === b.connected &&
    a.connecting === b.connecting &&
    a.lockFromStatus === b.lockFromStatus &&
    a.diagnosis?.category === b.diagnosis?.category &&
    a.diagnosis?.lock_state === b.diagnosis?.lock_state &&
    a.diagnosis?.probed_at === b.diagnosis?.probed_at &&
    a.temperatureC === b.temperatureC &&
    a.telemetryState === b.telemetryState
  );
};

type DeviceOverviewCardProps = {
  device: Device;
  active: boolean;
  onOpenInGroup?: (deviceKey: string) => void;
  maxFps?: number;
  onStateUpdate: (deviceKey: string, message: StreamMessage) => void;
  onStreamActiveChange?: (deviceKey: string, active: boolean) => void;
};

export const DeviceOverviewCard = memo(function DeviceOverviewCard({
  device,
  active,
  onOpenInGroup,
  maxFps,
  onStateUpdate,
  onStreamActiveChange,
}: DeviceOverviewCardProps) {
  const slice = useDeviceStateSlice(device.key, selectCardSlice, cardSliceEqual);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const visible = useInViewport(rootRef, { disabled: !active });
  const streamEnabled = active && visible;

  const handleStreamOpen = useCallback(() => {
    onStreamActiveChange?.(device.key, true);
  }, [device.key, onStreamActiveChange]);
  const handleStreamClose = useCallback(() => {
    onStreamActiveChange?.(device.key, false);
  }, [device.key, onStreamActiveChange]);

  const { panelRef, latestFrameRef, lockIndicator } = usePlotStream({
    deviceKey: device.key,
    enabled: streamEnabled,
    maxFps,
    onStateUpdate,
    onOpen: handleStreamOpen,
    onClose: handleStreamClose,
  });

  const connected = slice.connected;
  const lockState = slice.lockFromParams ?? slice.lockFromStatus;
  const statusLabel = connected ? (lockState ? 'Locked' : 'Unlocked') : 'Disconnected';
  const lockDisplay = resolveLockDisplay({
    connected,
    lockEnabled: lockState,
    indicator: lockIndicator,
  });
  const connectionDisplay = resolveConnectionDisplay({
    connected: slice.connected,
    connecting: slice.connecting,
    diagnosis: slice.diagnosis,
  });

  return (
    <div className="overview-card" ref={rootRef}>
      <Group justify="space-between" align="center" mb="xs">
        <div>
          <Text fw={600}>{device.name || 'Unnamed device'}</Text>
          <Text size="xs" c="dimmed">
            {device.host}:{device.port}
          </Text>
          <RpTemperatureLine
            deviceKey={device.key}
            status={{
              connected: slice.connected,
              connecting: slice.connecting,
              rp_temperature_c: slice.temperatureC,
              rp_telemetry: slice.telemetryState ? { state: slice.telemetryState } : null,
            }}
          />
        </div>
        <Group gap="xs" align="center">
          <Text size="xs" c="dimmed">
            {statusLabel}
          </Text>
          {connectionDisplay.show ? (
            <div
              className={`device-tag diag-${connectionDisplay.color}`}
              title={connectionDisplay.tooltip}
            >
              {connectionDisplay.label}
            </div>
          ) : null}
          <div className={`device-tag status-lock-${lockDisplay.uiState}`}>{lockDisplay.label}</div>
          {connected ? (
            <Button
              size="xs"
              variant="light"
              color="red"
              onClick={() => api.disconnectDevice(device.key).catch(() => null)}
            >
              Disconnect
            </Button>
          ) : (
            <Button
              size="xs"
              variant="light"
              color="green"
              onClick={() => api.connectDevice(device.key).catch(() => null)}
            >
              Connect
            </Button>
          )}
          <Button
            size="xs"
            variant="default"
            onClick={() => onOpenInGroup?.(device.key)}
            disabled={!onOpenInGroup}
          >
            Open in group
          </Button>
        </Group>
      </Group>
      <OverviewPlotPanel
        ref={panelRef}
        lockState={lockState}
        initActive={streamEnabled}
      />
      <ThrottledStatusRow
        frameRef={latestFrameRef}
        intervalMs={500}
        lockIndicator={lockIndicator}
        connected={connected}
        lockEnabled={lockState}
      />
    </div>
  );
});
