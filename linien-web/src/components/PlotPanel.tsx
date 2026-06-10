import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
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
  toFinite,
  toRgba,
  writeSeriesInto,
} from './plotShared';

type SelectionMode = 'autolock' | 'optimization' | null;

export type PlotPanelHandle = {
  applyFrame: (frame: PlotFrame) => void;
};

type PlotPanelProps = {
  selectionMode: SelectionMode;
  onSelectRange?: (x0: number, x1: number) => void | Promise<void>;
  lockState?: boolean;
  sweepCenter?: number;
  sweepAmplitude?: number;
  showManualTarget?: boolean;
  initActive?: boolean;
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

// PlotPanel exposes an imperative API: parent calls applyFrame(frame)
// when new plot data arrives. Plot data never flows through React props
// or state -- uPlot is updated directly. Component renders only when
// lockState / selectionMode / sweep params / showManualTarget change
// (all rare relative to the 10 Hz plot stream).
//
// Selection freeze semantic: when selectionMode is non-null, the
// parent (DeviceWorkspace) STOPS calling applyFrame so the plot stays
// at whatever was last applied. On selection cancel/commit the parent
// resumes calling applyFrame. No frozenFrame React state needed.
export const PlotPanel = forwardRef<PlotPanelHandle, PlotPanelProps>(function PlotPanel(
  {
    selectionMode,
    onSelectRange,
    lockState,
    sweepCenter,
    sweepAmplitude,
    showManualTarget,
    initActive = true,
  },
  ref,
) {
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

  // Latest frame stashed for replay on lockState change or post-init.
  const latestFrameRef = useRef<PlotFrame | null>(null);
  // Latest computed lockAxis -- detect transitions to update axis
  // label + force a recalcAxes redraw exactly once per transition.
  const lastLockAxisRef = useRef<boolean | null>(null);

  // Track props in refs so applyFrameInternal can read latest values
  // without re-binding the imperative handle.
  const lockStateRef = useRef<boolean | undefined>(lockState);
  const sweepCenterRef = useRef<number>(toFinite(sweepCenter) ?? 0);
  const sweepAmplitudeRef = useRef<number>(toFinite(sweepAmplitude) ?? 1);
  const showManualTargetRef = useRef<boolean>(!!showManualTarget);

  useEffect(() => {
    lockStateRef.current = lockState;
  }, [lockState]);
  useEffect(() => {
    sweepCenterRef.current = toFinite(sweepCenter) ?? 0;
  }, [sweepCenter]);
  useEffect(() => {
    sweepAmplitudeRef.current = toFinite(sweepAmplitude) ?? 1;
  }, [sweepAmplitude]);
  useEffect(() => {
    showManualTargetRef.current = !!showManualTarget;
  }, [showManualTarget]);

  // Per-PlotPanel reusable typed-array buffers for series data.
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

  // Inputs for the axis tick / cursor value formatters (stable callbacks
  // that read from this ref). Updated by applyFrameInternal as
  // lockAxis / pointCount evolve, and by sweep-param effects above.
  const axisInputsRef = useRef({
    lockAxis: false,
    sweepCenterValue: toFinite(sweepCenter) ?? 0,
    sweepAmplitudeValue: toFinite(sweepAmplitude) ?? 1,
    pointCount: N_POINTS,
  });
  useEffect(() => {
    axisInputsRef.current.sweepCenterValue = toFinite(sweepCenter) ?? 0;
    axisInputsRef.current.sweepAmplitudeValue = toFinite(sweepAmplitude) ?? 1;
  }, [sweepCenter, sweepAmplitude]);

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

  // Recompute the manual-target overlay's x-value from the current
  // sweep params + pointCount + lockAxis. Called from both
  // applyFrameInternal (so frame-driven pointCount/lockAxis updates
  // see the latest) and the sweep-param effect below (so prop-driven
  // changes also refresh).
  const recomputeManualTarget = (): void => {
    const lockAxis = lastLockAxisRef.current ?? false;
    if (!showManualTargetRef.current || lockAxis) {
      manualTargetRef.current = { enabled: false, xVal: null };
      return;
    }
    const center = sweepCenterRef.current;
    const amp = sweepAmplitudeRef.current;
    const pts = Math.max(pointCountRef.current, 2);
    const min = center - amp;
    const max = center + amp;
    const spacing = (max - min) / (pts - 1);
    const targetVoltage = Math.max(min, Math.min(max, center));
    const xVal =
      Number.isFinite(spacing) && spacing !== 0 ? (targetVoltage - min) / spacing : 0;
    manualTargetRef.current = { enabled: true, xVal };
  };

  useEffect(() => {
    recomputeManualTarget();
    uplotRef.current?.redraw();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showManualTarget, sweepCenter, sweepAmplitude]);

  // Flip once on first applied frame so the placeholder disappears.
  const hasDataRef = useRef(false);
  const [hasData, setHasData] = useState(false);

  const initializedRef = useRef(false);

  const applyFrameInternal = (frame: PlotFrame): void => {
    latestFrameRef.current = frame;
    const u = uplotRef.current;
    if (!u) return; // Init effect will replay.
    if (!hasDataRef.current) {
      hasDataRef.current = true;
      setHasData(true);
    }

    const buffers = seriesBuffersRef.current;
    const hasDataByKey = hasDataByKeyRef.current;
    const stats = seriesStatsRef.current;
    const series = frame.series;

    // Derive point count.
    let count = 0;
    for (const key of SERIES_KEYS) {
      const v = series?.[key];
      const len = Array.isArray(v)
        ? v.length
        : ArrayBuffer.isView(v)
        ? (v as unknown as ArrayLike<number>).length
        : 0;
      if (len > count) count = len;
    }
    if (count === 0) count = N_POINTS;
    pointCountRef.current = count;
    axisInputsRef.current.pointCount = count;

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

    const lockAxis =
      typeof lockStateRef.current === 'boolean'
        ? lockStateRef.current
        : Boolean(frame.lock);

    // Detect lockAxis transitions: update axis label + recalc axes.
    if (lockAxis !== lastLockAxisRef.current) {
      lastLockAxisRef.current = lockAxis;
      axisInputsRef.current.lockAxis = lockAxis;
      u.axes[0].label = lockAxis ? 'time (us)' : 'sweep voltage (V)';
      // Schedule a recalcAxes redraw via the deferred path -- the
      // batch below will redraw anyway, but recalcAxes is needed
      // when the label changes. Calling here ensures the label takes
      // effect.
      u.redraw(false, true);
      // Manual target visibility depends on lockAxis.
      recomputeManualTarget();
    }

    // Alias combined_error <- error_signal_1 in sweep mode.
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

    const dual = Boolean(frame.dual_channel);
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

    let hasDataKey = '';
    for (const key of SERIES_KEYS) hasDataKey += hasDataByKey[key] ? '1' : '0';
    const last = lastVisibilityRef.current;
    const visibilityChanged =
      last.lockAxis !== lockAxis ||
      last.dual !== dual ||
      last.hasDataKey !== hasDataKey;

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

    lockTargetRef.current = toFinite(frame.lock_target);
    // Manual target's xVal depends on pointCount (via spacing).
    // pointCount may have grown -- recompute.
    recomputeManualTarget();

    u.batch((uplot: uPlot) => {
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
          uplot.setSeries(seriesIdx, { show }, false);
        });
        suppressSeriesEventRef.current = false;
      }
      uplot.setData(plotData, false);
      if (xChanged) {
        uplot.setScale('x', { min: 0, max: Math.max(count - 1, 1) });
        lastAppliedXMaxRef.current = count;
      }
      if (yChanged) {
        uplot.setScale('y', { min: padded.yMin, max: padded.yMax });
        lastAppliedYRef.current = { min: padded.yMin, max: padded.yMax };
      }
      if (visibilityChanged) {
        const rows = uplot.root.querySelectorAll<HTMLElement>('.u-legend .u-series');
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
  };

  useImperativeHandle(
    ref,
    () => ({
      applyFrame: applyFrameInternal,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Re-apply latest frame when lockState prop changes.
  useEffect(() => {
    if (uplotRef.current && latestFrameRef.current) {
      applyFrameInternal(latestFrameRef.current);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lockState]);

  useEffect(() => {
    selectionRef.current = { mode: selectionMode, onSelectRange };
  }, [selectionMode, onSelectRange]);

  useEffect(() => {
    if (!uplotRef.current) return;
    uplotRef.current.select.show = selectionMode !== null;
    uplotRef.current.redraw();
  }, [selectionMode]);

  // Init uPlot lazily once initActive becomes true.
  useEffect(() => {
    if (initializedRef.current || !initActive) return;
    initializedRef.current = true;
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

    const initialWidth =
      container.clientWidth > 10 ? container.clientWidth : sizeRef.current.width;
    sizeRef.current.width = initialWidth;

    const axisTheme = getAxisTheme();
    const makeStroke = (color: string) => () => color;

    const bandLinks = BAND_CONFIGS.map((band) => ({
      controllerIdx: SERIES_INDEX[band.controller],
      memberIdxs: [SERIES_INDEX[band.upper], SERIES_INDEX[band.lower]],
    }));

    const initialLockAxis = lastLockAxisRef.current ?? false;
    const opts: uPlot.Options = {
      width: initialWidth,
      height: sizeRef.current.height,
      scales: {
        x: { time: false, auto: false, range: [0, N_POINTS - 1] },
        y: { auto: false },
      },
      cursor: {
        drag: { setScale: false },
      },
      select: { show: true, left: 0, top: 0, width: 0, height: 0 },
      axes: [
        {
          label: initialLockAxis ? 'time (us)' : 'sweep voltage (V)',
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
              Math.max(minPos, maxPos),
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
              current.onSelectRange(
                Math.min(Math.round(x0), Math.round(x1)),
                Math.max(Math.round(x0), Math.round(x1)),
              ),
            ).catch(() => null);
            u.setSelect({ left: 0, width: 0, height: 0, top: 0 }, false);
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

    // Replay any frame stashed before init completed.
    if (latestFrameRef.current) {
      applyFrameInternal(latestFrameRef.current);
    }

    return () => {
      window.removeEventListener('resize', handleResize);
      schemeObserver?.disconnect();
      observer?.disconnect();
      uplotRef.current?.destroy();
      uplotRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initActive]);

  // Selection touch handlers (unchanged from prior version).
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

    const bounds = getSelectionBounds(selectionMode, pointCountRef.current);
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
        { left, width, top: 0, height: over.clientHeight },
        false,
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
                Math.max(Math.round(x0), Math.round(x1)),
              ),
            ).catch(() => null);
          }
        }
      }
      clearSelection();
      startX = null;
      currentX = null;
    };

    const onTouchStart = (event: TouchEvent) => {
      if (event.touches.length !== 1) return;
      event.preventDefault();
      const x = clampToPlotX(event.touches[0].clientX);
      startX = x;
      currentX = x;
      drawSelection(x, x);
    };

    const onTouchMove = (event: TouchEvent) => {
      if (startX == null || event.touches.length < 1) return;
      event.preventDefault();
      const x = clampToPlotX(event.touches[0].clientX);
      currentX = x;
      drawSelection(startX, x);
    };

    const onTouchEnd = (event: TouchEvent) => {
      if (startX == null) return;
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
  }, [selectionMode]);

  const boundaryPercent =
    selectionMode === null ? 0 : ((1 - getSelectionWidth(selectionMode)) / 2) * 100;

  return (
    <div className="panel plot-panel" style={{ padding: 12 }}>
      {!hasData ? (
        <div style={{ color: '#7a6a58', fontSize: 13, marginBottom: 8 }}>
          Waiting for data...
        </div>
      ) : null}
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
});
