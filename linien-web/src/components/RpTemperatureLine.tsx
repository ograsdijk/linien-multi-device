import { Button, Group, Text } from '@mantine/core';
import { useEffect, useState } from 'react';
import type { DeviceStatus } from '../types';
import {
  resolveTelemetryDisplay,
  type TelemetryAction,
  type TelemetryDisplay,
} from '../features/devices/telemetryDisplay';
import { effectiveReadingAgeS } from '../features/devices/statusFreshness';
import { RpHostMetricsLine } from './RpHostMetricsLine';

// How often the reading re-checks its own age. Well below the staleness window
// (90 s), so a reading ages out promptly, and far too slow to matter for
// rendering cost -- the tick only re-runs this one line per card.
const AGE_TICK_MS = 15000;

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
  /** Omit to trust the gateway's state alone, with no local ageing. Supplying
   *  it is what makes a frozen status decay instead of showing a stale
   *  temperature indefinitely. */
  deviceKey?: string;
};

/**
 * The "RP temperature: 57.3 °C" line shown under a device's host/IP.
 *
 * This is the Red Pitaya's Zynq die temperature, not a laser temperature —
 * the label and tooltip both say so. While telemetry is unavailable it shows
 * the reason plus, where one exists, a single-click remedy.
 *
 * The reading ages locally: statuses are pushed only on change, so without a
 * clock of its own this line would display the last number it ever received
 * for as long as the page stayed open.
 *
 * The board's CPU/RAM/disk line is rendered here rather than beside this one so
 * that both share this component's ageing tick: they come from the same STATUS
 * request, and two timers could let one withdraw a stale reading while the
 * other still showed it.
 */
export function RpTemperatureLine({
  status,
  onAction,
  busy,
  deviceKey,
}: RpTemperatureLineProps) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    // Only a displayed reading can go stale; anything else is already showing
    // a reason, so there is nothing for a ticker to change.
    if (status?.rp_telemetry?.state !== 'running') return;
    const timer = window.setInterval(() => setNowMs(Date.now()), AGE_TICK_MS);
    return () => window.clearInterval(timer);
  }, [status?.rp_telemetry?.state]);
  const display = resolveTelemetryDisplay(
    status,
    effectiveReadingAgeS(deviceKey, status?.rp_temperature_age_s, nowMs)
  );
  return (
    <>
      <Text
        size="xs"
        c={TONE_COLOR[display.tone]}
        title="Red Pitaya (Zynq) die temperature"
      >
        RP temperature: {display.value}
      </Text>
      <RpHostMetricsLine status={status} deviceKey={deviceKey} nowMs={nowMs} />
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
