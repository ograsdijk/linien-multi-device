import { useEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';

type Series = Array<number | null>;

export type LockTracePlotProps = {
  x: Series;
  error: Series;
  monitor?: Series | null;
  monitorLabel?: string;
  // The board's current trace on the same sweep axis, for comparison.
  live?: { x: Series; y: Series } | null;
  zeroCrossingV?: number | null;
  height?: number;
};

const ERROR_COLOR = '#378ADD';
const MONITOR_COLOR = '#888780';
const LIVE_COLOR = '#D85A30';
const MARKER_COLOR = '#D85A30';

// A near-flat trace (a monitor sitting a few ADC steps from zero) would
// otherwise fill the whole axis with quantization steps.
const MIN_SPAN_V = 0.01;

const paddedRange = (_u: uPlot, min: number, max: number): uPlot.Range.MinMax => {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [-1, 1];
  const span = max - min;
  if (span < MIN_SPAN_V) {
    const mid = (min + max) / 2;
    return [mid - MIN_SPAN_V / 2, mid + MIN_SPAN_V / 2];
  }
  return [min - span * 0.05, max + span * 0.05];
};

const cssColor = (name: string, fallback: string): string => {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
};

/**
 * Merge the recorded and live traces onto one sorted x axis (uPlot needs a
 * single x array); each series is null where it has no sample.
 */
const alignSeries = (
  x: Series,
  ys: Series[],
  live: { x: Series; y: Series } | null | undefined,
): [number[], ...Series[]] => {
  if (!live) {
    return [x.map((v) => (v ?? NaN) as number), ...ys];
  }
  const points: Array<{ x: number; values: Array<number | null> }> = [];
  x.forEach((xv, i) => {
    if (xv !== null) points.push({ x: xv, values: [...ys.map((y) => y[i] ?? null), null] });
  });
  live.x.forEach((xv, i) => {
    if (xv !== null) {
      points.push({ x: xv, values: [...ys.map(() => null), live.y[i] ?? null] });
    }
  });
  points.sort((a, b) => a.x - b.x);
  const columns = ys.length + 1;
  return [
    points.map((p) => p.x),
    ...Array.from({ length: columns }, (_, c) => points.map((p) => p.values[c])),
  ];
};

export function LockTracePlot({
  x,
  error,
  monitor,
  monitorLabel = 'Monitor',
  live,
  zeroCrossingV,
  height = 260,
}: LockTracePlotProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const text = cssColor('--mantine-color-text', '#444');
    const grid = cssColor('--mantine-color-default-border', 'rgba(128,128,128,0.25)');
    const hasMonitor = Boolean(monitor && monitor.some((v) => v !== null));

    const ys: Series[] = hasMonitor ? [error, monitor as Series] : [error];
    const data = alignSeries(x, ys, live) as uPlot.AlignedData;

    const series: uPlot.Series[] = [
      { label: 'Sweep (V)' },
      { label: 'Error (V)', stroke: ERROR_COLOR, width: 1.5, spanGaps: true, scale: 'y' },
    ];
    if (hasMonitor) {
      series.push({
        label: `${monitorLabel} (V)`,
        stroke: MONITOR_COLOR,
        width: 1,
        spanGaps: true,
        scale: 'mon',
      });
    }
    if (live) {
      series.push({
        label: 'Now (V)',
        stroke: LIVE_COLOR,
        width: 1.2,
        spanGaps: true,
        scale: 'y',
      });
    }

    const axis = (scale: string, side?: number): uPlot.Axis => ({
      scale,
      side,
      stroke: text,
      grid: { stroke: grid, width: 0.5, show: side === undefined },
      ticks: { stroke: grid, width: 0.5 },
      size: 56,
    });

    const plot = new uPlot(
      {
        width: Math.max(container.clientWidth, 200),
        height,
        series,
        scales: { x: { time: false }, y: { range: paddedRange }, mon: { range: paddedRange } },
        axes: [
          { stroke: text, grid: { stroke: grid, width: 0.5 }, ticks: { stroke: grid, width: 0.5 } },
          axis('y'),
          ...(hasMonitor ? [axis('mon', 1)] : []),
        ],
        cursor: { drag: { x: true, y: false } },
        legend: { live: true },
        hooks: {
          draw: [
            (u) => {
              if (zeroCrossingV === null || zeroCrossingV === undefined) return;
              const px = u.valToPos(zeroCrossingV, 'x', true);
              if (!Number.isFinite(px)) return;
              const ctx = u.ctx;
              ctx.save();
              ctx.strokeStyle = MARKER_COLOR;
              ctx.lineWidth = 1 * devicePixelRatio;
              ctx.setLineDash([4 * devicePixelRatio, 4 * devicePixelRatio]);
              ctx.beginPath();
              ctx.moveTo(px, u.bbox.top);
              ctx.lineTo(px, u.bbox.top + u.bbox.height);
              ctx.stroke();
              ctx.restore();
            },
          ],
        },
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
  }, [x, error, monitor, monitorLabel, live, zeroCrossingV, height]);

  return <div ref={containerRef} style={{ width: '100%' }} />;
}
