import { memo, useCallback, useRef } from 'react';
import { Button, Group, Text } from '@mantine/core';
import type { Device, DeviceStatus, PlotFrame, StreamMessage } from '../types';
import { api } from '../api';
import { useDeviceStream } from '../hooks/useDeviceStream';
import { useInViewport } from '../hooks/useInViewport';
import { PlotPanel } from './PlotPanel';
import { StatusRow } from './StatusRow';
import { resolveLockDisplay } from '../features/locks/lockState';

type DeviceOverviewCardProps = {
  device: Device;
  state: {
    params: Record<string, any>;
    plotFrame?: PlotFrame | null;
    status?: DeviceStatus | null;
  };
  active: boolean;
  onOpenInGroup?: (deviceKey: string) => void;
  maxFps?: number;
  onStateUpdate: (deviceKey: string, message: StreamMessage) => void;
};

export const DeviceOverviewCard = memo(function DeviceOverviewCard({
  device,
  state,
  active,
  onOpenInGroup,
  maxFps,
  onStateUpdate,
}: DeviceOverviewCardProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const visible = useInViewport(rootRef, { disabled: !active });
  const streamEnabled = active && visible;
  const connected = Boolean(state.status?.connected);
  const lockStateFromParams = typeof state.params.lock === 'boolean' ? state.params.lock : undefined;
  const lockStateFromStatus = typeof state.status?.lock === 'boolean' ? state.status.lock : undefined;
  const lockState = lockStateFromParams ?? lockStateFromStatus;
  const sweepCenterRaw = state.params.sweep_center;
  const sweepCenterNum = sweepCenterRaw == null ? NaN : Number(sweepCenterRaw);
  const sweepCenter = Number.isFinite(sweepCenterNum) ? sweepCenterNum : undefined;
  const sweepAmplitudeRaw = state.params.sweep_amplitude;
  const sweepAmplitudeNum = sweepAmplitudeRaw == null ? NaN : Number(sweepAmplitudeRaw);
  const sweepAmplitude = Number.isFinite(sweepAmplitudeNum) ? sweepAmplitudeNum : undefined;
  const statusLabel = connected ? (lockState ? 'Locked' : 'Unlocked') : 'Disconnected';
  const lockIndicator = state.plotFrame?.lock_indicator ?? null;
  const lockDisplay = resolveLockDisplay({
    connected,
    lockEnabled: lockState,
    indicator: lockIndicator,
  });

  const lastPlotRef = useRef(0);
  const plotThrottleMs = maxFps && maxFps > 0 ? 1000 / maxFps : 0;
  const onMessage = useCallback(
    (msg: StreamMessage) => {
      if (plotThrottleMs > 0 && msg.type === 'plot_frame') {
        const now = performance.now();
        if (now - lastPlotRef.current < plotThrottleMs) {
          return;
        }
        lastPlotRef.current = now;
      }
      onStateUpdate(device.key, msg);
    },
    [device.key, onStateUpdate, plotThrottleMs]
  );

  useDeviceStream(device.key, streamEnabled, onMessage, { maxFps });

  return (
    <div className="overview-card" ref={rootRef}>
      <Group justify="space-between" align="center" mb="xs">
        <div>
          <Text fw={600}>{device.name || 'Unnamed device'}</Text>
          <Text size="xs" c="dimmed">
            {device.host}:{device.port}
          </Text>
        </div>
        <Group gap="xs" align="center">
          <Text size="xs" c="dimmed">
            {statusLabel}
          </Text>
          <div className={`device-tag status-lock-${lockDisplay.uiState}`}>{lockDisplay.label}</div>
          {connected ? (
            <Button size="xs" variant="light" color="red" onClick={() => api.disconnectDevice(device.key).catch(() => null)}>
              Disconnect
            </Button>
          ) : (
            <Button size="xs" variant="light" color="green" onClick={() => api.connectDevice(device.key).catch(() => null)}>
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
      <PlotPanel
        plotFrame={state.plotFrame}
        selectionMode={null}
        lockState={lockState}
        sweepCenter={sweepCenter}
        sweepAmplitude={sweepAmplitude}
        showManualTarget={false}
      />
      <StatusRow
        plotFrame={state.plotFrame}
        lockIndicator={lockIndicator}
        connected={connected}
        lockEnabled={lockState}
      />
    </div>
  );
});
