import { useEffect, useMemo, useRef, useState } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import type { PlotFrame } from '../types';

const N_POINTS = 2048;
const DECIMATION = 8;
const ADC_SAMPLE_RATE = 125e6;

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

type PlotData = [number[], ...Array<Array<number | null>>];

const SERIES_KEYS = [
  'combined_error',
  'control_signal',
  'control_signal_history',
  'slow_history',
  'monitor_signal_history',
  'error_signal_1',
  'error_signal_2',
  'monitor_signal',
  'signal_strength_a_upper',
  'signal_strength_a_lower',
  'signal_strength_b_upper',
  'signal_strength_b_lower',
] as const;

type SeriesKey = (typeof SERIES_KEYS)[number];

const LABELS: Record<SeriesKey, string> = {
  combined_error: 'error',
  control_signal: 'control',
  control_signal_history: 'control history',
  slow_history: 'slow history',
  monitor_signal_history: 'monitor history',
  error_signal_1: 'error 1',
  error_signal_2: 'error 2',
  monitor_signal: 'monitor',
  signal_strength_a_upper: 'signal A+',
  signal_strength_a_lower: 'signal A-',
  signal_strength_b_upper: 'signal B+',
  signal_strength_b_lower: 'signal B-',
};

const PALETTE = {
  errorCombined: '#d62728',
  slowHistory: '#2ca02c',
  monitor: '#1f77b4',
  controlSignal: '#bcbd22',
  error1: '#e377c2',
  controlHistory: '#ff7f0e',
  error2: '#9467bd',
  monitorHistory: '#17becf',
};

const toRgba = (hex: string, alpha: number) => {
  const normalized = hex.replace('#', '');
  if (normalized.length !== 6) return hex;
  const r = parseInt(normalized.slice(0, 2), 16);
  const g = parseInt(normalized.slice(2, 4), 16);
  const b = parseInt(normalized.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
};

const SERIES_STYLE: Record<
  SeriesKey,
  { color: string; strokeAlpha?: number; legendHidden?: boolean }
> = {
  combined_error: { color: PALETTE.errorCombined },
  control_signal: { color: PALETTE.controlSignal },
  control_signal_history: { color: PALETTE.controlHistory },
  slow_history: { color: PALETTE.slowHistory },
  monitor_signal_history: { color: PALETTE.monitorHistory },
  error_signal_1: { color: PALETTE.error1 },
  error_signal_2: { color: PALETTE.error2 },
  monitor_signal: { color: PALETTE.monitor },
  signal_strength_a_upper: { color: PALETTE.error1, strokeAlpha: 0.4, legendHidden: true },
  signal_strength_a_lower: { color: PALETTE.error1, strokeAlpha: 0.4, legendHidden: true },
  signal_strength_b_upper: { color: PALETTE.error2, strokeAlpha: 0.4, legendHidden: true },
  signal_strength_b_lower: { color: PALETTE.error2, strokeAlpha: 0.4, legendHidden: true },
};

const POINT_STYLE: uPlot.Series.Points = { show: false };

const toFinite = (value: unknown): number | null => {
  if (value == null) return null;
  const num = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(num) ? num : null;
};

const normalizeSeries = (value: unknown): Array<number | null> => {
  if (Array.isArray(value)) {
    return value.map((v) => toFinite(v));
  }
  if (ArrayBuffer.isView(value)) {
    if ('length' in value) {
      const typed = value as unknown as ArrayLike<number>;
      return Array.from(typed, (v) => toFinite(v));
    }
    return [];
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([key]) => String(Number(key)) === key)
      .sort((a, b) => Number(a[0]) - Number(b[0]));
    if (entries.length > 0) {
      return entries.map(([, v]) => toFinite(v));
    }
    return Object.values(value as Record<string, unknown>).map((v) => toFinite(v));
  }
  return [];
};

const SERIES_INDEX = SERIES_KEYS.reduce((acc, key, idx) => {
  acc[key] = idx + 1;
  return acc;
}, {} as Record<SeriesKey, number>);

const BAND_CONFIGS = [
  {
    upper: 'signal_strength_a_upper' as SeriesKey,
    lower: 'signal_strength_a_lower' as SeriesKey,
    fill: toRgba(PALETTE.error1, 0.22),
    controller: 'error_signal_1' as SeriesKey,
  },
  {
    upper: 'signal_strength_b_upper' as SeriesKey,
    lower: 'signal_strength_b_lower' as SeriesKey,
    fill: toRgba(PALETTE.error2, 0.22),
    controller: 'error_signal_2' as SeriesKey,
  },
];

const getAxisTheme = () => {
  if (typeof window === 'undefined') {
    return { axis: '#111111', grid: 'rgba(0, 0, 0, 0.15)', tick: 'rgba(0, 0, 0, 0.35)' };
  }
  const styles = getComputedStyle(document.documentElement);
  const axis = styles.getPropertyValue('--ink').trim() || '#111111';
  const grid = styles.getPropertyValue('--grid').trim() || 'rgba(0, 0, 0, 0.15)';
  const tick = styles.getPropertyValue('--tick').trim() || 'rgba(0, 0, 0, 0.35)';
  return { axis, grid, tick };
};

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

const getAccentColor = () => {
  if (typeof window === 'undefined') {
    return '#c4472d';
  }
  const styles = getComputedStyle(document.documentElement);
  return styles.getPropertyValue('--accent').trim() || '#c4472d';
};

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

  const { data, pointCount } = useMemo<{ data: PlotData; pointCount: number }>(() => {
    const seriesRaw = SERIES_KEYS.map((key) => normalizeSeries(activePlotFrame?.series?.[key]));
    if (!lockAxis) {
      const combinedIdx = SERIES_KEYS.indexOf('combined_error');
      const errIdx = SERIES_KEYS.indexOf('error_signal_1');
      if (seriesRaw[errIdx] && seriesRaw[errIdx].length > 0) {
        seriesRaw[combinedIdx] = seriesRaw[errIdx];
      }
    }
    const maxLen = Math.max(...seriesRaw.map((series) => series.length), 0);
    const count = maxLen > 0 ? maxLen : N_POINTS;
    const x = Array.from({ length: count }, (_, i) => i);
    const seriesData = seriesRaw.map((series) => {
      if (series.length === count) {
        return series;
      }
      if (series.length > 0) {
        return Array.from({ length: count }, (_, idx) => series[idx] ?? null);
      }
      return Array(count).fill(null);
    });
    return { data: [x, ...seriesData], pointCount: count };
  }, [activePlotFrame, lockAxis]);

  useEffect(() => {
    pointCountRef.current = pointCount;
  }, [pointCount]);

  const axisValues = useMemo(() => {
    const dtMicroSeconds = (DECIMATION / ADC_SAMPLE_RATE) * 1e6;
    return (_u: uPlot, ticks: number[]) => {
      if (lockAxis) {
        return ticks.map((v) => (v * dtMicroSeconds).toFixed(1));
      }
      const min = sweepCenterValue - sweepAmplitudeValue;
      const max = sweepCenterValue + sweepAmplitudeValue;
      const spacing = (max - min) / (Math.max(pointCount, 2) - 1);
      return ticks.map((v) => (min + v * spacing).toFixed(2));
    };
  }, [lockAxis, sweepCenterValue, sweepAmplitudeValue, pointCount]);

  const xValueFormatter = useMemo(() => {
    const dtMicroSeconds = (DECIMATION / ADC_SAMPLE_RATE) * 1e6;
    return (_u: uPlot, val: number) => {
      if (!Number.isFinite(val)) return '';
      if (lockAxis) {
        return `${(val * dtMicroSeconds).toFixed(2)} us`;
      }
      const min = sweepCenterValue - sweepAmplitudeValue;
      const max = sweepCenterValue + sweepAmplitudeValue;
      const spacing = (max - min) / (Math.max(pointCount, 2) - 1);
      return `${(min + val * spacing).toFixed(3)} V`;
    };
  }, [lockAxis, sweepCenterValue, sweepAmplitudeValue, pointCount]);

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
        x: { time: false },
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

    uplotRef.current = new uPlot(opts, data, container);
    uplotRef.current.setData(data, true);
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

  useEffect(() => {
    if (!uplotRef.current) return;
    const hasDataByKey = SERIES_KEYS.reduce((acc, key, idx) => {
      const values = data[idx + 1];
      const hasFinite = values.some((v) => typeof v === 'number' && Number.isFinite(v));
      acc[key] = hasFinite;
      return acc;
    }, {} as Record<SeriesKey, boolean>);

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

    uplotRef.current.batch((u: uPlot) => {
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
      u.setData(data, true);
      u.setScale('x', { min: 0, max: Math.max(pointCount - 1, 1) });
      const rows = u.root.querySelectorAll<HTMLElement>('.u-legend .u-series');
      rows.forEach((row: HTMLElement, rowIdx: number) => {
        if (rowIdx === 0) return;
        const key = SERIES_KEYS[rowIdx - 1];
        if (!key) return;
        const hide = SERIES_STYLE[key].legendHidden || !desiredVisibility[key] || !hasDataByKey[key];
        row.classList.toggle('legend-hidden', hide);
      });
    });
  }, [data, pointCount, lockAxis, activePlotFrame?.dual_channel]);

  useEffect(() => {
    if (!uplotRef.current) return;
    const u = uplotRef.current;
    u.axes[0].label = axisLabel;
    u.axes[0].values = axisValues;
    u.series[0].value = xValueFormatter;
    // Force uPlot to recalculate axis ticks even when x-scale min/max did not change.
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
