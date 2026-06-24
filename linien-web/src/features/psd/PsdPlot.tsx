import { memo, useEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { PsdCurveEntry } from './usePsdController';
import { formatLogTick } from './psdMath';

type PsdPlotProps = {
  curves: PsdCurveEntry[];
  height?: number;
  fLo?: number;
  fHi?: number;
};

const cssVar = (name: string, fallback: string): string => {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
};

// Build uPlot-aligned data: a shared, sorted union frequency axis plus one
// per-curve y-array with nulls where that curve has no sample at a given
// frequency. spanGaps then draws each curve as a continuous line through its
// own points even though the union axis is sparse for it.
const buildData = (curves: PsdCurveEntry[]): uPlot.AlignedData => {
  const freqSet = new Set<number>();
  for (const c of curves) {
    for (const p of c.curve) freqSet.add(p.f);
  }
  const xs = Array.from(freqSet).sort((a, b) => a - b);
  const xIndex = new Map(xs.map((f, i) => [f, i]));
  const series = curves.map((c) => {
    const ys: (number | null)[] = new Array(xs.length).fill(null);
    for (const p of c.curve) {
      const idx = xIndex.get(p.f);
      if (idx !== undefined) ys[idx] = p.psd;
    }
    return ys;
  });
  return [xs, ...series] as uPlot.AlignedData;
};

// Signature of the chart *structure* (which curves, what colour). When this
// changes the uPlot instance is rebuilt; otherwise we only push new data and
// toggle series visibility in place.
const structureSignature = (curves: PsdCurveEntry[]): string =>
  curves.map((c) => `${c.uuid}:${c.color}`).join('|');

const labelFor = (c: PsdCurveEntry): string => {
  const pid = `P${c.p ?? '?'} I${c.i ?? '?'} D${c.d ?? '?'}`;
  return `${c.device_key} · ${pid}`;
};

export const PsdPlot = memo(function PsdPlot({
  curves,
  height = 460,
  fLo,
  fHi,
}: PsdPlotProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const uplotRef = useRef<uPlot | null>(null);
  const sigRef = useRef<string>('');
  const observerRef = useRef<ResizeObserver | null>(null);
  const bandRef = useRef<{ lo?: number; hi?: number }>({ lo: fLo, hi: fHi });
  bandRef.current = { lo: fLo, hi: fHi };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      // Empty state renders a placeholder instead of the container; tear down
      // any existing instance so a later rebuild starts clean (avoids pushing
      // data into a uPlot whose DOM has been detached).
      if (uplotRef.current) {
        uplotRef.current.destroy();
        uplotRef.current = null;
        sigRef.current = '';
      }
      return;
    }

    const sig = structureSignature(curves);
    const data = buildData(curves);

    const needsRebuild = uplotRef.current === null || sig !== sigRef.current;

    if (needsRebuild) {
      if (uplotRef.current) {
        uplotRef.current.destroy();
        uplotRef.current = null;
      }
      sigRef.current = sig;

      const axisColor = cssVar('--mantine-color-dimmed', '#868e96');
      const gridColor = cssVar('--mantine-color-default-border', 'rgba(128,128,128,0.2)');
      const width = container.clientWidth > 10 ? container.clientWidth : 600;

      const opts: uPlot.Options = {
        width,
        height,
        scales: {
          x: { distr: 3, time: false },
          y: { distr: 3 },
        },
        // The PsdTable below is the legend (color swatch + device + P/I/D + RMS
        // + show/hide), so the built-in uPlot legend is redundant and, because
        // it renders outside the fixed-height container, would overlap the table.
        legend: { show: false },
        axes: [
          {
            label: 'Frequency (Hz)',
            stroke: () => axisColor,
            grid: { stroke: () => gridColor },
            ticks: { stroke: () => gridColor },
            values: (_u, splits) => splits.map(formatLogTick),
          },
          {
            label: 'PSD (V / Sqrt[Hz])',
            stroke: () => axisColor,
            grid: { stroke: () => gridColor },
            ticks: { stroke: () => gridColor },
            values: (_u, splits) => splits.map(formatLogTick),
          },
        ],
        series: [
          { label: 'Frequency' },
          ...curves.map((c) => ({
            label: labelFor(c),
            stroke: c.color,
            width: 1.5,
            spanGaps: true,
            show: c.visible,
            points: { show: false },
          })),
        ],
        hooks: {
          // Shade the [f_lo, f_hi] band of interest behind the curves.
          drawClear: [
            (u: uPlot) => {
              const { lo, hi } = bandRef.current;
              if (lo == null || hi == null || !(hi > lo)) return;
              const xLo = u.valToPos(lo, 'x', true);
              const xHi = u.valToPos(hi, 'x', true);
              if (!Number.isFinite(xLo) || !Number.isFinite(xHi)) return;
              const { ctx } = u;
              ctx.save();
              ctx.fillStyle = 'rgba(120,144,200,0.10)';
              ctx.fillRect(xLo, u.bbox.top, xHi - xLo, u.bbox.height);
              ctx.restore();
            },
          ],
        },
      };

      uplotRef.current = new uPlot(opts, data, container);
    } else {
      const u = uplotRef.current!;
      u.setData(data);
      // Apply visibility toggles in place (series index is curve index + 1).
      curves.forEach((c, idx) => {
        const seriesIdx = idx + 1;
        if (u.series[seriesIdx] && (u.series[seriesIdx].show ?? true) !== c.visible) {
          u.setSeries(seriesIdx, { show: c.visible });
        }
      });
    }
  }, [curves, height]);

  // Redraw the band shading when [f_lo, f_hi] changes (no structure rebuild).
  useEffect(() => {
    uplotRef.current?.redraw();
  }, [fLo, fHi]);

  // Keep the canvas sized to its container.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => {
      const u = uplotRef.current;
      if (!u) return;
      const width = container.clientWidth;
      if (width > 10) u.setSize({ width, height });
    });
    observer.observe(container);
    observerRef.current = observer;
    return () => {
      observer.disconnect();
      observerRef.current = null;
    };
  }, [height]);

  useEffect(
    () => () => {
      if (uplotRef.current) {
        uplotRef.current.destroy();
        uplotRef.current = null;
      }
    },
    []
  );

  if (curves.length === 0) {
    return (
      <div
        style={{
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--mantine-color-dimmed)',
          border: '1px dashed var(--mantine-color-default-border)',
          borderRadius: 8,
        }}
      >
        No PSD measurements yet — start one from a locked device above.
      </div>
    );
  }

  return <div ref={containerRef} style={{ width: '100%', height }} />;
});
