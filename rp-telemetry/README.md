# rp-telemetry

A very small C daemon that reports the **Zynq die (junction) temperature** of a
Gen 1 Red Pitaya STEMlab 125-14 over a one-line TCP protocol.

It exists because CPU time on those boards is scarce and is needed by
`linien-server`. The daemon spends its entire life blocked in `accept()`: there
is no polling loop, no timer, no thread, no HTTP, no JSON, no Python, and no
InfluxDB client. A request costs one `accept()`, one `recv()`, one
open/read/close of a single sysfs file, one `send()`, and one `close()` — about
once every 30 seconds.

See the repo README section **Red Pitaya telemetry** for the gateway/UI side.

## Protocol

```text
->  STATUS\n      <-  RPT1 57.34 cpu=3.2 load1=0.41 memtotal=509216
                          memavail=311044 uptime=690.2 rootfree=1204880
                          vccaux=1.802\n
                          (one line; wrapped here to fit)
                  <-  RPT1 ERR XADC\n       sysfs read failed
->  VERSION\n     <-  RPT1 VERSION 1.4.0\n
->  anything else <-  RPT1 ERR COMMAND\n
```

The temperature (°C) is the first field and always in the same place. What
follows it is an optional tail of `key=value` host metrics, added in 1.2.0:

| key | meaning |
| --- | --- |
| `cpu` | busy percent **since the previous STATUS request** |
| `load1` | 1-minute load average |
| `memtotal`, `memavail` | kB; `memavail` is the kernel's MemAvailable (MemFree on kernels too old to have it) |
| `uptime` | seconds since boot |
| `rootfree` | free kB on the root filesystem (the SD card) |
| `vccaux` | FPGA auxiliary rail in volts, nominally 1.8 V, from the PS XADC (1.4.0+); see below |

Each key is independently optional — a metric the board could not read is left
out rather than sent as zero — and a reader must ignore keys it does not know.
That is why this is a tail rather than a new command: a client written against
1.1.0 parses the temperature from these lines unchanged.

One request per connection; the daemon closes the socket afterwards and returns
to `accept()`. Requests are capped at 64 bytes and accepted sockets carry a 2 s
receive timeout, so a silent or over-chatty client cannot pin the daemon.

Default port: **18864** (`--port` to change).

## Temperature source

The Zynq XADC via Linux IIO. At startup the daemon scans
`/sys/bus/iio/devices/` for the directory containing `in_temp0_raw` — the
device index is *not* hard-coded — and reads the constant `in_temp0_offset` and
`in_temp0_scale` once. Per request only `in_temp0_raw` is re-read:

```text
temperature_c = (raw + offset) * scale / 1000.0
```

If the sysfs entries are missing or unreadable, the daemon answers
`RPT1 ERR XADC` and retries discovery on the next request rather than crashing.

## Building

### Cross-compile for the Red Pitaya (recommended)

Needs either Docker or [zig](https://ziglang.org/download/); no ARM toolchain
on your machine. Works from Linux, macOS, and Windows (Git Bash).

```bash
cd rp-telemetry
./build-arm.sh
```

Docker is used when its daemon is reachable, zig otherwise; force one with
`BUILD_BACKEND=zig` / `BUILD_BACKEND=docker`. Docker builds against glibc and
zig against musl, both static. The daemon uses nothing but file I/O and
sockets — no NSS, locale, or `dlopen` — so the two are interchangeable here.

> **Rebuild whenever `RPT_VERSION` changes.** The gateway decides a board is
> up to date by comparing the version the board reports against
> `BUNDLED_VERSION`. If the committed binary is older than that constant,
> installing deploys the old daemon, the board keeps reporting the old
> version, and the UI shows an "update available" banner that reinstalling
> never clears.

Output:

* `rp-telemetry/build/rp-telemetry-armv7`
* `linien-gateway/app/assets/rp-telemetry-armv7` — where the gateway looks for
  the binary it deploys.

The ARM build is **statically linked** (`-static`): Red Pitaya images in the
field carry several different glibc versions, and static linking removes that
variable for ~700 KB of flash the board can spare.

### With a local toolchain

```bash
make bundle          # arm-linux-gnueabihf-gcc -> app/assets/
make arm             # arm build only
```

### Native build (for the tests)

```bash
make                 # build/rp-telemetry, host architecture
```

`linien-gateway/tests/test_rp_telemetry_daemon.py` compiles the daemon for the
host and runs it against a fake IIO tree. Those tests skip automatically when
no POSIX C compiler is present (e.g. a Windows dev box).

## Running it by hand

```bash
./build/rp-telemetry --port 18864
./build/rp-telemetry --port 18864 --iio-root /tmp/fake-iio    # for testing
./build/rp-telemetry --version
```

Query it:

```bash
printf 'STATUS\n' | nc <red-pitaya-host> 18864
```

```text
RPT1 57.34 cpu=3.2 load1=0.41 memtotal=509216 memavail=311044 uptime=690.2 rootfree=1204880
```

## Deployment

Deploy through the gateway UI rather than by hand — see the repo README. The
gateway uploads the binary over SSH, installs it atomically at
`/usr/local/bin/rp-telemetry`, writes `rp-telemetry.service`, enables it at
boot, starts it, and verifies the protocol answers.

## Rail voltage (`vccaux`)

The daemon reports the FPGA auxiliary rail, nominally 1.8 V, from **the same PS
XADC directory it already chose for the temperature**. It never scans other
devices for the channel, so the PS-only rule below applies unchanged. The scale
is mV per LSB and is read once at discovery; the raw value is read per request.

```text
vccaux = raw * scale / 1000        (no divider: an internal rail is direct)
```

The channel is matched by **name suffix** (`*_vccaux_raw`), not by a fixed
index — see "Why not the 5 V input" below for why that matters. If the channel
is missing, or a value falls outside 0.5–3.0 V (a garbled read, not a health
judgement), `vccaux` is simply omitted.

What it is worth: `vccaux` is a *regulated output*, so a 5 V input that sags a
little is hidden by the regulator, and one sample per STATUS request (~30 s)
shows only slow drift and differences between boards. It **cannot** see
millisecond brownouts, so a steady reading does not rule out a transient droop.

To check a board by hand — note the explicit device, never a glob:

```bash
# Confirm which device is the PS XADC first (safe: resolves a symlink only).
readlink /sys/bus/iio/devices/iio:device0
# Then read only that one.
cat /sys/bus/iio/devices/iio:device0/in_voltage1_vccaux_{raw,scale}
```

> **Never `cat` or `grep` across `/sys/bus/iio/devices/*/`.** The glob includes
> the FPGA-backed device, and reading it hangs the AXI bus and reboots the
> board. Listing with `ls` is safe; reading is not.

## Why not the 5 V input

Version 1.3.0 tried to report the board's +5 V input, which on the Gen 1
STEMlab 125-14 reaches the XADC's VP/VN pair through a 56.0 kΩ / 4.99 kΩ
divider. It reported nothing on every board, for two independent reasons:

1. The kernel's `xilinx-xadc` driver declares VP/VN with no name suffix, so the
   attribute would be `in_voltage8_raw` — the `in_voltage8_vpvn_raw` the daemon
   looked for cannot exist.
2. More fundamentally, the PS XADC exposes **only** the internal rails
   (`vccint`, `vccaux`, `vccbram`, `vccpint`, `vccpaux`, `vccoddr`, `vrefp`,
   `vrefn`) plus temperature. External channels are created only when the
   devicetree declares an `xlnx,channels` node, and these images declare none.

The external channels do appear on the PL XADC wizard — the device that reboots
the board when read. Exposing VP/VN safely would need a devicetree change in
each board's boot partition, redone after every image update. Not worth it; see
TROUBLESHOOTING.md.

## Which XADC it reads

A Red Pitaya exposes two IIO devices, both named `xadc`: the processing-system
XADC at `f8007100.adc`, and an XADC wizard core at `83c00000.xadc_wiz` that
lives inside the FPGA bitstream. Reading the second one after `linien-server`
has reprogrammed the FPGA is an AXI access nothing answers — the bus hangs and
the watchdog reboots the board. The daemon therefore identifies the device by
its resolved sysfs path and, on a real board, reads nothing but the PS XADC;
anything else answers `ERR XADC` rather than risk a reset. A `--iio-root`
override lifts that restriction, for the tests and for other hardware. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Security

The daemon answers only the fixed protocol above. There is no code path that
executes anything a client supplies. It binds all interfaces by default because
the gateway needs LAN access, and — like the rest of this repo — it assumes a
trusted, isolated lab network. Do not expose port 18864 to untrusted networks.
