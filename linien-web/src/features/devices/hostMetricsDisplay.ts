import type { DeviceStatus, RpMetrics } from '../../types';
import { readingIsStale, type TelemetryTone } from './telemetryDisplay';

// Display hints only. Nothing here changes device behaviour; deciding what to
// do about a full disk or a pegged CPU stays an operator decision.
//
// A Red Pitaya runs from an SD card with ~500 MB of RAM. Linux is expected to
// look fairly full (page cache counts as used), so the memory warning sits
// high and keys off MemAvailable, which is the kernel's own estimate of what a
// new allocation could actually get.
export const MEMORY_WARN_PERCENT = 90;
export const MEMORY_CRITICAL_PERCENT = 97;
// Below this the SD card is close enough to full that logs, a telemetry
// install, or an apt operation will start failing.
export const DISK_WARN_KB = 200 * 1024;
export const DISK_CRITICAL_KB = 50 * 1024;
// Sustained CPU at this level on a 2-core Zynq means something is wrong; the
// normal load for linien-server is a few percent.
export const CPU_WARN_PERCENT = 85;

export type HostMetricsSegment = {
  /** Stable identifier for tests and React keys. */
  id: 'cpu' | 'memory' | 'disk' | 'uptime';
  text: string;
  tone: TelemetryTone;
  /** Longer form for the element's title attribute. */
  title: string;
};

export const formatBytesFromKb = (kb: number): string => {
  if (kb >= 1024 * 1024) return `${(kb / (1024 * 1024)).toFixed(1)} GB`;
  if (kb >= 1024) return `${Math.round(kb / 1024)} MB`;
  return `${Math.round(kb)} kB`;
};

/**
 * "3d 4h", "2h 15m", "8m". Coarse on purpose: the useful question is whether
 * the board rebooted recently, not how many seconds ago.
 */
export const formatUptime = (seconds: number): string => {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
};

const toneFor = (value: number, warn: number, critical: number): TelemetryTone => {
  if (value >= critical) return 'critical';
  if (value >= warn) return 'warn';
  return 'normal';
};

/**
 * Turn the board's host metrics into the segments of the line under the
 * temperature. Returns an empty array when there is nothing trustworthy to
 * show, which is the caller's signal to render nothing at all.
 *
 * The staleness test is the same one the temperature uses, against the same
 * age: both were sampled on one request, so they must go stale together rather
 * than leaving a live-looking CPU figure beside a withdrawn temperature.
 */
export const resolveHostMetrics = (
  status: DeviceStatus | null | undefined,
  readingAgeS?: number | null
): HostMetricsSegment[] => {
  const metrics: RpMetrics | null = status?.rp_metrics ?? null;
  if (!metrics) return [];
  if (readingIsStale(status, readingAgeS)) return [];

  const segments: HostMetricsSegment[] = [];
  const { cpu_percent: cpu, mem_used_percent: memUsed } = metrics;

  if (typeof cpu === 'number' && Number.isFinite(cpu)) {
    segments.push({
      id: 'cpu',
      text: `CPU ${Math.round(cpu)}%`,
      tone: toneFor(cpu, CPU_WARN_PERCENT, 101),
      // Says what the number is an average over: it is the busy fraction
      // between the gateway's last two polls, not an instantaneous sample.
      title: 'CPU busy since the previous poll',
    });
  }

  if (typeof memUsed === 'number' && Number.isFinite(memUsed)) {
    const available = metrics.mem_available_kb;
    segments.push({
      id: 'memory',
      text: `RAM ${Math.round(memUsed)}%`,
      tone: toneFor(memUsed, MEMORY_WARN_PERCENT, MEMORY_CRITICAL_PERCENT),
      title:
        typeof available === 'number' && Number.isFinite(available)
          ? `${formatBytesFromKb(available)} available`
          : 'Memory in use',
    });
  }

  const free = metrics.root_free_kb;
  if (typeof free === 'number' && Number.isFinite(free)) {
    segments.push({
      id: 'disk',
      text: `disk ${formatBytesFromKb(free)}`,
      // Inverted: less free space is worse, so the thresholds are floors.
      tone:
        free <= DISK_CRITICAL_KB
          ? 'critical'
          : free <= DISK_WARN_KB
            ? 'warn'
            : 'normal',
      title: 'Free space on the board’s root filesystem (SD card)',
    });
  }

  const uptime = metrics.uptime_s;
  if (typeof uptime === 'number' && Number.isFinite(uptime)) {
    segments.push({
      id: 'uptime',
      text: `up ${formatUptime(uptime)}`,
      tone: 'normal',
      title: 'Time since the board last booted',
    });
  }

  return segments;
};
