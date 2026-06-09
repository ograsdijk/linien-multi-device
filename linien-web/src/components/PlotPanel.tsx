import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { PlotFrame } from '../types';
import {
  ADC_SAMPLE_RATE,
  BAND_CONFIGS,
  DECIMATION,
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
  seriesArrayLength,
  toFinite,
  toRgba,
  writeSeriesInto,
} from './plotShared';

type SelectionMode = 'autolock' | 'optimization' | null;

type PlotPanelProps = {
  plotFrame?: PlotFrame | null;
  selectionMode: SelectionMode;
  onSelectRange?: (x0: number, x1: number) => void | Promise<void>;
  lockState?: boolean;
  sweepCenter?: number;
  sweepAmplitude?: number;
  showManualTarget?: boolean;
};

const POINT_STYLE: uPlot.Series.Points = { show: false };

const getSelectionWidth = (mode: SelectionMode) => (mode === 'optimization' ? 0.75 : 0.99);

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

const getSelectionBounds = (mode: Exclude<SelectionMode, null>, pointCount: number) => {
  const selectableWidth = getSelectionWidth(mode);
  const maxIndex = Math.max(pointCount - 1, 1);
  const boundary = ((maxIndex + 1) * (1 - selectableWidth)) / 2;
  return {
    minVal: boundary,
    maxVal: Math.max(boundary, maxIndex - boundary),
  };
};

const getSelectionThreshold = (bounds: { minVal: number; maxVal: number }) =>
  Math.max(1, (bounds.maxVal - bounds.minVal) * 0.01);

export function PlotPanel({
  plotFrame,
  selectionMode,
  onSelectRange,
  lockState,
  sweepCenter,
  sweepAmplitude,
  showManualTarget,
}: PlotPanelProps) {
  const [frozenFrame, setFrozenFrame] = useState<PlotFrame | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const uplotRef = useRef<uPlot | null>(null);
  const sizeRef = useRef({ width: 800, height: 420 });
  const userShowRef = useRef<Record<number, boolean>>({});
  const suppressSeriesEventRef = useRef(false);
  const pointCountRef = useRef(N_POINTS);
  const manualTargetRef = useRef<{ enabled: boolean; xVal: number | null }>({
    enabled: false,
    xVal: null,
  });
  const lockTargetRef = useRef<number | null>(null);
  const selectionRef = useRef<{
    mode: SelectionMode;
    onSelectRange?: PlotPanelProps['onSelectRange'];
  }>({ mode: selectionMode, onSelectRange });

  // Per-PlotPanel reusable typed-array buffers for series data. Allocated
  // once at N_POINTS and grown only if a frame ever exceeds that count
  // (effectively never in practice). The same Float64Array is handed to
  // uPlot every frame, mutated in place by `writeSeriesInto`; uPlot reads
  // it on the next batch+setData call.
  const seriesBuffersRef = useRef<Float64Array[]>(
    SERIES_KEYS.map(() => new Float64Array(N_POINTS))
  );
  const hasDataByKeyRef = useRef<Record<SeriesKey, boolean>>(
    SERIES_KEYS.reduce((acc, key) => {
      acc[key] = false;
      return acc;
    }, {} as Record<SeriesKey, boolean>)
  );
  // Per-series finite-stats from the most recent writeSeriesInto pass.
  // Aggregated over the visible-series subset to drive the explicit
  // y-axis setScale, avoiding uPlot's own all-series rescale sweep.
  const seriesStatsRef = useRef<SeriesStats[]>(
    SERIES_KEYS.map(() => ({ hasFinite: false, min: Infinity, max: -Infinity }))
  );
  // Track the last visibility configuration applied to uPlot so we can
  // skip the per-frame setSeries loop and legend DOM walk when nothing
  // visibility-related actually changed (the common case during steady
  // streaming).
  const lastVisibilityRef = useRef<{
    lockAxis: boolean | null;
    dual: boolean | null;
    hasDataKey: string;
  }>({ lockAxis: null, dual: null, hasDataKey: '' });
  // Track the x scale we last pushed to uPlot. Skipping setScale('x')
  // when the point count hasn't moved avoids a redundant scale update
  // per frame (the common case — pointCount is virtually always
  // N_POINTS).
  const lastAppliedXMaxRef = useRef<number | null>(null);
  // Track the y scale we last pushed so we can skip setScale('y') when
  // the rounded range hasn't changed enough to be visible. Avoids
  // micro-redraws as the signal jitters.
  const lastAppliedYRef = useRef<{ min: number; max: number } | null>(null);

  useEffect(() => {
    if (selectionMode === null) {
      setFrozenFrame(null);
      return;
    }
    setFrozenFrame((current) => current ?? plotFrame ?? null);
  }, [selectionMode, plotFrame]);

  const activePlotFrame = selectionMode === null ? plotFrame ?? null : frozenFrame ?? plotFrame ?? null;
  const lockAxis = typeof lockState === 'boolean' ? lockState : activePlotFrame?.lock ?? false;
  const sweepCenterValue = toFinite(sweepCenter) ?? 0;
  const sweepAmplitudeValue = toFinite(sweepAmplitude) ?? 1;

  // Compute point count as a pure derivation. Buffer writes happen in
  // the commit-phase effect below so we do not mutate refs during render
  // (which is unsafe under React 18 concurrent rendering / StrictMode
  // double-invocation, where a render may be discarded and the next
  // render would observe ref state inconsistent with the data uPlot
  // actually has).
  const pointCount = useMemo(() => {
    const series = activePlotFrame?.series;
    let maxLen = 0;
    for (const key of SERIES_KEYS) {
      const len = seriesArrayLength(series?.[key]);
      if (len > maxLen) maxLen = len;
    }
    return maxLen > 0 ? maxLen : N_POINTS;
  }, [activePlotFrame]);

  useEffect(() => {
    pointCountRef.current = pointCount;
  }, [pointCount]);

  // Hold the inputs that the axis/cursor formatters depend on in refs so
  // the formatters themselves can be stable across renders. Without this,
  // every sweep-center adjustment OR per-frame `pointCount` change would
  // invalidate the formatters and trigger an extra
  // `u.redraw(false, true)` outside the data update path.
  const axisInputsRef = useRef({
    lockAxis,
    sweepCenterValue,
    sweepAmplitudeValue,
    pointCount,
  });
  useEffect(() => {
    axisInputsRef.current = {
      lockAxis,
      sweepCenterValue,
      sweepAmplitudeValue,
      pointCount,
    };
  }, [lockAxis, sweepCenterValue, sweepAmplitudeValue, pointCount]);

  const axisValues = useMemo(() => {
    const dtMicroSeconds = (DECIMATION / ADC_SAMPLE_RATE) * 1e6;
    return (_u: uPlot, ticks: number[]) => {
      const inputs = axisInputsRef.current;
      if (inputs.lockAxis) {
        return ticks.map((v) => (v * dtMicroSeconds).toFixed(1));
      }
      const min = inputs.sweepCenterValue - inputs.sweepAmplitudeValue;
      const max = inputs.sweepCenterValue + inputs.sweepAmplitudeValue;
      const spacing = (max - min) / (Math.max(inputs.pointCount, 2) - 1);
      return ticks.map((v) => (min + v * spacing).toFixed(2));
    };
  }, []);

  const xValueFormatter = useMemo(() => {
    const dtMicroSeconds = (DECIMATION / ADC_SAMPLE_RATE) * 1e6;
    return (_u: uPlot, val: number) => {
      if (!Number.isFinite(val)) return '';
      const inputs = axisInputsRef.current;
      if (inputs.lockAxis) {
        return `${(val * dtMicroSeconds).toFixed(2)} us`;
      }
      const min = inputs.sweepCenterValue - inputs.sweepAmplitudeValue;
      const max = inputs.sweepCenterValue + inputs.sweepAmplitudeValue;
      const spacing = (max - min) / (Math.max(inputs.pointCount, 2) - 1);
      return `${(min + val * spacing).toFixed(3)} V`;
    };
  }, []);

  const axisLabel = lockAxis ? 'time (us)' : 'sweep voltage (V)';

  useEffect(() => {
    selectionRef.current = { mode: selectionMode, onSelectRange };
  }, [selectionMode, onSelectRange]);

  useEffect(() => {
    if (!showManualTarget || lockAxis) {
      manualTargetRef.current = { enabled: false, xVal: null };
      uplotRef.current?.redraw();
      return;
    }
    const min = sweepCenterValue - sweepAmplitudeValue;
    const max = sweepCenterValue + sweepAmplitudeValue;
    const spacing = (max - min) / (Math.max(pointCount, 2) - 1);
    const targetVoltage = Math.max(min, Math.min(max, sweepCenterValue));
    const xVal = Number.isFinite(spacing) && spacing !== 0 ? (targetVoltage - min) / spacing : 0;
    manualTargetRef.current = { enabled: true, xVal };
    uplotRef.current?.redraw();
  }, [showManualTarget, lockAxis, sweepCenterValue, sweepAmplitudeValue, pointCount]);

  useEffect(() => {
    lockTargetRef.current = toFinite(activePlotFrame?.lock_target);
    uplotRef.current?.redraw();
  }, [activePlotFrame?.lock_target]);

  useEffect(() => {
    let observer: ResizeObserver | null = null;
    const handleResize = () => {
      if (!uplotRef.current || !containerRef.current) return;
      const width = containerRef.current.clientWidth;
      if (width && width > 10) {
        sizeRef.current.width = width;
      }
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

    const opts: uPlot.Options = {
      width: initialWidth,
      height: sizeRef.current.height,
      scales: {
        // Both scales `auto: false` so uPlot never sweeps the
        // visible-series points looking for min/max on each setData.
        // The per-frame layout effect computes y range from
        // writeSeriesInto's stats (already iterating the data) and
        // sets x explicitly only when the point count changes.
        x: { time: false, auto: false, range: [0, N_POINTS - 1] },
        y: { auto: false },
      },
      cursor: {
        drag: { setScale: false },
      },
      select: { show: true, left: 0, top: 0, width: 0, height: 0 },
      axes: [
        {
          label: axisLabel,
          values: axisValues,
          stroke: makeStroke(axisTheme.axis),
          grid: { stroke: makeStroke(axisTheme.grid) },
          ticks: { stroke: makeStroke(axisTheme.tick) },
        },
        {
          label: 'signal (V)',
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
        { label: 'x', value: xValueFormatter },
        ...SERIES_KEYS.map((key) => {
          const style = SERIES_STYLE[key];
          const stroke = style.strokeAlpha ? toRgba(style.color, style.strokeAlpha) : style.color;
          return {
            label: LABELS[key],
            stroke,
            width: key.includes('signal_strength') ? 1 : 2,
            points: POINT_STYLE,
            spanGaps: true,
            class: style.legendHidden ? 'legend-hidden' : undefined,
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
          },
        ],
        draw: [
          (u) => {
            const { top, height } = u.bbox;
            const ctx = u.ctx;
            const theme = getAxisTheme();

            const manualTarget = manualTargetRef.current;
            if (manualTarget.enabled && manualTarget.xVal != null) {
              const xPos = u.valToPos(manualTarget.xVal, 'x', true);
              if (Number.isFinite(xPos)) {
                ctx.save();
                ctx.strokeStyle = theme.axis;
                ctx.globalAlpha = 0.55;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(xPos, top);
                ctx.lineTo(xPos, top + height);
                ctx.stroke();
                ctx.restore();
              }
            }

            const lockTarget = lockTargetRef.current;
            if (lockTarget != null) {
              const xPos = u.valToPos(lockTarget, 'x', true);
              if (Number.isFinite(xPos)) {
                ctx.save();
                ctx.strokeStyle = getAccentColor();
                ctx.globalAlpha = 0.8;
                ctx.lineWidth = 1.5;
                ctx.setLineDash([7, 5]);
                ctx.beginPath();
                ctx.moveTo(xPos, top);
                ctx.lineTo(xPos, top + height);
                ctx.stroke();
                ctx.restore();
              }
            }
          },
        ],
        setSelect: [
          (u) => {
            const current = selectionRef.current;
            if (!current.mode || !current.onSelectRange) return;
            const { left, width } = u.select;
            if (width < 2) {
              u.setSelect({ left: 0, width: 0, height: 0, top: 0 }, false);
              return;
            }

            const bounds = getSelectionBounds(current.mode, pointCountRef.current);
            const minPos = u.valToPos(bounds.minVal, 'x');
            const maxPos = u.valToPos(bounds.maxVal, 'x');
            const xStart = clamp(left, Math.min(minPos, maxPos), Math.max(minPos, maxPos));
            const xEnd = clamp(
              left + width,
              Math.min(minPos, maxPos),
              Math.max(minPos, maxPos)
            );
            if (Math.abs(xEnd - xStart) < 2) {
              u.setSelect({ left: 0, width: 0, height: 0, top: 0 }, false);
              return;
            }

            const x0 = u.posToVal(xStart, 'x');
            const x1 = u.posToVal(xEnd, 'x');
            if (Math.abs(x1 - x0) < getSelectionThreshold(bounds)) {
              u.setSelect({ left: 0, width: 0, height: 0, top: 0 }, false);
              return;
            }

            Promise.resolve(
              current.onSelectRange(Math.min(Math.round(x0), Math.round(x1)), Math.max(Math.round(x0), Math.round(x1)))
            ).catch(() => null);
            u.setSelect({ left: 0, width: 0, height: 0, top: 0 }, false);
          },
        ],
      },
    };

    // Initial data: the existing typed-array buffers (filled with NaN
    // until the first frame arrives). The layout effect that owns the
    // data path will call setData with the real frame contents on its
    // first run.
    const initialData = [
      getXBuffer(N_POINTS),
      ...seriesBuffersRef.current,
    ] as unknown as PlotData;
    uplotRef.current = new uPlot(opts, initialData, container);
    // Seed both scales explicitly. With auto:false on both, uPlot
    // won't recompute them during setData calls — we drive them from
    // the per-frame layout effect.
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
  }, []);

  // Single commit-phase effect that owns:
  //  1. Filling the reusable typed-array series buffers from the latest
  //     plot frame.
  //  2. Recomputing `hasDataByKey` and applying visibility/legend
  //     changes (only when actually changed).
  //  3. Calling uPlot setData/setScale.
  //
  // Doing this in `useLayoutEffect` (not `useMemo` during render)
  // ensures we never mutate refs in a render that React may discard
  // under concurrent rendering. uPlot then sees state consistent with
  // what we actually committed.
  useLayoutEffect(() => {
    if (!uplotRef.current) return;
    const buffers = seriesBuffersRef.current;
    const hasDataByKey = hasDataByKeyRef.current;
    const series = activePlotFrame?.series;
    const count = pointCount;

    // Grow buffers only if a longer frame arrives. Capacity stays
    // monotonic so a single jumbo frame doesn't permanently inflate
    // memory across all PlotPanels (each panel only grows its own).
    for (let i = 0; i < buffers.length; i++) {
      if (buffers[i].length < count) {
        buffers[i] = new Float64Array(count);
      }
    }

    const stats = seriesStatsRef.current;
    SERIES_KEYS.forEach((key, idx) => {
      const s = writeSeriesInto(buffers[idx], count, series?.[key]);
      hasDataByKey[key] = s.hasFinite;
      stats[idx] = s;
    });

    // When the time axis is in sweep-voltage mode, alias combined_error
    // to error_signal_1 if the latter has data. Previously we copied
    // 2048 floats per frame; uPlot accepts shared buffer refs across
    // series, so we just point the plotData slot at the same buffer
    // and copy the per-series stats too.
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
      // plotData is a fresh array per frame (new spread); mutating the
      // alias slot here does not mutate seriesBuffersRef.current.
      plotData[combinedIdx + 1] = buffers[errIdx];
    }

    const dual = Boolean(activePlotFrame?.dual_channel);
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
      desiredVisibility.control_signal_history = true;
      desiredVisibility.slow_history = true;
      desiredVisibility.monitor_signal_history = true;
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

    const error1Idx = SERIES_INDEX.error_signal_1;
    const error2Idx = SERIES_INDEX.error_signal_2;
    const error1Visible =
      desiredVisibility.error_signal_1 &&
      hasDataByKey.error_signal_1 &&
      (userShowRef.current[error1Idx] ?? true);
    const error2Visible =
      desiredVisibility.error_signal_2 &&
      hasDataByKey.error_signal_2 &&
      (userShowRef.current[error2Idx] ?? true);

    // Fingerprint the visibility-affecting inputs so we can short-circuit
    // the per-frame setSeries loop and legend DOM walk when only the
    // series numeric contents changed.
    let hasDataKey = '';
    for (const key of SERIES_KEYS) {
      hasDataKey += hasDataByKey[key] ? '1' : '0';
    }
    const last = lastVisibilityRef.current;
    const visibilityChanged =
      last.lockAxis !== lockAxis ||
      last.dual !== dual ||
      last.hasDataKey !== hasDataKey;

    // Aggregate y range over only the series that will actually be
    // drawn. Doing it from the per-series stats we already collected
    // in writeSeriesInto (one pass over the data) replaces uPlot's
    // built-in scale-recompute, which would otherwise sweep every
    // visible series's points again on every setData call.
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
    // Only push y scale when range moves perceptibly (>0.5% of the
    // current span). Tiny ADC jitter would otherwise trigger a full
    // canvas redraw on every frame.
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
      }
      // setData(false) skips uPlot's internal autoscale; we drive
      // scales explicitly below.
      u.setData(plotData, false);
      if (xChanged) {
        u.setScale('x', { min: 0, max: Math.max(count - 1, 1) });
        lastAppliedXMaxRef.current = count;
      }
      if (yChanged) {
        u.setScale('y', { min: padded.yMin, max: padded.yMax });
        lastAppliedYRef.current = { min: padded.yMin, max: padded.yMax };
      }
      if (visibilityChanged) {
        const rows = u.root.querySelectorAll<HTMLElement>('.u-legend .u-series');
        rows.forEach((row: HTMLElement, rowIdx: number) => {
          if (rowIdx === 0) return;
          const key = SERIES_KEYS[rowIdx - 1];
          if (!key) return;
          const hide =
            SERIES_STYLE[key].legendHidden || !desiredVisibility[key] || !hasDataByKey[key];
          row.classList.toggle('legend-hidden', hide);
        });
        lastVisibilityRef.current = { lockAxis, dual, hasDataKey };
      }
    });
  }, [activePlotFrame, pointCount, lockAxis]);

  useEffect(() => {
    if (!uplotRef.current) return;
    const u = uplotRef.current;
    u.axes[0].label = axisLabel;
    // axisValues / xValueFormatter are stable references that read their
    // numeric inputs from a ref, so we only need to wire them once and
    // then redraw when the human-readable axis label actually changes.
    u.axes[0].values = axisValues;
    u.series[0].value = xValueFormatter;
    u.redraw(false, true);
  }, [axisLabel, axisValues, xValueFormatter]);

  useEffect(() => {
    if (!uplotRef.current) return;
    uplotRef.current.select.show = selectionMode !== null;
    uplotRef.current.redraw();
  }, [selectionMode]);

  useEffect(() => {
    const u = uplotRef.current;
    if (!u || selectionMode === null) {
      return;
    }
    const over = u.root.querySelector('.u-over') as HTMLElement | null;
    if (!over) {
      return;
    }

    const previousTouchAction = over.style.touchAction;
    const previousCursor = over.style.cursor;
    over.style.touchAction = 'none';
    over.style.cursor = 'crosshair';

    const bounds = getSelectionBounds(selectionMode, pointCount);
    const minPos = u.valToPos(bounds.minVal, 'x');
    const maxPos = u.valToPos(bounds.maxVal, 'x');
    const lowerPos = Math.min(minPos, maxPos);
    const upperPos = Math.max(minPos, maxPos);
    const threshold = getSelectionThreshold(bounds);

    let startX: number | null = null;
    let currentX: number | null = null;

    const clearSelection = () => {
      u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
    };

    const clampToPlotX = (clientX: number) => {
      const rect = over.getBoundingClientRect();
      const raw = clientX - rect.left;
      return clamp(raw, lowerPos, upperPos);
    };

    const drawSelection = (x0: number, x1: number) => {
      const left = Math.min(x0, x1);
      const width = Math.abs(x1 - x0);
      u.setSelect(
        {
          left,
          width,
          top: 0,
          height: over.clientHeight,
        },
        false
      );
    };

    const finishSelection = () => {
      if (startX == null || currentX == null) {
        clearSelection();
        startX = null;
        currentX = null;
        return;
      }
      const left = Math.min(startX, currentX);
      const right = Math.max(startX, currentX);
      if (Math.abs(right - left) >= 2) {
        const x0 = u.posToVal(left, 'x');
        const x1 = u.posToVal(right, 'x');
        if (Math.abs(x1 - x0) >= threshold) {
          const current = selectionRef.current;
          if (current.mode && current.onSelectRange) {
            Promise.resolve(
              current.onSelectRange(
                Math.min(Math.round(x0), Math.round(x1)),
                Math.max(Math.round(x0), Math.round(x1))
              )
            ).catch(() => null);
          }
        }
      }
      clearSelection();
      startX = null;
      currentX = null;
    };

    const onTouchStart = (event: TouchEvent) => {
      if (event.touches.length !== 1) {
        return;
      }
      event.preventDefault();
      const x = clampToPlotX(event.touches[0].clientX);
      startX = x;
      currentX = x;
      drawSelection(x, x);
    };

    const onTouchMove = (event: TouchEvent) => {
      if (startX == null || event.touches.length < 1) {
        return;
      }
      event.preventDefault();
      const x = clampToPlotX(event.touches[0].clientX);
      currentX = x;
      drawSelection(startX, x);
    };

    const onTouchEnd = (event: TouchEvent) => {
      if (startX == null) {
        return;
      }
      event.preventDefault();
      finishSelection();
    };

    const onTouchCancel = () => {
      clearSelection();
      startX = null;
      currentX = null;
    };

    over.addEventListener('touchstart', onTouchStart, { passive: false });
    over.addEventListener('touchmove', onTouchMove, { passive: false });
    over.addEventListener('touchend', onTouchEnd, { passive: false });
    over.addEventListener('touchcancel', onTouchCancel, { passive: false });

    return () => {
      over.removeEventListener('touchstart', onTouchStart);
      over.removeEventListener('touchmove', onTouchMove);
      over.removeEventListener('touchend', onTouchEnd);
      over.removeEventListener('touchcancel', onTouchCancel);
      over.style.touchAction = previousTouchAction;
      over.style.cursor = previousCursor;
      clearSelection();
    };
  }, [selectionMode, pointCount]);

  const boundaryPercent =
    selectionMode === null ? 0 : ((1 - getSelectionWidth(selectionMode)) / 2) * 100;

  return (
    <div className="panel plot-panel" style={{ padding: 12 }}>
      {(!activePlotFrame ||
        !activePlotFrame.series ||
        Object.keys(activePlotFrame.series).length === 0) && (
        <div style={{ color: '#7a6a58', fontSize: 13, marginBottom: 8 }}>
          Waiting for data...
        </div>
      )}
      <div ref={containerRef} style={{ width: '100%', height: 420, position: 'relative' }}>
        {selectionMode !== null ? (
          <>
            <div className="plot-selection-banner">
              {selectionMode === 'autolock'
                ? 'Autolock selection armed: drag over the target line.'
                : 'Optimization selection armed: drag over the target region.'}
            </div>
            {boundaryPercent > 0 ? (
              <>
                <div className="plot-selection-mask" style={{ left: 0, width: `${boundaryPercent}%` }} />
                <div
                  className="plot-selection-mask"
                  style={{ right: 0, width: `${boundaryPercent}%` }}
                />
              </>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}
