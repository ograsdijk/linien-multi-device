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

  it('shows the 5 V supply between disk and uptime', () => {
    const withSupply = status({
      ...healthy,
      rp_metrics: { ...healthy.rp_metrics, supply_voltage_v: 4.982 },
    });
    expect(text(resolveHostMetrics(withSupply, 5))).toEqual([
      'CPU 4%',
      'RAM 41%',
      'disk 1.1 GB',
      '5V 4.98 V',
      'up 3d 4h',
    ]);
  });

  it('omits the supply for a daemon that does not report it', () => {
    const noSupply = status({ rp_metrics: { cpu_percent: 4, supply_voltage_v: null } });
    expect(resolveHostMetrics(noSupply, 5).map((s) => s.id)).toEqual(['cpu']);
    expect(resolveHostMetrics(healthy, 5).map((s) => s.id)).not.toContain('supply');
  });

  it('does not judge the supply voltage', () => {
    // No threshold yet: the number is for comparing boards.
    for (const volts of [4.5, 5.0, 5.4]) {
      const board = status({ rp_metrics: { supply_voltage_v: volts } });
      expect(toneOf(resolveHostMetrics(board, 5), 'supply')).toBe('normal');
    }
  });

  it('says the supply reading cannot capture brief droops', () => {
    const [supply] = resolveHostMetrics(
      status({ rp_metrics: { supply_voltage_v: 5 } }),
      5
    );
    expect(supply.title).toMatch(/brief droops are not captured/);
  });

  it('withdraws the supply with the rest once stale', () => {
    const board = status({ rp_metrics: { supply_voltage_v: 5 } });
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
