import { memo, useCallback, useRef, useState } from 'react';
import { Button, Group, Text } from '@mantine/core';
import type { Device, LockIndicatorSnapshot, PlotFrame, StreamMessage } from '../types';
import { api } from '../api';
import { useDeviceStream } from '../hooks/useDeviceStream';
import { useInViewport } from '../hooks/useInViewport';
import { OverviewPlotPanel, type OverviewPlotPanelHandle } from './OverviewPlotPanel';
import { ThrottledStatusRow } from './ThrottledStatusRow';
import { resolveLockDisplay } from '../features/locks/lockState';
import { useDeviceStateEntry } from '../state/deviceStatesStore';

type DeviceOverviewCardProps = {
  device: Device;
  active: boolean;
  onOpenInGroup?: (deviceKey: string) => void;
  maxFps?: number;
  onStateUpdate: (deviceKey: string, message: StreamMessage) => void;
  onStreamActiveChange?: (deviceKey: string, active: boolean) => void;
};

// Compare two lock-indicator snapshots by the fields the card UI
// actually reads. Avoids forcing a re-render when the backend sends a
// fresh indicator object every frame whose content is identical.
const indicatorEqual = (
  a: LockIndicatorSnapshot | null,
  b: LockIndicatorSnapshot | null,
): boolean => {
  if (a === b) return true;
  if (!a || !b) return false;
  if (a.state !== b.state) return false;
  // Reasons are surfaced indirectly via the StatusRow but kept on the
  // card to keep the badge text stable -- compare by reference + length.
  if (a.reasons === b.reasons) return true;
  if (!a.reasons || !b.reasons) return false;
  if (a.reasons.length !== b.reasons.length) return false;
  for (let i = 0; i < a.reasons.length; i++) {
    if (a.reasons[i] !== b.reasons[i]) return false;
  }
  return true;
};

export const DeviceOverviewCard = memo(function DeviceOverviewCard({
  device,
  active,
  onOpenInGroup,
  maxFps,
  onStateUpdate,
  onStreamActiveChange,
}: DeviceOverviewCardProps) {
  const state = useDeviceStateEntry(device.key);
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Imperative handle into OverviewPlotPanel. Plot frames are pushed
  // straight to uPlot via this ref -- no React state, no render of
  // the card per frame.
  const panelRef = useRef<OverviewPlotPanelHandle>(null);
  // Latest frame stashed so ThrottledStatusRow can pull it at its
  // own cadence (~2 Hz) without forcing the card to re-render at the
  // full plot-frame rate.
  const latestFrameRef = useRef<PlotFrame | null>(null);
  // Card-level UI state. Only updates on actual transitions so the
  // card body / badge re-render rate is decoupled from the 10 Hz
  // streaming rate.
  const [lockIndicator, setLockIndicator] = useState<LockIndicatorSnapshot | null>(null);

  const visible = useInViewport(rootRef, { disabled: !active });
  const streamEnabled = active && visible;
  const connected = Boolean(state.status?.connected);
  const lockStateFromParams =
    typeof state.params.lock === 'boolean' ? state.params.lock : undefined;
  const lockStateFromStatus =
    typeof state.status?.lock === 'boolean' ? state.status.lock : undefined;
  const lockState = lockStateFromParams ?? lockStateFromStatus;
  const statusLabel = connected ? (lockState ? 'Locked' : 'Unlocked') : 'Disconnected';
  const lockDisplay = resolveLockDisplay({
    connected,
    lockEnabled: lockState,
    indicator: lockIndicator,
  });

  // Per-card frame throttle. With binary frames arriving at server
  // cadence (~10 fps already capped by max_fps), this drops extras
  // by wall-clock. Kept in a ref to avoid recreating the onMessage
  // closure when maxFps changes (which would otherwise tear down
  // the WS subscription).
  const lastPlotRef = useRef(0);
  const plotThrottleMsRef = useRef(maxFps && maxFps > 0 ? 1000 / maxFps : 0);
  plotThrottleMsRef.current = maxFps && maxFps > 0 ? 1000 / maxFps : 0;

  const onMessage = useCallback(
    (msg: StreamMessage) => {
      if (msg.type === 'plot_frame') {
        const throttleMs = plotThrottleMsRef.current;
        if (throttleMs > 0) {
          const now = performance.now();
          if (now - lastPlotRef.current < throttleMs) return;
          lastPlotRef.current = now;
        }
        // Push to uPlot directly -- imperative, no React render.
        panelRef.current?.applyFrame(msg);
        // Stash for the throttled StatusRow.
        latestFrameRef.current = msg;
        // Update lock indicator only when actually changed. setState
        // with the same value is a no-op; setState with a new value
        // re-renders just this card.
        const nextIndicator = msg.lock_indicator ?? null;
        setLockIndicator((prev) =>
          indicatorEqual(prev, nextIndicator) ? prev : nextIndicator,
        );
        // Smart-diff store write for the bookkeeper. The store layer
        // gates writes on lock/indicator transitions so most calls
        // are no-ops in steady state (commit 75a46b7).
        onStateUpdate(device.key, msg);
        return;
      }
      // Non-plot messages keep the old shape.
      onStateUpdate(device.key, msg);
    },
    [device.key, onStateUpdate],
  );

  const handleStreamOpen = useCallback(() => {
    onStreamActiveChange?.(device.key, true);
  }, [device.key, onStreamActiveChange]);
  const handleStreamClose = useCallback(() => {
    onStreamActiveChange?.(device.key, false);
  }, [device.key, onStreamActiveChange]);

  useDeviceStream(device.key, streamEnabled, onMessage, {
    maxFps,
    detail: 'summary',
    onOpen: handleStreamOpen,
    onClose: handleStreamClose,
  });

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
