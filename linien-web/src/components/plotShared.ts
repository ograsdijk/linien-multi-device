// Shared helpers between PlotPanel (full interactive plot) and
// OverviewPlotPanel (slim thumbnail). Both produce the same plot frame
// shape via the same buffer/aliasing/y-autoscale pipeline; the
// differences are in interactivity (cursor, legend, selection) and
// axis density.

export const N_POINTS = 2048;
export const DECIMATION = 8;
export const ADC_SAMPLE_RATE = 125e6;

// uPlot accepts typed arrays as well as plain arrays. We use Float64Array
// for both x and series data so we can reuse pre-allocated buffers
// across plot frames and avoid the per-frame allocation of thousands of
// `Array<number | null>` slots.
export type PlotData = [Float64Array, ...Float64Array[]];

export const SERIES_KEYS = [
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

export type SeriesKey = (typeof SERIES_KEYS)[number];

export const LABELS: Record<SeriesKey, string> = {
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

export const PALETTE = {
  errorCombined: '#d62728',
  slowHistory: '#2ca02c',
  monitor: '#1f77b4',
  controlSignal: '#bcbd22',
  error1: '#e377c2',
  controlHistory: '#ff7f0e',
  error2: '#9467bd',
  monitorHistory: '#17becf',
};

export const toRgba = (hex: string, alpha: number) => {
  const normalized = hex.replace('#', '');
  if (normalized.length !== 6) return hex;
  const r = parseInt(normalized.slice(0, 2), 16);
  const g = parseInt(normalized.slice(2, 4), 16);
  const b = parseInt(normalized.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
};

export const SERIES_STYLE: Record<
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

export const SERIES_INDEX = SERIES_KEYS.reduce((acc, key, idx) => {
  acc[key] = idx + 1;
  return acc;
}, {} as Record<SeriesKey, number>);

export const BAND_CONFIGS = [
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

// Shared monotonic x-axis buffer pool keyed by point count. uPlot accepts
// typed arrays in `setData`, so we can hand out the same buffer for every
// plot panel sharing the same point count without copying.
const X_BUFFER_CACHE = new Map<number, Float64Array>();

export const getXBuffer = (count: number): Float64Array => {
  const cached = X_BUFFER_CACHE.get(count);
  if (cached) return cached;
  const values = new Float64Array(count);
  for (let i = 0; i < count; i++) values[i] = i;
  X_BUFFER_CACHE.set(count, values);
  return values;
};

export const toFinite = (value: unknown): number | null => {
  if (value == null) return null;
  const num = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(num) ? num : null;
};

const coerceToFiniteOrNaN = (v: unknown): number => {
  if (v == null) return NaN;
  if (typeof v === 'number') return Number.isFinite(v) ? v : NaN;
  const num = Number(v);
  return Number.isFinite(num) ? num : NaN;
};

export type SeriesStats = {
  hasFinite: boolean;
  min: number;
  max: number;
};

// Write `value` into the first `count` slots of `out`, tracking min/max
// of the finite points in the same pass. The y-axis range is then built
// directly from these per-series stats (over visible series only),
// letting us call `u.setData(data, false)` and skip uPlot's internal
// rescale-everything sweep on every frame.
export const writeSeriesInto = (
  out: Float64Array,
  count: number,
  value: unknown
): SeriesStats => {
  let hasFinite = false;
  let min = Infinity;
  let max = -Infinity;
  const updateStats = (num: number) => {
    if (num !== num) return; // NaN check
    hasFinite = true;
    if (num < min) min = num;
    if (num > max) max = num;
  };

  if (Array.isArray(value)) {
    const len = Math.min(value.length, count);
    for (let i = 0; i < len; i++) {
      const num = coerceToFiniteOrNaN(value[i]);
      out[i] = num;
      updateStats(num);
    }
    for (let i = len; i < count; i++) out[i] = NaN;
    return { hasFinite, min, max };
  }
  if (ArrayBuffer.isView(value)) {
    const typed = value as unknown as ArrayLike<number>;
    const len = Math.min(typed.length, count);
    for (let i = 0; i < len; i++) {
      const v = typed[i];
      if (Number.isFinite(v)) {
        out[i] = v;
        updateStats(v);
      } else {
        out[i] = NaN;
      }
    }
    for (let i = len; i < count; i++) out[i] = NaN;
    return { hasFinite, min, max };
  }
  if (value && typeof value === 'object') {
    // Slow path: object with numeric-string keys (rare in practice but
    // kept for compatibility with legacy payloads).
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([key]) => String(Number(key)) === key)
      .sort((a, b) => Number(a[0]) - Number(b[0]));
    if (entries.length > 0) {
      const len = Math.min(entries.length, count);
      for (let i = 0; i < len; i++) {
        const num = coerceToFiniteOrNaN(entries[i][1]);
        out[i] = num;
        updateStats(num);
      }
      for (let i = len; i < count; i++) out[i] = NaN;
      return { hasFinite, min, max };
    }
    const values = Object.values(value as Record<string, unknown>);
    const len = Math.min(values.length, count);
    for (let i = 0; i < len; i++) {
      const num = coerceToFiniteOrNaN(values[i]);
      out[i] = num;
      updateStats(num);
    }
    for (let i = len; i < count; i++) out[i] = NaN;
    return { hasFinite, min, max };
  }
  for (let i = 0; i < count; i++) out[i] = NaN;
  return { hasFinite: false, min: Infinity, max: -Infinity };
};

export type AxisTheme = { axis: string; grid: string; tick: string };

const FALLBACK_AXIS_THEME: AxisTheme = {
  axis: '#111111',
  grid: 'rgba(0, 0, 0, 0.15)',
  tick: 'rgba(0, 0, 0, 0.35)',
};
const FALLBACK_ACCENT = '#c4472d';

export const getAxisTheme = (): AxisTheme => {
  if (typeof window === 'undefined') {
    return { ...FALLBACK_AXIS_THEME };
  }
  const styles = getComputedStyle(document.documentElement);
  const axis = styles.getPropertyValue('--ink').trim() || FALLBACK_AXIS_THEME.axis;
  const grid = styles.getPropertyValue('--grid').trim() || FALLBACK_AXIS_THEME.grid;
  const tick = styles.getPropertyValue('--tick').trim() || FALLBACK_AXIS_THEME.tick;
  return { axis, grid, tick };
};

export const getAccentColor = (): string => {
  if (typeof window === 'undefined') {
    return FALLBACK_ACCENT;
  }
  const styles = getComputedStyle(document.documentElement);
  return styles.getPropertyValue('--accent').trim() || FALLBACK_ACCENT;
};

// Module-level theme cache shared by every plot panel. All panels read
// the same CSS variables off document.documentElement, so one cache
// serves all of them and we do exactly ONE getComputedStyle per
// color-scheme change instead of one per panel per draw.
//
// getComputedStyle forces a style flush; calling it inside a uPlot
// `draw` hook (which fires on every frame and every redraw) was a
// per-frame forced reflow -- Lighthouse measured ~57 ms of it across
// the tab-switch redraw bursts. The draw hooks now read from this
// cache, which is refreshed only when the scheme MutationObserver
// fires.
let _cachedAxisTheme: AxisTheme | null = null;
let _cachedAccent: string | null = null;

export const refreshThemeCache = (): void => {
  _cachedAxisTheme = getAxisTheme();
  _cachedAccent = getAccentColor();
};

export const getCachedAxisTheme = (): AxisTheme => {
  if (_cachedAxisTheme === null) refreshThemeCache();
  return _cachedAxisTheme as AxisTheme;
};

export const getCachedAccentColor = (): string => {
  if (_cachedAccent === null) refreshThemeCache();
  return _cachedAccent as string;
};

// Default y-range pad and fallback for when no visible series has any
// finite value (e.g. first frame after connect, or all data gated off).
export const Y_FALLBACK_MIN = -1;
export const Y_FALLBACK_MAX = 1;
export const Y_RANGE_PAD_FRACTION = 0.05;

export const padYRange = (yMin: number, yMax: number): { yMin: number; yMax: number } => {
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMin > yMax) {
    return { yMin: Y_FALLBACK_MIN, yMax: Y_FALLBACK_MAX };
  }
  const span = yMax - yMin;
  if (span === 0) {
    // Single-value series: pad symmetrically so the line doesn't sit on
    // top of the axis.
    const pad = Math.abs(yMin) > 0 ? Math.abs(yMin) * 0.05 : 0.01;
    return { yMin: yMin - pad, yMax: yMax + pad };
  }
  const pad = span * Y_RANGE_PAD_FRACTION;
  return { yMin: yMin - pad, yMax: yMax + pad };
};
