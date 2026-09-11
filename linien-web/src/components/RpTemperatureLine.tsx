import { Button, Group, Text } from '@mantine/core';
import type { DeviceStatus } from '../types';
import {
  resolveTelemetryDisplay,
  type TelemetryAction,
  type TelemetryDisplay,
} from '../features/devices/telemetryDisplay';

const TONE_COLOR: Record<TelemetryDisplay['tone'], string> = {
  normal: 'dimmed',
  warn: 'orange',
  critical: 'red',
  muted: 'dimmed',
};

type RpTemperatureLineProps = {
  status: DeviceStatus | null | undefined;
  /** Omit to render the reading without any remedy button (overview cards). */
  onAction?: (action: Exclude<TelemetryAction, null>) => void;
  busy?: boolean;
};

/**
 * The "RP temperature: 57.3 °C" line shown under a device's host/IP.
 *
 * This is the Red Pitaya's Zynq die temperature, not a laser temperature —
 * the label and tooltip both say so. While telemetry is unavailable it shows
 * the reason plus, where one exists, a single-click remedy.
 */
export function RpTemperatureLine({ status, onAction, busy }: RpTemperatureLineProps) {
  const display = resolveTelemetryDisplay(status);
  return (
    <>
      <Text
        size="xs"
        c={TONE_COLOR[display.tone]}
        title="Red Pitaya (Zynq) die temperature"
      >
        RP temperature: {display.value}
      </Text>
      {display.detail ? (
        <Group gap={6} align="center" wrap="nowrap">
          <Text size="xs" c="dimmed">
            {display.detail}
          </Text>
          {display.action && onAction ? (
            <Button
              size="compact-xs"
              variant="subtle"
              color="blue"
              loading={busy}
              onClick={() => onAction(display.action as Exclude<TelemetryAction, null>)}
            >
              {display.actionLabel}
            </Button>
          ) : null}
        </Group>
      ) : null}
    </>
  );
}
