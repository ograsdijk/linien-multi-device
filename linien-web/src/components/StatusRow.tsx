import { Group, Text } from '@mantine/core';
import type { PlotFrame } from '../types';

export function StatusRow({ plotFrame }: { plotFrame?: PlotFrame | null }) {
  if (!plotFrame) {
    return null;
  }
  const isLocked = Boolean(plotFrame.lock);
  const power1 = plotFrame.signal_power?.channel1;
  const power2 = plotFrame.signal_power?.channel2;
  const showPowers =
    !isLocked &&
    ((power1 !== null && power1 !== undefined) ||
      (power2 !== null && power2 !== undefined));
  return (
    <div className="panel" style={{ padding: 8 }}>
      <Group justify="space-between" className="status-row">
        {showPowers ? (
          <>
            <Text>
              Ch1: {power1 !== null && power1 !== undefined ? power1.toFixed(2) : '--'} dBm
            </Text>
            <Text>
              Ch2: {power2 !== null && power2 !== undefined ? power2.toFixed(2) : '--'} dBm
            </Text>
          </>
        ) : null}
        {plotFrame.stats?.error_std !== null && plotFrame.stats?.error_std !== undefined ? (
          <Text>Error std: {plotFrame.stats.error_std.toFixed(2)}</Text>
        ) : null}
        {plotFrame.stats?.control_std !== null && plotFrame.stats?.control_std !== undefined ? (
          <Text>Control std: {plotFrame.stats.control_std.toFixed(2)}</Text>
        ) : null}
      </Group>
    </div>
  );
}
