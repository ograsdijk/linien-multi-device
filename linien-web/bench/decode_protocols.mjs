// Compare wire-protocol decode costs for a realistic plot_frame.
//
// Tests three protocols at the same logical payload:
//   1. JSON  (current) — orjson.dumps on the server, JSON.parse here.
//   2. Binary "naive"  — a header (counts/lengths) + raw Float32 buffers.
//                       Decoder slices typed arrays directly from a
//                       single ArrayBuffer with zero copy.
//   3. Binary "quantized" — same shape but Int16 values * 32767 (signed
//                          [-1, 1] -> int16). Halves wire size again.
//
// Each variant produces uPlot-ready data (5 series of Float32/Float64).
// Decode timing only -- no DOM, no React, no uPlot. This is the upper
// bound on what a binary protocol could save us per frame on the
// frontend main thread.

const N_POINTS = 2048;
const N_SERIES = 5;
const ITERS = 5000;

// --- Synthetic data ----------------------------------------------------

function makeRealisticSeries(n) {
  const a = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    a[i] = i % 503 === 0 ? NaN : Math.sin(i / 30) * 0.6 + (i % 17) * 0.001;
  }
  return a;
}
const seriesFloats = Array.from({ length: N_SERIES }, () => makeRealisticSeries(N_POINTS));
const seriesNames = [
  'combined_error', 'control_signal', 'error_signal_1',
  'error_signal_2', 'monitor_signal',
];

// --- 1. JSON ------------------------------------------------------------

function buildJsonPayload() {
  const series = {};
  for (let s = 0; s < N_SERIES; s++) {
    const arr = new Array(N_POINTS);
    const src = seriesFloats[s];
    for (let i = 0; i < N_POINTS; i++) {
      const v = src[i];
      arr[i] = Number.isFinite(v) ? v : null;
    }
    series[seriesNames[s]] = arr;
  }
  return JSON.stringify({
    type: 'plot_frame', lock: false, dual_channel: false,
    series,
    signal_power: { channel1: null, channel2: null },
    stats: { error_std: null, control_std: null },
    lock_indicator: {
      state: 'unknown', reasons: [],
      metrics: { control_stuck_s: 0, control_rail_s: 0 },
      last_transition_at: null,
    },
    auto_relock: {
      enabled: false, state: 'idle', attempts: 0, max_attempts: 3,
      cooldown_remaining_s: 0,
    },
    lock_target: null, x_label: 'sweep voltage', x_unit: 'V',
  });
}

// --- 2. Binary, naive (Float32) ----------------------------------------
//
// Layout:
//   u32  magic     = 0x504C4F54 ('PLOT')
//   u8   version   = 1
//   u8   nSeries
//   u16  nameTableSize (bytes of name table)
//   u32  nPoints
//   u8   flags     (bit 0 = lock, bit 1 = dual_channel)
//   ... padding to 16-byte align ...
//   name table: for each series, u8 len + utf8 bytes
//   ... padding to Float32 align ...
//   series data: nSeries * nPoints * Float32
//
// JSON sidecar (rare, small) carries lock_indicator/auto_relock/stats
// in a header chunk -- omitted from this bench since those are <0.5 KB.

function buildBinaryNaive() {
  const names = seriesNames;
  let nameBytes = 0;
  for (const n of names) nameBytes += 1 + n.length;
  const header = 4 + 1 + 1 + 2 + 4 + 1;
  const headerPad = (16 - (header % 16)) % 16;
  const nameSection = nameBytes;
  const namePad = (4 - ((header + headerPad + nameSection) % 4)) % 4;
  const dataOffset = header + headerPad + nameSection + namePad;
  const total = dataOffset + N_SERIES * N_POINTS * 4;

  const ab = new ArrayBuffer(total);
  const view = new DataView(ab);
  view.setUint32(0, 0x504C4F54);
  view.setUint8(4, 1);
  view.setUint8(5, N_SERIES);
  view.setUint16(6, nameBytes);
  view.setUint32(8, N_POINTS);
  view.setUint8(12, 0);
  let off = header + headerPad;
  for (const n of names) {
    view.setUint8(off, n.length);
    off += 1;
    for (let i = 0; i < n.length; i++) view.setUint8(off + i, n.charCodeAt(i));
    off += n.length;
  }
  const f32 = new Float32Array(ab, dataOffset, N_SERIES * N_POINTS);
  for (let s = 0; s < N_SERIES; s++) {
    f32.set(seriesFloats[s], s * N_POINTS);
  }
  return ab;
}

function decodeBinaryNaive(ab) {
  const view = new DataView(ab);
  const magic = view.getUint32(0);
  if (magic !== 0x504C4F54) throw new Error('bad magic');
  const version = view.getUint8(4);
  const nSeries = view.getUint8(5);
  const nameBytes = view.getUint16(6);
  const nPoints = view.getUint32(8);
  const flags = view.getUint8(12);
  const lock = !!(flags & 1);
  const dual = !!(flags & 2);
  const header = 4 + 1 + 1 + 2 + 4 + 1;
  const headerPad = (16 - (header % 16)) % 16;
  const namePad = (4 - ((header + headerPad + nameBytes) % 4)) % 4;
  const dataOffset = header + headerPad + nameBytes + namePad;
  let off = header + headerPad;
  const names = new Array(nSeries);
  const decoder = new TextDecoder('utf-8');
  for (let s = 0; s < nSeries; s++) {
    const len = view.getUint8(off); off += 1;
    names[s] = decoder.decode(new Uint8Array(ab, off, len));
    off += len;
  }
  // Zero-copy slices into the original buffer.
  const series = {};
  for (let s = 0; s < nSeries; s++) {
    series[names[s]] = new Float32Array(ab, dataOffset + s * nPoints * 4, nPoints);
  }
  return { lock, dual, series, version };
}

// --- 3. Binary, quantized (Int16) -------------------------------------
//
// Same layout, values stored as int16 (value * 32767). Decoder maps
// back to Float32 in one tight loop. Half the wire size of the naive
// binary variant, modest decode cost.

function buildBinaryQuantized() {
  const names = seriesNames;
  let nameBytes = 0;
  for (const n of names) nameBytes += 1 + n.length;
  const header = 4 + 1 + 1 + 2 + 4 + 1;
  const headerPad = (16 - (header % 16)) % 16;
  const namePad = (2 - ((header + headerPad + nameBytes) % 2)) % 2;
  const dataOffset = header + headerPad + nameBytes + namePad;
  const total = dataOffset + N_SERIES * N_POINTS * 2;

  const ab = new ArrayBuffer(total);
  const view = new DataView(ab);
  view.setUint32(0, 0x504C4F54);
  view.setUint8(4, 1);
  view.setUint8(5, N_SERIES);
  view.setUint16(6, nameBytes);
  view.setUint32(8, N_POINTS);
  view.setUint8(12, 2); // flags: bit1 = quantized
  let off = header + headerPad;
  for (const n of names) {
    view.setUint8(off, n.length);
    off += 1;
    for (let i = 0; i < n.length; i++) view.setUint8(off + i, n.charCodeAt(i));
    off += n.length;
  }
  const i16 = new Int16Array(ab, dataOffset, N_SERIES * N_POINTS);
  for (let s = 0; s < N_SERIES; s++) {
    const src = seriesFloats[s];
    for (let i = 0; i < N_POINTS; i++) {
      const v = src[i];
      // NaN encoded as Int16 sentinel -32768; clamp finite values to [-1, 1].
      if (!Number.isFinite(v)) {
        i16[s * N_POINTS + i] = -32768;
      } else {
        const clamped = Math.max(-1, Math.min(1, v));
        i16[s * N_POINTS + i] = Math.round(clamped * 32767);
      }
    }
  }
  return ab;
}

function decodeBinaryQuantized(ab) {
  const view = new DataView(ab);
  const nSeries = view.getUint8(5);
  const nameBytes = view.getUint16(6);
  const nPoints = view.getUint32(8);
  const header = 4 + 1 + 1 + 2 + 4 + 1;
  const headerPad = (16 - (header % 16)) % 16;
  const namePad = (2 - ((header + headerPad + nameBytes) % 2)) % 2;
  const dataOffset = header + headerPad + nameBytes + namePad;
  let off = header + headerPad;
  const names = new Array(nSeries);
  const decoder = new TextDecoder('utf-8');
  for (let s = 0; s < nSeries; s++) {
    const len = view.getUint8(off); off += 1;
    names[s] = decoder.decode(new Uint8Array(ab, off, len));
    off += len;
  }
  const i16 = new Int16Array(ab, dataOffset, nSeries * nPoints);
  // Decode each series into its own Float32Array for the caller.
  // (Zero-copy isn't possible because we need Float for downstream
  // writeSeriesInto / uPlot.)
  const series = {};
  const SCALE = 1 / 32767;
  for (let s = 0; s < nSeries; s++) {
    const out = new Float32Array(nPoints);
    const base = s * nPoints;
    for (let i = 0; i < nPoints; i++) {
      const v = i16[base + i];
      out[i] = v === -32768 ? NaN : v * SCALE;
    }
    series[names[s]] = out;
  }
  return { series };
}

// --- Bench harness ----------------------------------------------------

function bench(name, iters, fn) {
  for (let i = 0; i < 50; i++) fn();
  const t0 = performance.now();
  for (let i = 0; i < iters; i++) fn();
  const t1 = performance.now();
  return { name, iters, totalMs: t1 - t0, nsPerOp: ((t1 - t0) * 1e6) / iters };
}
function fmt(b) {
  return `${b.name.padEnd(46)} ${b.iters.toString().padStart(6)} iters  ${b.totalMs.toFixed(2).padStart(8)} ms total  ${b.nsPerOp.toFixed(0).padStart(8)} ns/op`;
}

const jsonPayload = buildJsonPayload();
const binNaive = buildBinaryNaive();
const binQuant = buildBinaryQuantized();

console.log(`payload sizes:`);
console.log(`  JSON      : ${(jsonPayload.length / 1024).toFixed(1)} KB`);
console.log(`  binNaive  : ${(binNaive.byteLength / 1024).toFixed(1)} KB  (${(binNaive.byteLength * 100 / jsonPayload.length).toFixed(1)}% of JSON)`);
console.log(`  binQuant  : ${(binQuant.byteLength / 1024).toFixed(1)} KB  (${(binQuant.byteLength * 100 / jsonPayload.length).toFixed(1)}% of JSON)`);
console.log('');

const benches = [
  bench('JSON.parse(payload)',                ITERS, () => { JSON.parse(jsonPayload); }),
  bench('decodeBinaryNaive (zero-copy F32)',  ITERS, () => { decodeBinaryNaive(binNaive); }),
  bench('decodeBinaryQuantized (i16 -> F32)', ITERS, () => { decodeBinaryQuantized(binQuant); }),
];

for (const b of benches) console.log(fmt(b));

// --- Sanity ----------------------------------------------------------
const json = JSON.parse(jsonPayload);
const naive = decodeBinaryNaive(binNaive);
const quant = decodeBinaryQuantized(binQuant);
console.log('');
console.log(`sanity: JSON first 3 of combined_error    = ${JSON.stringify(json.series.combined_error.slice(0, 3))}`);
console.log(`        naive first 3 of combined_error   = ${JSON.stringify(Array.from(naive.series.combined_error.slice(0, 3)))}`);
console.log(`        quant first 3 of combined_error   = ${JSON.stringify(Array.from(quant.series.combined_error.slice(0, 3)))}`);

// --- Summary at 12 cards x 10 fps -------------------------------------
const FPS = 10, CARDS = 12;
console.log('');
console.log(`At ${CARDS} cards x ${FPS} fps:`);
for (const b of benches) {
  const msPerSec = (b.nsPerOp * FPS * CARDS) / 1e6;
  console.log(`  ${b.name.padEnd(46)} ${msPerSec.toFixed(1).padStart(6)} ms/sec main-thread`);
}
const jsonBw = (jsonPayload.length * FPS * CARDS) / 1024;
const naiveBw = (binNaive.byteLength * FPS * CARDS) / 1024;
const quantBw = (binQuant.byteLength * FPS * CARDS) / 1024;
console.log(`bandwidth:`);
console.log(`  JSON      : ${jsonBw.toFixed(0).padStart(6)} KB/s`);
console.log(`  binNaive  : ${naiveBw.toFixed(0).padStart(6)} KB/s`);
console.log(`  binQuant  : ${quantBw.toFixed(0).padStart(6)} KB/s`);
