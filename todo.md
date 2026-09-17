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

## XADC undervoltage alarms — investigated, not implemented

The one mechanism that could actually catch a supply droop behind the
unexplained reboots. `vccaux` telemetry (daemon 1.4.0) samples once per 30 s
poll, so it shows slow drift and board-to-board differences and **cannot** see a
millisecond dip. The XADC's alarm comparators run continuously in hardware and
can.

Everything needed is already exposed on the safe PS device
(`/sys/bus/iio/devices/iio:device0/events/`), for all six real rails — `vccint`,
`vccaux`, `vccbram`, `vccpint`, `vccpaux`, `vccoddr`:

```
in_voltage<N>_<rail>_thresh_falling_value
in_voltage<N>_<rail>_thresh_rising_value
in_voltage<N>_<rail>_thresh_either_en
```

### Driver semantics (read out of xilinx-xadc-events.c, worth not rediscovering)

- Threshold values are **raw 12-bit codes**, the same units as `_raw`. Convert
  the same way the daemon already does: `code = volts / scale_mV * 1000`.
- The enable is **shared** between directions. To watch only for undervoltage,
  park `thresh_rising_value` at 4095 and set only `thresh_falling_value`, then
  write `thresh_either_en`.
- The event is delivered with direction `EITHER` — the kernel does not say which
  way the rail crossed, so the daemon has to read the rail to classify it.
- On Zynq the driver masks an active alarm and re-arms once it clears, so a
  sagging rail produces **one event per excursion**, not a storm.
- Zynq requires an IRQ for events at all ("no IRQ => no events"). The presence
  of the `events/` attributes is the evidence that it has one.

### Why it was not done with the rail telemetry

The daemon spends its entire life blocked in `accept()` — no timer, no thread,
no polling loop. That is an explicit design goal in its header, and the reason
it costs nothing on a board whose CPU belongs to `linien-server`. Catching
events means:

1. Restructuring the accept loop into a `poll()` over the listener plus an IIO
   event fd, preserving the existing `EINTR` / `ECONNABORTED` / `EMFILE`
   handling and the `g_stop` signal path.
2. `ioctl(fd, IIO_GET_EVENT_FD_IOCTL, &efd)` on `/dev/iio:deviceN`, then reading
   `struct iio_event_data { __u64 id; __s64 timestamp; }` from it. Note the
   char device is **not** derivable from the sysfs path the daemon already
   holds; discovery has to find it.
3. `<linux/iio/events.h>` and `<sys/ioctl.h>` do not exist on macOS, so the
   whole path needs `#ifdef __linux__` guards. `test_rp_telemetry_daemon.py`
   compiles the daemon natively, so **the new code could not be exercised by
   the local suite at all** — only on Linux CI, and the fake sysfs tree has no
   char device to open. That testability gap is the real cost, not the lines.

On the wire it stays cheap: one compact field on the existing STATUS tail
(a per-rail trip count, or a bitmask plus a count). A full response is ~105
bytes against `MAX_RESPONSE` 256.

### Before writing any of it

- Read the nominal raws off one board to pick thresholds — explicitly, never
  through a glob (`cat /sys/bus/iio/devices/iio:device0/in_voltage1_vccaux_{raw,scale}`;
  a glob read includes the PL device and reboots the board).
- **Unverified:** whether enabling alarms from the PS conflicts with the PL XADC
  wizard, which drives the same hardware block. Try it on one spare board before
  rolling anything out.
- Decide what a trip should do beyond the counter: a recorded board event in the
  timeline would timestamp it, rather than leaving a number that went up between
  two polls.
