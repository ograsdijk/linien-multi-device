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
                          v5=4.987\n
                          (one line; wrapped here to fit)
                  <-  RPT1 ERR XADC\n       sysfs read failed
->  VERSION\n     <-  RPT1 VERSION 1.3.0\n
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
| `v5` | board +5 V supply in volts, from the XADC VP/VN pair (1.3.0+); see below |

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

Needs Docker; no ARM toolchain on your machine. Works from Linux, macOS, and
Windows (Git Bash).

```bash
cd rp-telemetry
./build-arm.sh
```

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

## Supply voltage (`v5`)

On the Gen 1 STEMlab 125-14 the +5 V input is wired to the XADC's dedicated
VP/VN pair through a 56.0 kΩ / 4.99 kΩ divider. The daemon reads
`in_voltage8_vpvn_raw` and `in_voltage8_vpvn_scale` from **the same PS XADC
directory it already chose for the temperature**. It never scans other devices
for the channel, so the PS-only rule below applies unchanged. The scale is
mV per LSB (1000 / 4096 on the kernel's xilinx-xadc driver) and is read once
at discovery; the raw value is read per request.

```text
v5 = raw * scale / 1000 * (56000 + 4990) / 4990      (~ raw * 0.002984 V)
```

If the channel is missing, or a value falls outside 3.0–6.5 V (a garbled read,
not a health judgement), `v5` is simply omitted. One sample per STATUS request
(~30 s) shows static or slowly drifting supply levels and differences between
boards, ports and cables. It **cannot** see millisecond brownouts, so a steady
reading does not rule out a transient droop.

To check a board by hand:

```bash
grep -H . /sys/bus/iio/devices/iio:device*/in_voltage8_vpvn_*
readlink -f /sys/bus/iio/devices/iio:device*   # use only the f8007100 one
```

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
