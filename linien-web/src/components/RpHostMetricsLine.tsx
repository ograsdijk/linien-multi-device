import { Group, Text } from '@mantine/core';
import { Fragment } from 'react';
import type { DeviceStatus } from '../types';
import { resolveHostMetrics } from '../features/devices/hostMetricsDisplay';
import type { TelemetryTone } from '../features/devices/telemetryDisplay';
import { effectiveReadingAgeS } from '../features/devices/statusFreshness';

const TONE_COLOR: Record<TelemetryTone, string> = {
  normal: 'dimmed',
  warn: 'orange',
  critical: 'red',
  muted: 'dimmed',
};

type RpHostMetricsLineProps = {
  status: DeviceStatus | null | undefined;
  /** Same key the temperature line uses; it is what lets the reading age. */
  deviceKey?: string;
  /** Re-read on each tick of the caller's ageing timer. */
  nowMs?: number;
};

/**
 * "CPU 4% · RAM 41% · disk 1.1 GB · 5V 4.98 V · up 3d 4h" under the temperature.
 *
 * Sampled on the same request as the temperature, so it is gated on the same
 * age: when the reading goes stale this line disappears rather than leaving a
 * live-looking CPU figure beside a withdrawn temperature. A board running a
 * daemon older than 1.2.0 reports no metrics and renders nothing; one older
 * than 1.3.0 reports no supply voltage, and that segment is simply absent.
 */
export function RpHostMetricsLine({ status, deviceKey, nowMs }: RpHostMetricsLineProps) {
  const segments = resolveHostMetrics(
    status,
    effectiveReadingAgeS(deviceKey, status?.rp_temperature_age_s, nowMs)
  );
  if (segments.length === 0) return null;
  return (
    <Group gap={6} align="center" wrap="wrap">
      {segments.map((segment, index) => (
        <Fragment key={segment.id}>
          {/* The separator is its own node so each metric's text stands alone:
              it carries no meaning, and folding it into the label would make
              every segment but the first read as "· RAM 41%". */}
          {index > 0 ? (
            <Text size="xs" c="dimmed" aria-hidden>
              ·
            </Text>
          ) : null}
          <Text size="xs" c={TONE_COLOR[segment.tone]} title={segment.title}>
            {segment.text}
          </Text>
        </Fragment>
      ))}
    </Group>
  );
}
