# TODO

## Autolock Hysteresis Safety — shipped, needs hardware validation

Implemented on `feat/autolock-hysteresis`. See the "Guarded center move" section in
[README.md](README.md) for what it does and how it is configured.

Remaining, and only doable at a board:

1. Run **Measure hysteresis** on a real device and record the verdict.
2. Set `approach_offset_v` and `settle_ms` from that measurement — the shipped
   defaults are placeholders, not measurements.
3. Confirm an `exposed_write_registers()` round trip is fast enough for the ramp
   cadence you end up wanting (`ramp_step_delay_ms`, and the number of steps the
   offset/step ratio implies).
4. Enable the guarded move per device once its numbers are known. It is off by
   default, so until then every device keeps the plain direct set.
