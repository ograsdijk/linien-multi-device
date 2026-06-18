// Micro-benchmark for the plot hot path.
//
// Validates the y-autoscale takeover claim ("5-10x cheaper per frame")
// by timing three things at realistic input sizes:
//
//   1. writeSeriesInto  --  old version (returns bool) vs new (returns
//      {hasFinite, min, max}). Per-frame budget = 12 cards x 12 series x
//      ~10 fps = 1440 calls/sec, each over 2048 points.
//
//   2. simulateUplotInternalAutoscale  --  models what uPlot's setData
//      (with the second arg true) does: iterate every visible series's
//      points to find y min/max. This is what the new code skips.
//
//   3. JSON.parse on a realistic 30 KB summary plot_frame payload --
//      the worker offload is only useful if this is actually slow.
//
// Each bench runs N iterations, reports total ms and ns/op.
// No browser, no React, no uPlot -- just the numeric functions.

const N_POINTS = 2048;
const FRAMES = 5000;          // simulated frame deliveries
const FRAMES_HEAVY = 2000;    // for the heavier per-frame work

// --- Synthetic data ----------------------------------------------------

function makeRealisticSeries(n) {
  // Mix of finite floats and an occasional NaN, matching the shape of
  // an error_signal trace.
  const a = new Array(n);
  for (let i = 0; i < n; i++) {
    a[i] = i % 503 === 0 ? null : Math.sin(i / 30) * 0.6 + (i % 17) * 0.001;
  }
  return a;
}

function makeFrameSeries() {
  // 5 visible series (SUMMARY_SERIES_KEYS): combined_error,
  // control_signal, error_signal_1, error_signal_2, monitor_signal.
  return {
    combined_error: makeRealisticSeries(N_POINTS),
    control_signal: makeRealisticSeries(N_POINTS),
    error_signal_1: makeRealisticSeries(N_POINTS),
    error_signal_2: makeRealisticSeries(N_POINTS),
    monitor_signal: makeRealisticSeries(N_POINTS),
  };
}

// --- Old writeSeriesInto (pre b450391) --------------------------------

function coerceToFiniteOrNaN(v) {
  if (v == null) return NaN;
  if (typeof v === 'number') return Number.isFinite(v) ? v : NaN;
  const num = Number(v);
  return Number.isFinite(num) ? num : NaN;
}

function writeSeriesInto_OLD(out, count, value) {
  let hasFinite = false;
  if (Array.isArray(value)) {
    const len = Math.min(value.length, count);
    for (let i = 0; i < len; i++) {
      const num = coerceToFiniteOrNaN(value[i]);
      out[i] = num;
      if (!hasFinite && num === num) hasFinite = true;
    }
    for (let i = len; i < count; i++) out[i] = NaN;
    return hasFinite;
  }
  return false;
}

// --- New writeSeriesInto (b450391) ------------------------------------

function writeSeriesInto_NEW(out, count, value) {
  let hasFinite = false;
  let min = Infinity;
  let max = -Infinity;
  if (Array.isArray(value)) {
    const len = Math.min(value.length, count);
    for (let i = 0; i < len; i++) {
      const num = coerceToFiniteOrNaN(value[i]);
      out[i] = num;
      if (num === num) {
        hasFinite = true;
        if (num < min) min = num;
        if (num > max) max = num;
      }
    }
    for (let i = len; i < count; i++) out[i] = NaN;
    return { hasFinite, min, max };
  }
  return { hasFinite: false, min: Infinity, max: -Infinity };
}

// --- Variant: prime + else-if (fewer branches in steady state) -------

function writeSeriesInto_VAR(out, count, value) {
  let hasFinite = false;
  let min = 0; // sentinel; overwritten on first finite
  let max = 0;
  if (Array.isArray(value)) {
    const len = Math.min(value.length, count);
    for (let i = 0; i < len; i++) {
      const num = coerceToFiniteOrNaN(value[i]);
      out[i] = num;
      if (num === num) {
        if (!hasFinite) {
          min = num;
          max = num;
          hasFinite = true;
        } else if (num < min) {
          min = num;
        } else if (num > max) {
          max = num;
        }
      }
    }
    for (let i = len; i < count; i++) out[i] = NaN;
    return { hasFinite, min, max };
  }
  return { hasFinite: false, min: Infinity, max: -Infinity };
}

// --- Simulated uPlot internal autoscale -------------------------------
// This is what setData(data, true) does (roughly): iterate every visible
// series's typed array to recompute the y scale's min/max. Modelled on
// uPlot's internal `setScale` -> `autoScaleY` -> per-series iteration.

function simulateUplotInternalAutoscale(buffers) {
  let min = Infinity;
  let max = -Infinity;
  for (let s = 0; s < buffers.length; s++) {
    const buf = buffers[s];
    const len = buf.length;
    for (let i = 0; i < len; i++) {
      const v = buf[i];
      if (v === v) { // not NaN
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
  }
  return [min, max];
}

// --- New aggregate y range (from per-series stats) --------------------
// What the new code does: aggregate {min, max} from per-series stats
// already returned by writeSeriesInto_NEW. No second pass over data.

function aggregateY_NEW(stats) {
  let yMin = Infinity;
  let yMax = -Infinity;
  for (let i = 0; i < stats.length; i++) {
    const s = stats[i];
    if (!s.hasFinite) continue;
    if (s.min < yMin) yMin = s.min;
    if (s.max > yMax) yMax = s.max;
  }
  return [yMin, yMax];
}

// --- Bench harness ----------------------------------------------------

function bench(name, iters, fn) {
  // warmup
  for (let i = 0; i < Math.min(50, iters); i++) fn();
  const t0 = performance.now();
  for (let i = 0; i < iters; i++) fn();
  const t1 = performance.now();
  const total = t1 - t0;
  const perOp = (total * 1e6) / iters; // ns per op
  return { name, iters, totalMs: total, nsPerOp: perOp };
}

function fmt(b) {
  return `${b.name.padEnd(50)} ${b.iters.toString().padStart(6)} iters  ${b.totalMs.toFixed(2).padStart(8)} ms total  ${b.nsPerOp.toFixed(0).padStart(8)} ns/op`;
}

// --- Scenario 1: writeSeriesInto, old vs new --------------------------
// 12 visible series, 2048 points each, FRAMES iterations.

const series = makeFrameSeries();
const seriesList = Object.values(series);
// extend to 12 by duplicating (matches PlotPanel's 12-slot buffer layout
// where many are NaN in summary frames).
while (seriesList.length < 12) seriesList.push(seriesList[0]);
const buffers = seriesList.map(() => new Float64Array(N_POINTS));

const writeOld = bench('writeSeriesInto OLD x 12 series', FRAMES, () => {
  for (let s = 0; s < 12; s++) {
    writeSeriesInto_OLD(buffers[s], N_POINTS, seriesList[s]);
  }
});

const writeNewStats = new Array(12);
const writeNew = bench('writeSeriesInto NEW x 12 series (with stats)', FRAMES, () => {
  for (let s = 0; s < 12; s++) {
    writeNewStats[s] = writeSeriesInto_NEW(buffers[s], N_POINTS, seriesList[s]);
  }
});

const writeVarStats = new Array(12);
const writeVar = bench('writeSeriesInto VAR x 12 series (else-if)', FRAMES, () => {
  for (let s = 0; s < 12; s++) {
    writeVarStats[s] = writeSeriesInto_VAR(buffers[s], N_POINTS, seriesList[s]);
  }
});

// --- Scenario 2: y autoscale, uPlot internal vs our aggregator -------

const uplotAuto = bench('simulateUplotInternalAutoscale (12 buffers)', FRAMES_HEAVY, () => {
  simulateUplotInternalAutoscale(buffers);
});

const aggNew = bench('aggregateY_NEW (from per-series stats)', FRAMES_HEAVY, () => {
  aggregateY_NEW(writeNewStats);
});

// Combined: old approach = writeOld + uplotAuto
//           new approach = writeNew + aggNew
const oldPerFrame_ns = writeOld.nsPerOp + uplotAuto.nsPerOp;
const newPerFrame_ns = writeNew.nsPerOp + aggNew.nsPerOp;
const speedup = oldPerFrame_ns / newPerFrame_ns;

// --- Scenario 3: JSON.parse on a 30 KB plot_frame payload -------------

const payloadObj = {
  type: 'plot_frame',
  lock: false,
  dual_channel: false,
  series,
  signal_power: { channel1: null, channel2: null },
  stats: { error_std: null, control_std: null },
  lock_indicator: {
    state: 'unknown',
    reasons: [],
    metrics: { control_stuck_s: 0, control_rail_s: 0 },
    last_transition_at: null,
  },
  auto_relock: {
    enabled: false, state: 'idle', attempts: 0, max_attempts: 3,
    cooldown_remaining_s: 0,
  },
  lock_target: null,
  x_label: 'sweep voltage',
  x_unit: 'V',
};
const payload = JSON.stringify(payloadObj);
console.log(`payload size: ${(payload.length / 1024).toFixed(1)} KB`);

const jsonParse = bench('JSON.parse on plot_frame payload', FRAMES, () => {
  JSON.parse(payload);
});

// --- Report -----------------------------------------------------------

console.log('');
console.log(fmt(writeOld));
console.log(fmt(writeNew));
console.log(fmt(writeVar));
console.log(fmt(uplotAuto));
console.log(fmt(aggNew));
console.log(fmt(jsonParse));
console.log('');
console.log('Per-frame combined (write + y autoscale):');
console.log(`  OLD: ${(oldPerFrame_ns / 1000).toFixed(2)} us/frame`);
console.log(`  NEW: ${(newPerFrame_ns / 1000).toFixed(2)} us/frame`);
console.log(`  Speedup: ${speedup.toFixed(2)}x`);
console.log('');
const FPS = 10;
const CARDS = 12;
const oldBudget_ms_per_sec = (oldPerFrame_ns * FPS * CARDS) / 1e6;
const newBudget_ms_per_sec = (newPerFrame_ns * FPS * CARDS) / 1e6;
console.log(`At ${CARDS} cards x ${FPS} fps:`);
console.log(`  OLD: ${oldBudget_ms_per_sec.toFixed(1)} ms/sec on hot-path numeric work`);
console.log(`  NEW: ${newBudget_ms_per_sec.toFixed(1)} ms/sec on hot-path numeric work`);
console.log(`  saved: ${(oldBudget_ms_per_sec - newBudget_ms_per_sec).toFixed(1)} ms/sec`);
console.log('');
console.log(`JSON.parse on a single ${(payload.length / 1024).toFixed(0)} KB frame: ${(jsonParse.nsPerOp / 1000).toFixed(1)} us`);
console.log(`At ${CARDS} cards x ${FPS} fps: ${((jsonParse.nsPerOp * FPS * CARDS) / 1e6).toFixed(1)} ms/sec on JSON.parse alone`);
console.log('(parse is offloaded to a worker -- this is the cost the main thread is NOT paying)');
