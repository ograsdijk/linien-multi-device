import { describe, expect, it } from 'vitest';
import type { DeviceStatus } from '../../types';
import {
  formatBytesFromKb,
  formatUptime,
  resolveHostMetrics,
} from './hostMetricsDisplay';

const status = (overrides: Partial<DeviceStatus>): DeviceStatus => ({
  connected: true,
  connecting: false,
  rp_telemetry: { state: 'running', stale_after_s: 90 },
  ...overrides,
});

const healthy = status({
  rp_temperature_c: 57.3,
  rp_metrics: {
    cpu_percent: 4.2,
    load1: 0.1,
    mem_total_kb: 509216,
    mem_available_kb: 300000,
    mem_used_percent: 41.1,
    uptime_s: 273_600,
    root_free_kb: 1_204_880,
  },
});

const text = (metrics: ReturnType<typeof resolveHostMetrics>) =>
  metrics.map((segment) => segment.text);

const toneOf = (metrics: ReturnType<typeof resolveHostMetrics>, id: string) =>
  metrics.find((segment) => segment.id === id)?.tone;

describe('resolveHostMetrics', () => {
  it('renders CPU, memory, disk and uptime', () => {
    expect(text(resolveHostMetrics(healthy, 5))).toEqual([
      'CPU 4%',
      'RAM 41%',
      'disk 1.1 GB',
      'up 3d 4h',
    ]);
  });

  it('shows nothing for a board whose daemon reports no metrics', () => {
    // Every board in the field runs the older daemon until it is reinstalled.
    expect(resolveHostMetrics(status({ rp_metrics: null }), 5)).toEqual([]);
  });

  it('omits only the metrics the board could not read', () => {
    const partial = status({
      rp_metrics: { cpu_percent: 4.2, uptime_s: 90 },
    });
    expect(text(resolveHostMetrics(partial, 5))).toEqual(['CPU 4%', 'up 1m']);
  });

  it('withdraws everything once the reading has aged out', () => {
    // Sampled on the same request as the temperature, so a stale temperature
    // means stale metrics -- showing a live-looking CPU figure beside a
    // withdrawn temperature would be worse than showing neither.
    expect(resolveHostMetrics(healthy, 200)).toEqual([]);
  });

  it('warns about memory before it is exhausted', () => {
    const tight = status({ rp_metrics: { mem_used_percent: 93 } });
    expect(toneOf(resolveHostMetrics(tight, 5), 'memory')).toBe('warn');

    const critical = status({ rp_metrics: { mem_used_percent: 98 } });
    expect(toneOf(resolveHostMetrics(critical, 5), 'memory')).toBe('critical');
  });

  it('warns about a filling SD card', () => {
    // Inverted thresholds: here less is worse.
    const low = status({ rp_metrics: { root_free_kb: 150 * 1024 } });
    expect(toneOf(resolveHostMetrics(low, 5), 'disk')).toBe('warn');

    const critical = status({ rp_metrics: { root_free_kb: 20 * 1024 } });
    expect(toneOf(resolveHostMetrics(critical, 5), 'disk')).toBe('critical');

    const fine = status({ rp_metrics: { root_free_kb: 2 * 1024 * 1024 } });
    expect(toneOf(resolveHostMetrics(fine, 5), 'disk')).toBe('normal');
  });

  it('warns about a pegged CPU', () => {
    const busy = status({ rp_metrics: { cpu_percent: 97 } });
    expect(toneOf(resolveHostMetrics(busy, 5), 'cpu')).toBe('warn');
  });

  it('says what the CPU figure is an average over', () => {
    // It is the busy fraction between two polls, not an instantaneous sample,
    // and a reader who assumes otherwise misreads a quiet board as a busy one.
    const [cpu] = resolveHostMetrics(status({ rp_metrics: { cpu_percent: 4 } }), 5);
    expect(cpu.title).toBe('CPU busy since the previous poll');
  });

  it('shows the vccaux rail between disk and uptime', () => {
    const withRail = status({
      ...healthy,
      rp_metrics: { ...healthy.rp_metrics, vccaux_v: 1.802 },
    });
    expect(text(resolveHostMetrics(withRail, 5))).toEqual([
      'CPU 4%',
      'RAM 41%',
      'disk 1.1 GB',
      'VCCAUX 1.80 V',
      'up 3d 4h',
    ]);
  });

  it('omits the rail for a daemon that does not report it', () => {
    const noRail = status({ rp_metrics: { cpu_percent: 4, vccaux_v: null } });
    expect(resolveHostMetrics(noRail, 5).map((s) => s.id)).toEqual(['cpu']);
    expect(resolveHostMetrics(healthy, 5).map((s) => s.id)).not.toContain('vccaux');
  });

  it('does not judge the rail voltage', () => {
    // No threshold yet: the number is for comparing boards.
    for (const volts of [1.7, 1.8, 1.9]) {
      const board = status({ rp_metrics: { vccaux_v: volts } });
      expect(toneOf(resolveHostMetrics(board, 5), 'vccaux')).toBe('normal');
    }
  });

  it('says the rail reading cannot capture brief droops', () => {
    const [rail] = resolveHostMetrics(status({ rp_metrics: { vccaux_v: 1.8 } }), 5);
    expect(rail.title).toMatch(/brief droops are not captured/);
  });

  it('says the rail is not the 5 V input', () => {
    // The field it replaced claimed to be the board supply; this one must not
    // be mistaken for it.
    const [rail] = resolveHostMetrics(status({ rp_metrics: { vccaux_v: 1.8 } }), 5);
    expect(rail.title).toMatch(/regulated output/);
  });

  it('withdraws the rail with the rest once stale', () => {
    const board = status({ rp_metrics: { vccaux_v: 1.8 } });
    expect(resolveHostMetrics(board, 200)).toEqual([]);
  });
});

describe('formatting', () => {
  it('scales free space to something readable', () => {
    expect(formatBytesFromKb(1_204_880)).toBe('1.1 GB');
    expect(formatBytesFromKb(150 * 1024)).toBe('150 MB');
    expect(formatBytesFromKb(512)).toBe('512 kB');
  });

  it('reports uptime coarsely', () => {
    // The useful question is whether the board rebooted recently.
    expect(formatUptime(273_600)).toBe('3d 4h');
    expect(formatUptime(8_100)).toBe('2h 15m');
    expect(formatUptime(480)).toBe('8m');
    expect(formatUptime(-5)).toBe('0m');
  });
});
