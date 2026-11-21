import { useEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { PostLockFrame } from '../features/locks/lockHistory';

export type PostLockPlotProps = {
  frames: PostLockFrame[];
  height?: number;
};

const CONTROL_COLOR = '#1D9E75';
const ERROR_COLOR = '#378ADD';

const cssColor = (name: string, fallback: string): string => {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
};

// Keep a nearly constant control voltage from filling the axis with noise.
const controlRange = (_u: uPlot, min: number, max: number): uPlot.Range.MinMax => {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [-1, 1];
  const span = Math.max(max - min, 0.002);
  const mid = (min + max) / 2;
  return [mid - span * 0.6, mid + span * 0.6];
};

/** Control voltage and error spread, frame by frame, after the lock command. */
export function PostLockPlot({ frames, height = 200 }: PostLockPlotProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || frames.length === 0) return undefined;
    const text = cssColor('--mantine-color-text', '#444');
    const grid = cssColor('--mantine-color-default-border', 'rgba(128,128,128,0.25)');
    const data: uPlot.AlignedData = [
      frames.map((f) => f.t_s),
      frames.map((f) => (f.lock ? f.control_mean_v : null)),
      frames.map((f) => (f.lock && f.error_std_v !== null ? f.error_std_v * 1000 : null)),
    ];
    const plot = new uPlot(
      {
        width: Math.max(container.clientWidth, 200),
        height,
        series: [
          { label: 'Since lock command (s)' },
          { label: 'Control (V)', stroke: CONTROL_COLOR, width: 1.5, scale: 'control', points: { show: true, size: 4 } },
          { label: 'Error spread (mV)', stroke: ERROR_COLOR, width: 1.2, scale: 'error', points: { show: true, size: 3 } },
        ],
        scales: { x: { time: false }, control: { range: controlRange }, error: { range: (_u, _min, max) => [0, Math.max(max, 0.01) * 1.2] } },
        axes: [
          { stroke: text, grid: { stroke: grid, width: 0.5 }, ticks: { stroke: grid, width: 0.5 } },
          { scale: 'control', stroke: text, grid: { stroke: grid, width: 0.5 }, ticks: { stroke: grid, width: 0.5 }, size: 60 },
          { scale: 'error', side: 1, stroke: text, grid: { show: false }, ticks: { stroke: grid, width: 0.5 }, size: 50 },
        ],
        legend: { live: true },
      },
      data,
      container,
    );
    const observer = new ResizeObserver(() => {
      plot.setSize({ width: Math.max(container.clientWidth, 200), height });
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      plot.destroy();
    };
  }, [frames, height]);

  return <div ref={containerRef} style={{ width: '100%' }} />;
}
