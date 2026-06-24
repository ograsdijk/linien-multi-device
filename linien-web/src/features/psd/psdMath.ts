import type { PsdCurvePoint } from '../../types';

// Tick label for a log axis: plain within a readable range, scientific notation
// outside it (so values below ~1e-3 don't render as "0").
export function formatLogTick(v: number): string {
  if (!Number.isFinite(v)) return '';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e-3 && a < 1e5) return String(+v.toPrecision(3));
  return v.toExponential(0);
}

// Mirror of the gateway's flo_to_max_decimation (session.py). f_lo sets how
// deep the device sweeps (decimations 0,4,8,… up to this), so a higher f_lo
// makes each acquisition much faster.
export function floToMaxDecimation(fLo: number, minDec = 8, maxDec = 24): number {
  if (!fLo || fLo <= 0) return maxDec;
  const need = (125e6 * 10) / (16384 * fLo);
  let d = need > 1 ? Math.ceil(Math.log2(need)) : 0;
  d = Math.ceil(d / 4) * 4; // round up to a multiple of 4
  return Math.min(Math.max(d, minDec), maxDec);
}

// Rough wall-clock estimate for a sweep to `maxDec`: each decimation fills a
// 16384-sample buffer at fs = 125 MHz / 2^d (plus ~0.1 s/step overhead).
export function approxRunSeconds(maxDec: number): number {
  let s = 0;
  for (let d = 0; d <= maxDec; d += 4) {
    s += 16384 / (125e6 / 2 ** d) + 0.1;
  }
  return s;
}

export function formatRunTime(seconds: number): string {
  if (seconds < 1) return `~${seconds.toFixed(1)} s`;
  if (seconds < 90) return `~${Math.round(seconds)} s`;
  return `~${(seconds / 60).toFixed(1)} min`;
}

export function lowestFreqForMaxDecimation(maxDec: number): number {
  // LPSD low-frequency edge of the deepest decimation: (125e6/2^d)/16384*10.
  return (125e6 / 2 ** maxDec / 16384) * 10;
}

// Client-side band-limited integrated RMS (V) from a stitched ASD curve,
// mirroring DeviceSession._curve_rms so the table updates instantly as the band
// changes (no re-acquisition). Curve is ascending in f; psd is V/Sqrt[Hz].
export function bandRms(
  curve: PsdCurvePoint[] | undefined,
  fLo: number | null,
  fHi: number | null
): number | null {
  if (!curve || curve.length < 2) return null;
  const f = curve.map((p) => p.f);
  const asd = curve.map((p) => p.psd);
  const lo = fLo == null ? f[0] : Math.max(fLo, f[0]);
  const hi = fHi == null ? f[f.length - 1] : Math.min(fHi, f[f.length - 1]);
  if (!(hi > lo)) return null;

  const interp = (x: number): number => {
    if (x <= f[0]) return asd[0];
    if (x >= f[f.length - 1]) return asd[asd.length - 1];
    let k = 1;
    while (k < f.length && f[k] < x) k++;
    const t = (x - f[k - 1]) / (f[k] - f[k - 1]);
    return asd[k - 1] + t * (asd[k] - asd[k - 1]);
  };

  const fb: number[] = [lo];
  const ab: number[] = [interp(lo)];
  for (let k = 0; k < f.length; k++) {
    if (f[k] > lo && f[k] < hi) {
      fb.push(f[k]);
      ab.push(asd[k]);
    }
  }
  fb.push(hi);
  ab.push(interp(hi));

  let variance = 0;
  for (let k = 1; k < fb.length; k++) {
    const df = fb[k] - fb[k - 1];
    variance += df * 0.5 * (ab[k - 1] ** 2 + ab[k] ** 2);
  }
  return variance >= 0 ? Math.sqrt(variance) : null;
}
