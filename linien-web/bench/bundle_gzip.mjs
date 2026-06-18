// Measure pre-gzip ratios for each chunk in dist/, and the in-frame
// savings from the gateway-side combined_error->error_signal_1 alias.

import fs from 'node:fs';
import path from 'node:path';
import { gzipSync } from 'node:zlib';

// --- #17 pre-gzip bundle ----------------------------------------------

const distRoot = path.resolve('dist/assets');
const entries = fs.readdirSync(distRoot)
  .map((f) => ({ name: f, full: path.join(distRoot, f) }))
  .filter((e) => fs.statSync(e.full).isFile())
  .map((e) => {
    const raw = fs.readFileSync(e.full);
    const gz = gzipSync(raw, { level: 9 });
    return { name: e.name, raw: raw.length, gz: gz.length };
  })
  .sort((a, b) => b.raw - a.raw);

console.log('='.repeat(90));
console.log('#17  Pre-gzipped bundle (level 9)');
console.log('='.repeat(90));
console.log('  file                                  raw KB    gz KB    ratio');
let totalRaw = 0, totalGz = 0;
for (const e of entries) {
  totalRaw += e.raw;
  totalGz += e.gz;
  console.log(
    `  ${e.name.padEnd(40)} ${(e.raw / 1024).toFixed(1).padStart(8)} ${(e.gz / 1024).toFixed(1).padStart(8)}  ${(e.raw / e.gz).toFixed(2).padStart(5)}x`
  );
}
console.log(`  ${'-'.repeat(40)} ${'-'.repeat(8)} ${'-'.repeat(8)} ${'-'.repeat(6)}`);
console.log(
  `  ${'TOTAL'.padEnd(40)} ${(totalRaw / 1024).toFixed(1).padStart(8)} ${(totalGz / 1024).toFixed(1).padStart(8)}  ${(totalRaw / totalGz).toFixed(2).padStart(5)}x`
);
console.log();
console.log('  Effect: serving dist/* with Content-Encoding: gzip would cut');
console.log(`  initial-page transfer by ${((totalRaw - totalGz) / 1024).toFixed(0)} KB`);
console.log(`  (${(100 * (1 - totalGz / totalRaw)).toFixed(0)}% smaller on the wire).`);
console.log();

// --- #15 gateway-side alias savings ----------------------------------

console.log('='.repeat(90));
console.log('#15  Gateway-side combined_error <- error_signal_1 alias');
console.log('='.repeat(90));
const N_POINTS = 2048;
const BYTES_PER_FLOAT = 4;
const FPS = 10;
const CARDS = 12;
const seriesBytes = N_POINTS * BYTES_PER_FLOAT;
const wirePerSec = seriesBytes * FPS * CARDS;
console.log(`  combined_error series bytes:        ${seriesBytes} (= ${N_POINTS} pts * ${BYTES_PER_FLOAT} bytes)`);
console.log(`  per-frame bandwidth saved:          ${seriesBytes} bytes`);
console.log(`  per-second bandwidth saved (12x10): ${(wirePerSec / 1024).toFixed(0)} KB/s`);
console.log(`  per-frame encode saved:             ~one .tobytes() of 2048 floats = ~2 us`);
console.log(`  per-frame decode saved (worker):    Float32Array view drop ~0 us`);
console.log();
console.log('  Win is mostly bandwidth; CPU effect on either side is ~0.');
console.log('  Only fires in sweep mode (not lock mode) since the alias only');
console.log('  makes sense when combined_error equals error_signal_1 (= single-channel).');
