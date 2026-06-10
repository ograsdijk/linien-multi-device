import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { PlotFrame } from '../types';
import {
  BAND_CONFIGS,
  LABELS,
  N_POINTS,
  SERIES_INDEX,
  SERIES_KEYS,
  SERIES_STYLE,
  type PlotData,
  type SeriesKey,
  type SeriesStats,
  getAccentColor,
  getAxisTheme,
  getXBuffer,
  padYRange,
  toFinite,
  toRgba,
  writeSeriesInto,
} from './plotShared';

// Thumbnail variant of PlotPanel used by DeviceOverviewCard.
//
// Differences from the full PlotPanel:
//   - cursor disabled (no mousemove redraws across 12 overview canvases)
//   - legend hidden (no DOM rows, no theme mutation observer wiring)
//   - axis values reduced to plain numbers (no us/V formatting)
//   - smaller default height
//   - no selection / autolock / optimization handlers
//   - no manual-target overlay
//   - lock-target line is still drawn (useful at-a-glance signal)
//
// Shares the buffer-reuse, y-autoscale, and buffer-alias pipeline with
// PlotPanel via plotShared so per-frame data work is identical.

const POINT_STYLE: uPlot.Series.Points = { show: false };
const DEFAULT_HEIGHT = 220;

type OverviewPlotPanelProps = {
  plotFrame?: PlotFrame | null;
  lockState?: boolean;
  // When false (or absent), defer the uPlot constructor until the
  // card becomes active+visible. Avoids the new uPlot(...) cost for
  // cards scrolled off-screen at first paint. The init effect
  // re-evaluates when this transitions true.
  initActive?: boolean;
};

export function OverviewPlotPanel({ plotFrame, lockState, initActive = true }: OverviewPlotPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const uplotRef = useRef<uPlot | null>(null);
  const sizeRef = useRef({ width: 400, height: DEFAULT_HEIGHT });
  const pointCountRef = useRef(N_POINTS);
  const lockTargetRef = useRef<number | null>(null);
  const suppressSeriesEventRef = useRef(false);
  const userShowRef = useRef<Record<number, boolean>>({});
  const [error1Visible, setError1Visible] = useState(true);
  const [error2Visible, setError2Visible] = useState(true);

  const seriesBuffersRef = useRef<Float64Array[]>(
    SERIES_KEYS.map(() => new Float64Array(N_POINTS))
  );
  const hasDataByKeyRef = useRef<Record<SeriesKey, boolean>>(
    SERIES_KEYS.reduce((acc, key) => {
      acc[key] = false;
      return acc;
    }, {} as Record<SeriesKey, boolean>)
  );
  const seriesStatsRef = useRef<SeriesStats[]>(
    SERIES_KEYS.map(() => ({ hasFinite: false, min: Infinity, max: -Infinity }))
  );
  const lastVisibilityRef = useRef<{
    lockAxis: boolean | null;
    dual: boolean | null;
    hasDataKey: string;
  }>({ lockAxis: null, dual: null, hasDataKey: '' });
  const lastAppliedXMaxRef = useRef<number | null>(null);
  const lastAppliedYRef = useRef<{ min: number; max: number } | null>(null);

  const lockAxis = typeof lockState === 'boolean' ? lockState : plotFrame?.lock ?? false;

  // Point count is purely derived from the incoming frame; refs hold
  // the last applied value for fast change-detection in the layout
  // effect.
  const series = plotFrame?.series;
  let detectedCount = 0;
  if (series) {
    for (const key of SERIES_KEYS) {
      const v = series[key];
      const len = Array.isArray(v)
        ? v.length
        : ArrayBuffer.isView(v)
        ? (v as unknown as ArrayLike<number>).length
        : 0;
      if (len > detectedCount) detectedCount = len;
    }
  }
  const pointCount = detectedCount > 0 ? detectedCount : N_POINTS;

  useEffect(() => {
    pointCountRef.current = pointCount;
  }, [pointCount]);

  useEffect(() => {
    lockTargetRef.current = toFinite(plotFrame?.lock_target);
    uplotRef.current?.redraw();
  }, [plotFrame?.lock_target]);

  // Init uPlot exactly once, the first time `initActive` becomes true.
  // Cards scrolled off-screen at first paint never construct uPlot;
  // they pay for it lazily on first visibility. Once initialized,
  // a later transition back to inactive does NOT destroy the
  // instance — re-creation on every scroll would defeat the win.
  const initializedRef = useRef(false);
  useEffect(() => {
    if (initializedRef.current || !initActive) return;
    initializedRef.current = true;
    let observer: ResizeObserver | null = null;
    const handleResize = () => {
      if (!uplotRef.current || !containerRef.current) return;
      const width = containerRef.current.clientWidth;
      if (width && width > 10) sizeRef.current.width = width;
      uplotRef.current.setSize({
        width: sizeRef.current.width,
        height: sizeRef.current.height,
      });
    };

    const container = containerRef.current;
    if (!container || uplotRef.current) return;

    const initialWidth = container.clientWidth > 10 ? container.clientWidth : sizeRef.current.width;
    sizeRef.current.width = initialWidth;

    const axisTheme = getAxisTheme();
    const makeStroke = (color: string) => () => color;

    const bandLinks = BAND_CONFIGS.map((band) => ({
      controllerIdx: SERIES_INDEX[band.controller],
      memberIdxs: [SERIES_INDEX[band.upper], SERIES_INDEX[band.lower]],
    }));

    // Force the canvas backing store to a 1:1 pixel ratio even on
    // HiDPI displays. Default behavior (devicePixelRatio = 2 or 3)
    // multiplies the pixel count 4-9x per canvas, which dominates
    // paint cost across 12 thumbnails. At thumbnail size the loss
    // of sharpness is invisible. uPlot reads `pxRatio` from opts at
    // construction time even though its TS types omit it.
    const opts: uPlot.Options = {
      width: initialWidth,
      height: sizeRef.current.height,
      ...({ pxRatio: 1 } as { pxRatio: number }),
      scales: {
        x: { time: false, auto: false, range: [0, N_POINTS - 1] },
        y: { auto: false },
      },
      // Cursor entirely disabled. With 12 overview cards we don't
      // want every mousemove to trigger uPlot's cursor draw and
      // legend value update for the hovered card; the thumbnail is
      // read-only.
      cursor: { show: false, drag: { setScale: false }, points: { show: false } },
      legend: { show: false },
      axes: [
        {
          stroke: makeStroke(axisTheme.axis),
          grid: { stroke: makeStroke(axisTheme.grid) },
          ticks: { stroke: makeStroke(axisTheme.tick) },
          // Compact tick labels: no units, two decimals. The full
          // PlotPanel formats us/V; thumbnails skip that overhead.
          values: (_u, ticks) => ticks.map((v) => v.toFixed(0)),
        },
        {
          stroke: makeStroke(axisTheme.axis),
          grid: { stroke: makeStroke(axisTheme.grid) },
          ticks: { stroke: makeStroke(axisTheme.tick) },
        },
      ],
      bands: BAND_CONFIGS.map((band) => ({
        series: [SERIES_INDEX[band.lower], SERIES_INDEX[band.upper]],
        fill: band.fill,
      })),
      series: [
        { label: 'x' },
        ...SERIES_KEYS.map((key) => {
          const style = SERIES_STYLE[key];
          const stroke = style.strokeAlpha ? toRgba(style.color, style.strokeAlpha) : style.color;
          return {
            label: LABELS[key],
            stroke,
            width: key.includes('signal_strength') ? 1 : 1.5,
            points: POINT_STYLE,
            spanGaps: true,
            show: true,
          };
        }),
      ],
      hooks: {
        setSeries: [
          (u, idx) => {
            if (idx == null) return;
            if (suppressSeriesEventRef.current) return;
            userShowRef.current[idx] = u.series[idx].show ?? false;
            const link = bandLinks.find((item) => item.controllerIdx === idx);
            if (!link) return;
            suppressSeriesEventRef.current = true;
            const show = u.series[idx].show ?? false;
            link.memberIdxs.forEach((memberIdx) => {
              u.setSeries(memberIdx, { show }, false);
            });
            suppressSeriesEventRef.current = false;
            if (idx === SERIES_INDEX.error_signal_1) setError1Visible(show);
            if (idx === SERIES_INDEX.error_signal_2) setError2Visible(show);
          },
        ],
        draw: [
          (u) => {
            const lockTarget = lockTargetRef.current;
            if (lockTarget == null) return;
            const xPos = u.valToPos(lockTarget, 'x', true);
            if (!Number.isFinite(xPos)) return;
            const { top, height } = u.bbox;
            const ctx = u.ctx;
            ctx.save();
            ctx.strokeStyle = getAccentColor();
            ctx.globalAlpha = 0.75;
            ctx.lineWidth = 1.25;
            ctx.setLineDash([6, 4]);
            ctx.beginPath();
            ctx.moveTo(xPos, top);
            ctx.lineTo(xPos, top + height);
            ctx.stroke();
            ctx.restore();
          },
        ],
      },
    };

    const initialData = [
      getXBuffer(N_POINTS),
      ...seriesBuffersRef.current,
    ] as unknown as PlotData;
    uplotRef.current = new uPlot(opts, initialData, container);
    uplotRef.current.setScale('x', { min: 0, max: N_POINTS - 1 });
    uplotRef.current.setScale('y', { min: -1, max: 1 });
    uplotRef.current.setData(initialData, false);
    handleResize();

    const applyAxisTheme = () => {
      if (!uplotRef.current) return;
      const theme = getAxisTheme();
      uplotRef.current.axes.forEach((axis) => {
        axis.stroke = makeStroke(theme.axis);
        axis.grid = { ...(axis.grid || {}), stroke: makeStroke(theme.grid) };
        axis.ticks = { ...(axis.ticks || {}), stroke: makeStroke(theme.tick) };
      });
      uplotRef.current.redraw();
    };
    applyAxisTheme();

    let schemeObserver: MutationObserver | null = null;
    if (typeof document !== 'undefined') {
      schemeObserver = new MutationObserver(() => applyAxisTheme());
      schemeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-mantine-color-scheme'],
      });
    }

    observer = new ResizeObserver(() => handleResize());
    observer.observe(container);
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      schemeObserver?.disconnect();
      observer?.disconnect();
      uplotRef.current?.destroy();
      uplotRef.current = null;
    };
  }, [initActive]);

  useLayoutEffect(() => {
    if (!uplotRef.current) return;
    const buffers = seriesBuffersRef.current;
    const hasDataByKey = hasDataByKeyRef.current;
    const stats = seriesStatsRef.current;
    const count = pointCount;

    for (let i = 0; i < buffers.length; i++) {
      if (buffers[i].length < count) {
        buffers[i] = new Float64Array(count);
      }
    }

    SERIES_KEYS.forEach((key, idx) => {
      const s = writeSeriesInto(buffers[idx], count, series?.[key]);
      hasDataByKey[key] = s.hasFinite;
      stats[idx] = s;
    });

    const combinedIdx = SERIES_KEYS.indexOf('combined_error');
    const errIdx = SERIES_KEYS.indexOf('error_signal_1');
    let aliasCombinedToErr1 = false;
    if (!lockAxis && hasDataByKey.error_signal_1) {
      hasDataByKey.combined_error = true;
      stats[combinedIdx] = stats[errIdx];
      aliasCombinedToErr1 = true;
    }

    const x = getXBuffer(count);
    const plotData = [x, ...buffers] as unknown as PlotData;
    if (aliasCombinedToErr1) {
      plotData[combinedIdx + 1] = buffers[errIdx];
    }

    const dual = Boolean(plotFrame?.dual_channel);
    const desiredVisibility: Record<SeriesKey, boolean> = {
      combined_error: false,
      control_signal: false,
      control_signal_history: false,
      slow_history: false,
      monitor_signal_history: false,
      error_signal_1: false,
      error_signal_2: false,
      monitor_signal: false,
      signal_strength_a_upper: false,
      signal_strength_a_lower: false,
      signal_strength_b_upper: false,
      signal_strength_b_lower: false,
    };

    if (lockAxis) {
      desiredVisibility.combined_error = true;
      desiredVisibility.control_signal = true;
    } else {
      desiredVisibility.combined_error = !dual;
      desiredVisibility.error_signal_1 = dual;
      desiredVisibility.error_signal_2 = dual;
      desiredVisibility.monitor_signal = true;
      desiredVisibility.signal_strength_a_upper = true;
      desiredVisibility.signal_strength_a_lower = true;
      desiredVisibility.signal_strength_b_upper = true;
      desiredVisibility.signal_strength_b_lower = true;
    }

    let hasDataKey = '';
    for (const key of SERIES_KEYS) hasDataKey += hasDataByKey[key] ? '1' : '0';
    const last = lastVisibilityRef.current;
    const visibilityChanged =
      last.lockAxis !== lockAxis ||
      last.dual !== dual ||
      last.hasDataKey !== hasDataKey;

    // y autoscale from per-series stats (only over visible series).
    let yMin = Infinity;
    let yMax = -Infinity;
    SERIES_KEYS.forEach((key, idx) => {
      if (!desiredVisibility[key] || !hasDataByKey[key]) return;
      if (key === 'signal_strength_a_upper' || key === 'signal_strength_a_lower') {
        if (!error1Visible) return;
      }
      if (key === 'signal_strength_b_upper' || key === 'signal_strength_b_lower') {
        if (!error2Visible) return;
      }
      const s = stats[idx];
      if (!s.hasFinite) return;
      if (s.min < yMin) yMin = s.min;
      if (s.max > yMax) yMax = s.max;
    });
    const padded = padYRange(yMin, yMax);
    const lastY = lastAppliedYRef.current;
    const ySpan = Math.max(1e-9, padded.yMax - padded.yMin);
    const yChanged =
      lastY === null ||
      Math.abs(lastY.min - padded.yMin) > ySpan * 0.005 ||
      Math.abs(lastY.max - padded.yMax) > ySpan * 0.005;
    const xChanged = lastAppliedXMaxRef.current !== count;

    uplotRef.current.batch((u: uPlot) => {
      if (visibilityChanged) {
        suppressSeriesEventRef.current = true;
        SERIES_KEYS.forEach((key, idx) => {
          const seriesIdx = idx + 1;
          let show = desiredVisibility[key] && hasDataByKey[key];
          if (show) {
            show = userShowRef.current[seriesIdx] ?? true;
          }
          if (key === 'signal_strength_a_upper' || key === 'signal_strength_a_lower') {
            show = desiredVisibility[key] && hasDataByKey[key] && error1Visible;
          }
          if (key === 'signal_strength_b_upper' || key === 'signal_strength_b_lower') {
            show = desiredVisibility[key] && hasDataByKey[key] && error2Visible;
          }
          u.setSeries(seriesIdx, { show }, false);
        });
        suppressSeriesEventRef.current = false;
        lastVisibilityRef.current = { lockAxis, dual, hasDataKey };
      }
      u.setData(plotData, false);
      if (xChanged) {
        u.setScale('x', { min: 0, max: Math.max(count - 1, 1) });
        lastAppliedXMaxRef.current = count;
      }
      if (yChanged) {
        u.setScale('y', { min: padded.yMin, max: padded.yMax });
        lastAppliedYRef.current = { min: padded.yMin, max: padded.yMax };
      }
    });
  }, [plotFrame, pointCount, lockAxis, error1Visible, error2Visible, series]);

  return (
    <div className="panel plot-panel" style={{ padding: 8 }}>
      {(!plotFrame || !plotFrame.series || Object.keys(plotFrame.series).length === 0) && (
        <div style={{ color: '#7a6a58', fontSize: 12, marginBottom: 6 }}>
          Waiting for data...
        </div>
      )}
      <div
        ref={containerRef}
        style={{ width: '100%', height: DEFAULT_HEIGHT, position: 'relative' }}
      />
    </div>
  );
}
