# rp-telemetry troubleshooting

## The board reboots when a temperature is requested

**Symptom.** `rp-telemetry` installs and starts fine. The board stays up while
both it and `linien-server` are running. The moment a STATUS request reaches the
daemon, the board resets. Some boards never show it; on the boards that do, it is
reproducible. No pre-crash logs survive (`journalctl --list-boots` shows only
boot 0 — the Red Pitaya image has no persistent journal).

**Cause.** A Red Pitaya exposes **two** IIO devices, and both are named `xadc`:

```
/sys/bus/iio/devices/iio:device0  name=xadc  ->  /sys/devices/soc0/axi/f8007100.adc/iio:device0
/sys/bus/iio/devices/iio:device1  name=xadc  ->  /sys/devices/soc0/axi/83c00000.xadc_wiz/iio:device1
```

- `f8007100.adc` is the **PS XADC** — part of the Zynq processing system. Always
  present, always safe, unaffected by anything the FPGA does.
- `83c00000.xadc_wiz` is an **XADC wizard core inside the FPGA bitstream**, reached
  over the AXI bus at `0x83c00000`.

`linien-server` reprograms the FPGA on start. The replacement bitstream has
nothing at `0x83c00000`, but the *device node* does not go away — it comes from
the device tree, which is static. So the sysfs file still looks perfectly valid,
and reading `in_temp0_raw` from it issues an AXI access that nothing answers. The
bus hangs and the watchdog resets the board.

Version 1.0.0 of the daemon took whichever entry `readdir()` returned first with
an `in_temp0_raw`. `readdir()` on sysfs is unordered (kernfs hashing and probe
order), so which of the two devices was chosen was effectively a coin flip —
fixed per board, which is why some boards were fine and others reset every time.
And not every board's device tree instantiates the wizard at all; those boards
only ever had the safe device to find.

It explains the timing exactly: `in_temp0_offset` and `in_temp0_scale` are read
once at startup and are plain sysfs attributes, so starting the daemon is
harmless. Only `in_temp0_raw` touches the hardware, and only per request.

**Fix (daemon 1.1.0).** Discovery now resolves each IIO device's real path and
classifies it:

- path contains `f8007100` → the PS XADC, used;
- path contains `adc_wiz` → FPGA-backed, **refused outright** and logged;
- anything else → also refused on a real board, because matching on `adc_wiz`
  only catches the wizard by the name Xilinx's tooling happens to give it, and
  a PL peripheral under any other name would reset the board just as
  thoroughly. `ERR XADC` is the right answer for a board we do not recognise;
- unresolvable → refused, on the same principle.

That restriction applies when scanning the real `/sys/bus/iio/devices`. A
caller that passes `--iio-root` somewhere else (the test fixtures, unusual
hardware) takes responsibility for what it points the daemon at and gets the
permissive behaviour.

At startup the daemon prints what it restricted itself to and what it settled
on:

```
rp-telemetry: restricting discovery to the PS XADC (f8007100)
rp-telemetry: ignoring FPGA-backed XADC /sys/bus/iio/devices/iio:device1 (reading it can hang the AXI bus)
rp-telemetry: reading temperature from /sys/bus/iio/devices/iio:device0/in_temp0_raw
```

A fixed handful of lines at startup, nothing per request -- refusals are
latched, so a board that never finds a usable XADC does not accumulate a
warning per poll.

### Checking a board

Which devices exist, and what they really are:

```sh
for d in /sys/bus/iio/devices/*; do
  echo "$d  name=$(cat $d/name 2>/dev/null)  ->  $(readlink -f $d)"
done
```

Confirm the daemon picked the right one (substitute the real index — `deviceN`
is a placeholder):

```sh
systemctl status rp-telemetry --no-pager -l
journalctl -u rp-telemetry -n 20 --no-pager
```

Read the safe device by hand:

```sh
cat /sys/devices/soc0/axi/f8007100.adc/iio:device*/in_temp0_raw
```

**Do not** `cat` an `in_temp0_raw` under `83c00000.xadc_wiz` while
`linien-server` is running. That is the thing that reboots the board, and it will
do it from the shell just as readily as from the daemon.

Check the deployed version:

```sh
printf 'VERSION\n' | nc 127.0.0.1 18864     # expect: RPT1 VERSION 1.1.0
```

A board still answering `1.0.0` is running the unsafe discovery — reinstall it
from the gateway's telemetry panel (Update).
