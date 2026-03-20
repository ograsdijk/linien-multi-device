import { Group, Text } from '@mantine/core';
import type { LockIndicatorSnapshot, PlotFrame } from '../types';
import { resolveLockDisplay } from '../features/locks/lockState';

type StatusRowProps = {
  plotFrame?: PlotFrame | null;
  lockIndicator?: LockIndicatorSnapshot | null;
  connected?: boolean;
  lockEnabled?: boolean;
};

export function StatusRow({ plotFrame, lockIndicator, connected, lockEnabled }: StatusRowProps) {
  if (!plotFrame) {
    return null;
  }
  const isLocked = Boolean(plotFrame.lock);
  const indicator = lockIndicator ?? plotFrame.lock_indicator ?? null;
  const display = resolveLockDisplay({
    connected: connected !== false,
    lockEnabled,
    indicator,
  });
  const indicatorReason =
    indicator?.reasons && indicator.reasons.length > 0 ? indicator.reasons[0] : null;
  const indicatorText = display.label;
  const showReason = connected !== false && lockEnabled !== false && Boolean(indicatorReason);
  const power1 = plotFrame.signal_power?.channel1;
  const power2 = plotFrame.signal_power?.channel2;
  const showPowers =
    !isLocked &&
    ((power1 !== null && power1 !== undefined) || (power2 !== null && power2 !== undefined));
  const formatSci = (value: number) => value.toExponential(3);
  return (
    <div className="panel" style={{ padding: 8 }}>
      <Group justify="space-between" className="status-row">
        <Text>
          {indicatorText}
          {showReason ? ` (${indicatorReason})` : ''}
        </Text>
        {showPowers ? (
          <>
            <Text>Ch1: {power1 !== null && power1 !== undefined ? power1.toFixed(2) : '--'} dBm</Text>
            <Text>Ch2: {power2 !== null && power2 !== undefined ? power2.toFixed(2) : '--'} dBm</Text>
          </>
        ) : null}
        {plotFrame.stats?.error_std !== null && plotFrame.stats?.error_std !== undefined ? (
          <Text>Error std: {formatSci(plotFrame.stats.error_std)} V</Text>
        ) : null}
        {plotFrame.stats?.control_std !== null && plotFrame.stats?.control_std !== undefined ? (
          <Text>Control std: {formatSci(plotFrame.stats.control_std)} V</Text>
        ) : null}
      </Group>
    </div>
  );
}
