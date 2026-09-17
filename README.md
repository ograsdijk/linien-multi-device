# Linien Multi-Device

Monorepo with a FastAPI gateway and a React (Vite) web UI for controlling multiple
Linien laser-lock devices from one interface.

> [!IMPORTANT]
> This gateway has **no authentication** and, in the documented configuration, binds
> to all network interfaces and returns device credentials in cleartext. Deploy it
> only on a trusted, isolated lab network or behind an authenticating reverse proxy.
> See [Security model](#security-model) and [Known issues](#known-issues).

## Highlights

- Multi-device management with groups, drag-and-drop ordering, and shared live state.
- Live plotting over WebSockets with an off-main-thread stream parser worker and an
  optional binary frame protocol.
- Web-native locking workflow:
  - `Manual` lock tab.
  - `Autolock` (scan-based target detection + lock), including calibration of the
    scan settings from a live PDH error trace.
  - `Autolock dev` (legacy selection flow) and the slope-selection `Optimization`
    flow are currently compatibility-disabled — see [Known limitations](#known-limitations).
- Lock quality tooling:
  - configurable lock indicator (error / control / monitor based),
  - per-frame signal statistics (mean/std control voltage, error and monitor stats)
    exposed over REST `status()` and the plot stream — computed whenever the device is
    locked, independent of the lock indicator (so disabling the indicator does not hide
    the control-voltage readout used for orchestration / recentering),
  - auto-relock controller with configurable trigger / verify / cooldown behavior.
- Broadband PSD (power-spectral-density) noise-spectrum acquisition: a server-side
  measurement on locked devices, streamed live to a viewer and tailable over REST.
- Connection-drop diagnosis: when a device drops, the gateway probes it out-of-band
  (TCP + SSH) to classify the cause (crash vs reboot vs unreachable) and infer whether
  the hardware lock is likely still held.
- Multi-device operations: a device overview grid, simultaneous (multi-device) sweep,
  fresh-trace acquisition, and per-device sweep-speed control.
- Optional lock logging to Postgres (`pdh_lock_results`) for manual, auto-lock-from-scan,
  and auto-relock actions.
- Optional InfluxDB logging control from the UI (credentials, interval, loggable
  parameter multiselect), resumed on reconnect.
- Red Pitaya die-temperature telemetry: a near-zero-CPU C helper on the board,
  polled over a tiny TCP protocol, shown per device and logged to InfluxDB by the
  gateway.
- In-app logs: tail, clear, and a live structured log-event stream surfaced as toasts.
- Board diagnostics: a retained per-device timeline of reboots and outages, an
  on-demand post-mortem bundle pulled from the board, and one-click persistent
  journald so the next crash leaves evidence behind.

## Repo structure

- `linien-gateway`: FastAPI backend (Python).
- `linien-web`: React UI (Vite + TypeScript).
- `linien-sim`: virtual Linien-compatible simulator for local testing.
- `rp-telemetry`: tiny C daemon deployed to each Red Pitaya to report its Zynq die
  temperature (see [Red Pitaya telemetry](#red-pitaya-telemetry-zynq-die-temperature)).
- `docker/`: optional Docker stacks (gateway + UI; Postgres + pgAdmin).

## Architecture overview

- The gateway keeps one long-lived **session** per device (`app/session.py`). A session
  owns the RPyC connection to the Linien server, a background poll thread, the persistent
  settings snapshot, and the per-device lock/auto-relock/diagnosis state.
- The web UI talks to the gateway over a REST API (`/api/...`) for control and over
  per-device WebSockets (`/api/devices/{key}/stream`) for live plot/status frames.
  Separate WebSockets carry structured log events (`/api/logs/stream`) and live PSD
  noise-spectrum results (`/api/psd/stream`).
- Plot frames can be sent as JSON or, when the client requests `binary=1`, as a compact
  binary frame decoded in a Web Worker (`src/workers/streamParserWorker.ts`).
- Interactive API docs are available at `/docs` (FastAPI / OpenAPI) when the gateway is
  running.

## Prerequisites

- Python 3.10+
- Node.js 20 (the Docker build and `package-lock.json` target Node 20; 18 may work but
  is not what the build is validated against).
- [`uv`](https://docs.astral.sh/uv/) is recommended for the gateway. The project pins
  `numpy>=2` and reconciles it against `linien-common`/`linien-client 2.1.0` via a
  `[tool.uv]` override; a plain `pip install` does **not** honor that override and will
  fail to resolve (see [Known issues](#known-issues)).
- The connection-diagnosis feature needs `fabric`/`paramiko` (pulled in transitively via
  `linien-client`) and SSH access (`root@<device>`) to each Red Pitaya.

## Configuration

### Repo-root `config.json`

Network and plot-stream settings, read by the gateway at startup:

```json
{
  "apiHost": "0.0.0.0",
  "apiPort": 8000,
  "webDevPort": 5175,
  "plotStreamDefaultFps": 60,
  "plotStreamMaxFpsCap": 60,
  "plotStreamDropOldFrames": true
}
```

- `apiHost`: FastAPI bind host (`0.0.0.0` for LAN access — read the [Security model](#security-model) first).
- `apiPort`: FastAPI port.
- `webDevPort`: Vite dev server port.
- `plotStreamDefaultFps`: applied when a client doesn't provide `max_fps`.
- `plotStreamMaxFpsCap`: hard upper cap applied to all client `max_fps` values.
- `plotStreamDropOldFrames`: when `true`, each socket keeps only the newest pending plot frame.

> Note: `config.json` is only fully honored by `python linien-gateway/run.py`. The
> installed `linien-gateway` console script currently hardcodes `0.0.0.0:8000` and
> ignores `config.json` (see [Known issues](#known-issues)).

### Runtime / user-data files (not committed)

- `device_settings.json` (repo root): per-device settings persisted by the gateway:
  - `auto_lock_scan_settings`
  - `lock_indicator_config`
  - `auto_relock_config`

  This file is **gitignored and created at runtime** — do not expect it in a fresh
  checkout. These settings are broadcast to all connected clients for the same device
  via WebSocket `config_update` events.
- `board_events.json` (repo root): the per-device board/server timeline (see
  [Board diagnostics](#board-diagnostics)). Also gitignored and created at
  runtime; capped at 200 events and 30 days per device.
- The Linien client also persists a device list (`devices.json`) and
  `manual_lock_postgres.json` under `linien_common.config.USER_DATA_PATH`.
- In addition to the three config blocks above, the gateway snapshots a set of
  *restorable* Linien server parameters per device and replays them on reconnect.

## Development

### Backend (FastAPI)

```powershell
python .\linien-gateway\run.py
```

This entrypoint reads `config.json` (default bind `127.0.0.1:8000`).

### Frontend (Vite)

```powershell
cd linien-web
npm install
npm run dev
```

Default dev UI URL is `http://localhost:5175`.
If the UI should target a different gateway host, set `VITE_API_URL`
(for example `http://192.168.1.10:8000/api`).

## Serve pre-built UI from FastAPI

```powershell
cd linien-web
npm install
npm run build

cd ..\linien-gateway
python -m uvicorn app.main:app --reload
```

Open `http://localhost:8000` for the UI and `/api` for backend endpoints
(`/docs` for interactive API docs).

For LAN access (run from the `linien-gateway` directory so `app.main:app` resolves):

```powershell
cd linien-gateway
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Locking workflows

- **Manual lock** — pick a target on the live trace and engage the PID lock.
- **Autolock (scan-based)** — the gateway sweeps, detects a lockable crossing from the
  error/monitor traces, moves to it, and locks. The scan settings can be **calibrated**
  from a live unlocked PDH error trace
  (`POST /api/devices/{key}/control/auto_lock_scan/calibrate`).
- **Detect-without-lock** — `POST /api/devices/{key}/control/auto_lock_candidates`
  reports whether a lockable target exists (and which) without engaging the lock; useful
  for orchestration / offset stepping.
- **Disabled flows** — the legacy selection-driven `Autolock dev` and the slope-selection
  `Optimization` / PID-optimization flows are gated off at runtime
  (`AUTOMATION_TEMP_DISABLED`); their endpoints and UI still exist but raise an error.
  See [Known limitations](#known-limitations).

## Guarded center move (autolock hysteresis safety)

On hysteretic piezo/cavity systems, commanding `sweep_center = V` does not put the
actuator at `V`. The detector picks the right crossing, but on the next sweep the
feature appears at `V + delta` — and the lock engages somewhere it should not.

That `delta` is measurable on the very next sweep, so the gateway measures it rather
than only guarding against it. **Disabled by default**; enable per device under the
`Autolock` tab.

What happens on `auto_lock_scan` when it is enabled:

1. **Direct probe.** Set the center to the detected target, exactly as before, then
   wait for a sweep acquired after the move and re-detect. If the crossing landed within
   the acceptance window, start the lock. This is the common path, and it costs a couple
   of sweeps of latency. (Two frames are required, not one: the freshness stamp records
   when the gateway *processed* a frame, not when the board *acquired* it, so a single
   frame can be one captured mid-ramp or before the settle finished.)
2. **Correct.** Otherwise the center is moved to where the feature actually appeared,
   via an anti-backlash move: overshoot by `approach_offset_v`, then ramp back onto the
   target in `ramp_step_v` steps so the final approach always travels the same way, and
   wait `settle_ms`. Re-verify, up to `max_approach_iterations` times.
3. **Flip direction.** If corrections on one side are spent, the whole thing repeats
   from the opposite direction.
4. **Abort.** Otherwise no lock is started, the sweep center is restored, and the
   reason is surfaced in the UI.

The acceptance window is **not** configured as a voltage. A lock succeeds whenever the
DC point lands between the two lobe extrema, and calibration already measures that
width into `half_range_sweep_v`, so the window is `capture_fraction` x that width and
is shown in volts next to the setting.

A re-detection further out than the neighbour guard **aborts instead of being
corrected** — that is a different crossing, and chasing it would walk the lock onto the
wrong feature. In PDH mode the bound comes from the scan's measured `sideband_offset_v`;
otherwise it falls back to `max_correction_span` feature widths.

On a closely spaced signal the configured window can reach more than halfway to the next
feature. Rather than widen the guard (which would leave no room to correct at all, and
report every miss as a neighbouring crossing), the **acceptance window is tightened** to
half the guard, and the abort message says so. The window shown in volts beside
`capture_fraction` is the configured one; the applied window is reported per-lock as
`capture_tolerance_v`, and the diagnostic judges by the same applied window so it cannot
call an offset negligible that the lock would then reject. Once a measurement has run,
the settings panel shows the applied window alongside the configured one whenever the
two differ.

Setting `max_correction_span = 0` **disables** the neighbour guard rather than rejecting
everything. A device with no calibrated `half_range_sweep_v` has no capture region to
derive at all, so the guarded move refuses with a prompt to calibrate instead of failing
every landing and blaming the signal.

### Setting the two physical tunables

`approach_offset_v` and `settle_ms` are properties of your actuator, so measure them
rather than guessing: **Measure hysteresis** (`POST
/api/devices/{key}/control/lock_approach/measure`) approaches the current target from
both directions at several settle times, without locking, and reports a verdict:

| verdict | what it means | what to change |
| --- | --- | --- |
| `backlash` | the offset flips sign with the approach direction | raise `approach_offset_v` above the measured width |
| `creep` | same offset either way, and it shrinks the longer you wait | raise `settle_ms` |
| `drift_or_creep` | same offset either way, and it does not shrink | try longer settles, or look for laser/cavity drift |
| `negligible` | both directions land inside the window | leave the guarded move off |
| `inconclusive` | too few measurements succeeded | check the sweep is running |

A single direction cannot distinguish backlash from creep, which is why the
diagnostic always measures both.

Each measurement is also written to `pdh_lock_results` with
`lock_source = "lock_approach_probe"`, so probes and locks trend together in one query.
`center_move_v` is 0 for a probe (it restores the center it found), `center_offset_v`
carries the largest excursion measured, and `approach_detail` holds every sample.

> **Not yet validated on hardware.** The control logic is tested against a simulated
> hysteretic actuator (backlash, creep, non-repeatable displacement, neighbouring
> crossings, a stopped sweep). The physical defaults are placeholders until the
> diagnostic has been run on a real board.

## Auto-relock

A per-device controller that re-establishes a lost lock. It is configured with
trigger / verify / cooldown behavior and exposes its state over REST and in the live
stream. Auto-relock-driven locks are logged to Postgres with `lock_source = "auto_relock"`.

## PSD (noise spectrum)

A server-side broadband power-spectral-density measurement of the locked error/control
signal, surfaced in the web UI as a noise-spectrum viewer.

- **Start / stop (single device)** — `POST /api/devices/{key}/control/start_psd_acquisition`
  (optional body: `algorithm`, `max_decimation`) and
  `POST /api/devices/{key}/control/stop_psd_acquisition`. Start returns immediately and
  `409`s if the laser isn't locked; the device acquires in the background.
- **Start / stop (simultaneous)** — `POST /api/control/start_psd_acquisition` and
  `POST /api/control/stop_psd_acquisition` over a set of device keys, triggered in
  parallel; unconnected/unlocked devices land in `skipped` rather than failing the batch.
- **Live results** — streamed over `ws /api/psd/stream` and reflected per device in
  `status()` as `psd_running`.
- **History** — `GET /api/psd/tail` (recent results, `limit` query param) and
  `DELETE /api/psd` to clear.

## Connection diagnosis

When a device's RPyC connection drops, a background probe (`app/diagnosis.py`) classifies
the cause out-of-band and surfaces it in the device status as a `diagnosis` object
(category + lock-state inference + message), rendered as a badge in the UI. The probe
needs SSH access to the Red Pitaya. Note that the gateway does **not** auto-reconnect — the
"recovering" wording is informational only, and reconnect is operator-driven.

## Board diagnostics

Connection diagnosis says *what* happened. This says *why*, and keeps a record.
Open it from **Diagnostics** on a device card.

### Timeline

The gateway retains each board's transitions in `board_events.json`: connection
losses, diagnosis changes, reboots, and telemetry outages and recoveries. It
survives a gateway restart, which is often itself part of the incident, so
"this board rebooted three times last night" is answerable the next morning.

Nothing new is polled to produce it. Every entry mirrors a log event the
gateway already emits exactly once per transition, so the poll paths are
untouched and the timeline cannot fill with repeats of a steady state.

Reboots are detected by comparing `/proc/sys/kernel/random/boot_id` between
probes. That is free — it rides along in a compound read `diagnosis.py` already
sends — and it is a fact rather than the previous 600 s uptime heuristic, which
was wrong in both directions: it missed a reboot on a board that had since been
up for a day, and it called a crash-on-a-freshly-booted-board a reboot and
wrongly declared the lock lost.

### Collect diagnostics

`POST /api/devices/{key}/diagnostics/collect` gathers, over one SSH connection:

- board identity, uptime and boot ID;
- whether the board keeps logs at all;
- `linien-server.service` state and exit status, including `NRestarts` and
  whether it died on a signal;
- its journal for this boot **and the previous one**;
- the kernel ring buffer, with watchdog/reset/OOM/panic lines pre-extracted;
- `pstore` crash remnants, memory, disk, load, and FPGA manager state;
- the `rp-telemetry` journal.

Every command is `timeout`-bounded and every section fails independently —
these images vary, and a missing tool is a finding, not a reason to lose the
other eleven sections. Collection is operator-triggered only, on its own small
SSH pool, so it can never queue ahead of a telemetry action or slow a status
endpoint. It is read-only.

### Enable persistent logs

**This is the one that matters, and it has to be done before the crash you want
to read about.** Stock Red Pitaya images keep the journal in RAM: after a reset
`journalctl -b -1` has nothing, and the pre-crash evidence is simply gone. No
amount of collecting recovers it retroactively.

`POST /api/devices/{key}/diagnostics/enable-persistent-log` writes a capped
`Storage=persistent` journald drop-in (32 MB, 8 MB per file — these are SD
cards), creates `/var/log/journal`, and restarts journald. The write is
checksum-verified and `sync`ed, with the same care the telemetry unit write
earned on real hardware.

On these images that is not enough on its own: `/var/log` is itself a 5 MB RAM
disk, so journald obeys `Storage=persistent`, cannot fit an 8 MB journal file
there, and falls back to logging in RAM — silently, leaving a board that looks
exactly like one that was never configured. So before restarting journald, the
action checks what filesystem is under `/var/log/journal` (from `/proc/mounts`,
not from `df`'s device column — a tmpfs is usually mounted as `none`), and when
it finds a RAM disk it puts real storage under the path: a
`var-log-journal.mount` unit bind-mounting `/var/log-persistent/journal` from
the root filesystem.

That unit has no `[Install]` section on purpose. `systemd-journal-flush.service`
carries `RequiresMountsFor=/var/log/journal`, which pulls the mount in *and*
orders it ahead of the flush at every boot, so nothing needs enabling. The
tidier-looking `WantedBy=local-fs.target` is a trap: it would make a failed
mount a boot failure, and a headless board in emergency mode needs a lab visit
with an SD reader. Pulled in only by the flush, a broken mount costs you the
journal and nothing else. (A boot-time symlink is not an option either — the
flush runs `Before=systemd-tmpfiles-setup.service`, so the link would not exist
yet and the whole boot would stay in RAM.)

It then asks journald which file it is *actually* writing to, rather than
checking that `/var/log/journal` exists — that directory is created by this
very action, so its presence proves nothing. The check retries for a few
seconds, because `journalctl --flush` returns before the flush has finished on
older systemd and a board configured correctly was being reported as a failure.
A board whose `Storage=` is still overridden by another drop-in reports failure
and says where to look, along with what the board itself reports: both journal
directories, the filesystem under them, every effective `Storage=` line with the
file it came from, and journald's state. `POST /api/diagnostics/enable-persistent-log` does a
set of boards at once, which is how you would want to do it the first time.

A board where even `/var` is a RAM disk is refused outright rather than
half-configured: there is nowhere to put a journal, and only a change to the
image can fix that.

The modal offers the action only when a collected bundle shows the board has no
persistent journal, and stops offering it once it does.

## Multi-device operations

- **Overview grid** — compact per-device cards with locked control/monitor history plots
  and a configurable overview frame rate.
- **Simultaneous sweep** — `POST /api/control/start_sweep` starts sweeps on a selected set
  of connected devices at roughly the same time, with an optional uniform `sweep_speed`.
- **Acquire scan** — `POST /api/devices/{key}/control/acquire_scan` and
  `POST /api/control/acquire_scan` capture a fresh sweep trace. (These are gateway-side
  endpoints with no current web-UI caller.)
- **Sweep speed** — per-device and applied across simultaneous sweeps.

## WebSocket streaming

- Per-device stream: `ws /api/devices/{key}/stream`. Query parameters:
  - `max_fps` — client frame-rate request (capped by `plotStreamMaxFpsCap`).
  - `detail` — `summary` or `full`.
  - `binary` — `1` to receive binary plot frames (decoded in the stream-parser worker).
  - control message `{ "type": "set_max_fps", "value": N }` retunes the rate live.
- Logs stream: `ws /api/logs/stream`.
- PSD stream: `ws /api/psd/stream` (live noise-spectrum results — see [PSD](#psd-noise-spectrum)).

## Optional Postgres lock logging

Configure in the UI:

- Use the `Postgres` chip in the top header.
- Enable logging, set host/port/db/user/password/ssl/timeout.
- Use `Test connection` and `Save`.

Logging behavior:

- Best effort, non-blocking for lock actions.
- Writes include `lock_source` — one of `manual_lock`, `auto_lock_scan`, `auto_relock`,
  or `lock_approach_probe` — plus error/monitor traces, and the guarded-move columns when
  one ran.
  Auto-relock rows carry them too, so the unattended path that runs most often is the
  one that populates the hysteresis trend.

Expected Postgres schema (`pdh_lock_results`):

```sql
CREATE TABLE IF NOT EXISTS pdh_lock_results (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    laser_name TEXT NOT NULL,
    lock_source TEXT NOT NULL DEFAULT 'manual_lock',
    success BOOLEAN,
    modulation_frequency_hz DOUBLE PRECISION,
    demod_phase_deg DOUBLE PRECISION,
    signal_offset_volts DOUBLE PRECISION,
    modulation_amplitude DOUBLE PRECISION,
    pid_p DOUBLE PRECISION,
    pid_i DOUBLE PRECISION,
    pid_d DOUBLE PRECISION,
    trace_x DOUBLE PRECISION[] NOT NULL,
    trace_y DOUBLE PRECISION[] NOT NULL,
    monitor_trace_y DOUBLE PRECISION[] NOT NULL,
    trace_x_units TEXT NOT NULL DEFAULT 'V',
    trace_y_units TEXT NOT NULL DEFAULT 'V',
    monitor_trace_y_units TEXT NOT NULL DEFAULT 'V',
    -- The sweep itself, previously only recoverable by inverting trace_x:
    sweep_center_v DOUBLE PRECISION,
    sweep_amplitude_v DOUBLE PRECISION,
    -- Guarded center move (see "Guarded center move" above):
    target_voltage_v DOUBLE PRECISION,      -- crossing the detector picked
    sweep_center_start_v DOUBLE PRECISION,  -- center before anything moved
    center_move_v DOUBLE PRECISION,         -- how far the center travelled, signed
    center_correction_v DOUBLE PRECISION,   -- hysteresis correction; 0 = target untouched
    center_offset_v DOUBLE PRECISION,       -- residual at acceptance
    capture_tolerance_v DOUBLE PRECISION,   -- the window that was applied
    approach_enabled BOOLEAN,
    approach_direct BOOLEAN,
    approach_from_below BOOLEAN,
    approach_attempts INTEGER,
    approach_detail JSONB                   -- per-attempt breakdown
);
```

Existing deployments migrate themselves: the first write of each gateway process applies
`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` on the same connection before
inserting, so no hand-run migration and no re-saving of the settings is needed. (The
schema step previously ran only from `Test connection`, which meant an upgraded gateway
nobody re-saved would insert columns that had never been added — and the failure is
swallowed by the best-effort writer, so *all* lock logging would have gone quiet.)

**Aborted auto-locks are written too**, with `success = false`. They carry the largest
measured offsets, so leaving them out would keep the most informative rows out of the
table you would characterise a laser's hysteresis from. The same numbers also ride on
the `auto_lock_scan_started` / `auto_lock_scan_failed` board events, so per-device
history is visible in the UI without querying the database.

For a local Dockerized Postgres/pgAdmin setup, see [docker/README.md](docker/README.md).
The shipped init SQL is `docker/postgres/postgres-init/01-init.sql`.

## Optional InfluxDB logging

- Use the `InfluxDB` chip in the top header.
- Select a device, configure credentials, interval, and logged parameters.
- Start/stop logging from the same popover.

## Red Pitaya telemetry (Zynq die temperature)

Each Red Pitaya can run **`rp-telemetry`**, a tiny C daemon that reports the
board's **Zynq die (junction) temperature** — the temperature of the SoC itself,
*not* ambient/room temperature and *not* a laser temperature. The gateway polls
it, shows it on each device card, and (optionally) logs it to InfluxDB.

The daemon is written in C and does almost nothing on purpose: CPU time on a Gen
1 STEMlab 125-14 is needed by `linien-server`. It spends its whole life blocked
in `accept()` — no polling loop, no timer, no thread, no HTTP, no JSON, no
Python, and no InfluxDB client on the board. Source and build instructions:
[`rp-telemetry/`](rp-telemetry/README.md).

### Protocol and port

Line-based TCP on port **18864**, one request per (short-lived) connection:

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

`RPT1` is the protocol/version identifier. Requests are capped at 64 bytes and
accepted sockets have a 2 s receive timeout.

Manual test:

```bash
printf 'STATUS\n' | nc <red-pitaya-host> 18864
```

```text
RPT1 57.34 cpu=3.2 load1=0.41 memtotal=509216 memavail=311044 uptime=690.2 rootfree=1204880
```

The temperature comes from the Zynq XADC through Linux IIO. The daemon
discovers the IIO device exposing `in_temp0_raw` at startup (the device index is
not hard-coded), reads the constant `in_temp0_offset` / `in_temp0_scale` once,
and per request re-reads only `in_temp0_raw`:

```text
temperature_c = (raw + offset) * scale / 1000.0
```

### Deployment

Installation is always an **explicit operator action** — the gateway never
installs the daemon just because a device exists or connects.

Build the ARM binary once (Docker or zig, no local toolchain needed):

```bash
cd rp-telemetry
./build-arm.sh
```

That drops a statically linked armv7 binary at
`linien-gateway/app/assets/rp-telemetry-armv7`, which is what the gateway
deploys. Until it exists, the install action fails with a message saying so
rather than deploying anything.

Then, per device: open the thermometer menu on the device card and choose
**Install**. Or use **Telemetry: install all** in the devices panel header to do
every board at once (each board is handled independently; one unreachable board
does not fail the batch).

Beside it, **Telemetry: start all** starts the service on every board — the
action you want after a power cut or a batch of reboots, when every daemon is
down and the per-device menu is twelve visits away. It asks for no confirmation
because starting an already-running service is a no-op, and a board whose unit
starts and then dies immediately is reported as a failure rather than counted
as started.

Install is idempotent and does the whole job over SSH:

1. upload the binary to `/tmp/rp-telemetry.upload`,
2. verify it (sha256, falling back to a byte-size check on images without
   `sha256sum`),
3. stage it at `/usr/local/bin/.rp-telemetry.new` with mode `0755` and **rename
   it atomically** onto `/usr/local/bin/rp-telemetry`, so a failed upload can
   never leave a truncated executable,
4. write `/etc/systemd/system/rp-telemetry.service`,
5. `systemctl daemon-reload`, `enable` (so it comes back after a reboot),
   `restart`,
6. confirm `systemctl is-active`,
7. confirm the TCP protocol returns a plausible temperature.

The unit is:

```ini
[Unit]
Description=Red Pitaya telemetry (Zynq die temperature)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/rp-telemetry --port 18864
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

**Uninstall / reinstall** — the same menu offers `Start`, `Stop`, `Restart`, and
`Uninstall` per device (`Stop`, `Restart` and `Uninstall` are deliberately not
offered fleet-wide: a mistake there should cost one board, not twelve). Uninstall stops and disables the service, removes the unit and the
binary, and clears the gateway's install record. Reinstalling is just
**Install** again; it replaces the binary and restarts the service. When the
board runs an older build than the one bundled with the gateway, the card shows
an **Update** action.

### Polling, caching, and staleness

- The gateway polls every device every **30 s**, all devices concurrently, over
  TCP only. Connect and read timeouts are 1 s each, so one unreachable board
  never delays the others.
- **SSH is never used for monitoring** — only for the explicit management
  actions above. Installation state is remembered in the device record so the
  gateway can tell "installed but stopped" from "never installed" without an SSH
  round trip.
- Readings are cached. `GET /api/devices/statuses`, `GET /api/devices/{key}/status`
  and `DeviceSession.status()` read that cache and make no remote calls, so
  telemetry cannot slow the status endpoints, the RPyC poll loop, plot
  processing, WebSocket streaming, auto-relock, or connection diagnosis.
- A changed reading is pushed over the existing per-device WebSocket `status`
  message, so the UI updates without waiting for the REST backstop poll.
  Temperature is compared at 0.1 °C resolution, so a settled board does not
  generate a message every cycle.
- **Staleness**: a successful reading older than **90 s** (three missed polls) is
  reported as `stale` and the UI stops presenting it as current. The last value
  is kept internally for context but is never shown as a live number. Note that
  a *failed* poll reports `offline`/`stopped`/`error` instead, so `stale` means
  the gateway stopped polling, not that the board is unwell.

### When something goes wrong

Failures of an action you triggered (Install, Start, Stop, Restart, Uninstall)
surface as an error toast **and** an entry in the Logs modal, carrying the
reason — a missing bundled binary, a checksum mismatch, a truncated upload, a
service that would not start, or one that started but never answered.

If a board *reboots* the moment a temperature is requested, it is running
daemon 1.0.0 and has picked the FPGA-backed XADC; update it from the telemetry
panel and see
[rp-telemetry/TROUBLESHOOTING.md](rp-telemetry/TROUBLESHOOTING.md).

Because the daemon's own output goes to the board's systemd journal, the
gateway pulls the last 20 journal lines back when a start or verification step
fails and appends them to the message. That is what turns the most likely
first-install failure — a binary built for the wrong architecture — from
"service did not become active" into `Exec format error`, without an SSH
session. `GET /api/devices/{key}/telemetry/service` returns the same journal
tail in its `journal` field.

Background problems the operator never triggered — sustained telemetry loss, a
version mismatch, a failed InfluxDB write, and the recoveries from each — are
toasted once per state transition, not once per poll, and are always in the
Logs modal.

Per-device telemetry state is one of `unknown` (not polled yet),
`not_installed`, `running`, `stopped`, `offline`, `stale`, `error`, or
`version_mismatch` (the endpoint answered something that is not this protocol).

### UI

Each device card shows, under the host/IP:

```text
Laser A
192.168.1.42:18862
RP temperature: 57.3 °C
CPU 4% · RAM 41% · disk 1.1 GB · VCCAUX 1.80 V · up 3d 4h
```

The second line is the board's own health, sampled on the same request as the
temperature and therefore withdrawn with it the moment the reading goes stale.
`CPU` is the busy fraction between the gateway's last two polls, not an instant.
Memory turns amber above 90 % used and red above 97 %; free disk turns amber
below 200 MB and red below 50 MB. `VCCAUX` is the board's FPGA auxiliary rail,
nominally 1.8 V, as measured by the XADC (daemon 1.4.0+). It is shown without
any colour threshold and is meant for comparing boards, not for judging them.
It is a regulated output rather than the board's 5 V input, and at one sample
per poll it cannot catch brief droops. Boards running a telemetry daemon older
than 1.2.0 report no metrics and show only the temperature line; boards older
than 1.4.0 show no `VCCAUX`.

and when it is unavailable, the reason plus a one-click remedy:

```text
RP temperature: unavailable
Telemetry not installed   [Install]
```

The multi-device overview cards show the temperature alone: CPU and memory move
on every poll, and putting them in the card would re-render every plot card in
the grid twice a minute to display a number nobody is watching there. Temperatures are
shown neutrally up to 75 °C, amber to 85 °C, and red above that — see
`linien-web/src/features/devices/telemetryDisplay.ts` for the thresholds and the
rationale (the XC7Z010 is rated to a maximum junction temperature of 85 °C).
Nothing is ever shut down automatically.

### InfluxDB logging

The **gateway** writes the temperature — the daemon never talks to InfluxDB, and
the Red Pitaya makes no extra HTTP/TLS request.

- Field name: **`rp_temperature_c`**, written to the same measurement, bucket,
  org, and URL already configured for that device.
- Alongside it, the board's host metrics from the same sample:
  **`rp_cpu_percent`**, **`rp_load1`**, **`rp_mem_used_percent`**,
  **`rp_mem_available_kb`**, **`rp_root_free_kb`**, **`rp_uptime_s`** and
  **`rp_vccaux_v`** (daemon 1.4.0+) — one
  point per device per cycle, not one per metric. A metric the board did not
  report is **absent** from the point rather than written as zero: a gap in the
  series is the truth, a zero is a measurement that never happened. Boards
  running a daemon older than 1.2.0 log the temperature alone.
- Written **untagged**, exactly like the Linien parameter logging, so the
  temperature lands in the same series as the rest of that device's data
  instead of a neighbouring one. Each device is expected to have its own
  destination — as it already must for the Linien parameters themselves.
- Cadence matches the telemetry poll (30 s).
- Only written for devices that have InfluxDB logging **enabled**.
- Points for devices sharing a destination are batched into one request, and the
  HTTP connection is kept alive between cycles.
- Best effort: a failed write never affects telemetry polling, the Linien
  connection, locking, plotting, or auto-relock, and a sustained outage is logged
  once (with one recovery message) rather than every 30 s.

The existing Linien parameter logging is unchanged: it still runs on the Red
Pitaya, driven by `linien-server`, and remains authoritative for those
parameters.

### Testing without hardware

`linien-sim` ships a host-side stand-in that speaks the same protocol:

```bash
linien-rp-telemetry-sim --port 18864 --base 57
linien-rp-telemetry-sim --port 18864 --fail          # exercise the error state
linien-rp-telemetry-sim --port 18864 --version 0.9.0 # exercise "update available"
```

### Security note

`rp-telemetry` answers only the fixed protocol above — there is no path to
arbitrary command execution — but it is unauthenticated and binds all interfaces
so the gateway can reach it over the LAN. Like the rest of this deployment it
assumes a trusted, isolated lab network. See [Security model](#security-model).

## Logs and observability

- In-app logs: `GET /api/logs/tail`, `DELETE /api/logs`, and the `ws /api/logs/stream`
  live feed, surfaced through the Logs modal.
- Structured log events (e.g. `lock_lost`, `auto_relock_action_failed`,
  `connection_diagnosis`) drive UI toasts.

## Simulator

See [linien-sim/README.md](linien-sim/README.md) for setup and CLI controls.

Quick start:

```powershell
cd linien-sim
python -m venv .venv
.venv\Scripts\activate
pip install -e .
linien-sim --host 127.0.0.1 --port 18863 --username root --password root
```

Then add a normal device in the web UI pointing to `127.0.0.1:18863` and click `Connect`
(do not use `Start server`).

## Docker

See [docker/README.md](docker/README.md).

> [!NOTE]
> The gateway compose file bind-mounts `data/device_settings.json`. A seed file
> (`{}`) is committed so the mount targets a real file (otherwise Docker would create a
> directory there), and the gateway falls back to a non-atomic write when the atomic
> rename can't cross the bind-mount filesystem boundary — so device-config persistence
> works in the Docker stack.

Also note that the two Docker stacks are separate compose projects, so the gateway's
default Postgres host (`127.0.0.1`) will not reach the Postgres container — point it at
the Postgres host/network explicitly.

## Security model

This is an internal lab tool and currently assumes a fully trusted network:

- **No authentication or authorization** on any REST/WebSocket endpoint. Anyone who can
  reach the port can control every device (start/stop lock, write FPGA registers, shut
  down the linien-server, reboot the Red Pitaya). Remote reboot is guarded only by a UI
  confirmation dialog, which prevents accidents, not unauthorized access.
- The documented configuration **binds to `0.0.0.0`** and CORS is `allow_origins=["*"]`
  with `allow_credentials=True`.
- Secrets are **returned in cleartext**: `GET /api/devices` includes each device's
  SSH/RPyC `password`; `GET /api/devices/{key}/logging/credentials` returns the InfluxDB
  token; the Postgres config endpoints return the DB password.

Run the gateway only on an isolated/trusted network, or place it behind an authenticating
reverse proxy. Do not expose it to untrusted networks.

## Known issues

A code audit (findings independently verified) drove the fixes in this section.

### Addressed

- **`devices.json` write races + corrupt-file tolerance** — all mutations are serialized
  under one process-wide lock, and reads tolerate a corrupt file. The underlying write
  still goes through `linien_client`, so a crash *exactly* mid-write remains a small
  residual risk (it is not made atomic here).
- **connect/disconnect race** — `connect()` is serialized against `disconnect()` so a
  disconnect can no longer be silently undone or leak a poll thread.
- **Auto-relock retry** — a failed attempt now retries regardless of the live lock state
  (it no longer abandons the device unlocked after a single sweep-mode failure).
- **Auto-relock no longer stalls reads** — the controller's `tick()` decides under
  `_state_lock` and the blocking relock sweep/scan runs outside it, so `status()` /
  snapshot reads don't block during a relock.
- **Diagnosis lock-state** — "lock likely held" is no longer reported when the FPGA
  gateware is not loaded (now "lost"/"unknown" as appropriate).
- **Installed entrypoint** honors `config.json` (apiHost/apiPort), matching `run.py`.
- **Docker device-config persistence** — seed file + cross-filesystem write fallback (see
  the [Docker](#docker) note).
- **Logs WebSocket** auto-reconnects with backoff and no longer tears down on device-list
  changes; **`request()`** reads the error body once (real error surfaces); **config
  broadcasts** no longer clobber in-progress lock-indicator / auto-relock edits.
- **CORS** no longer combines wildcard origin with credentials.
- Various hygiene: store validation, little-endian stream bytes, dead-code removal, toast
  a11y, boolean-param writes, sweep-bar pointer-cancel, simulator loop guard, numpy/uv
  packaging note.

### By design (documented, not changed)

- **No authentication; cleartext secrets on read; SSRF-by-config; `pickle.loads` of device
  payloads.** These follow from the trusted-LAN deployment model and the upstream Linien
  RPyC protocol. See [Security model](#security-model). Do not expose the gateway to
  untrusted networks.

### Deferred (intentionally not changed)

- **Cross-thread locking of a few session status scalars** (e.g. `_logging_active_cache`)
  — these are single-attribute reads/writes that are atomic under the CPython GIL, so the
  worst case is a momentarily stale flag. Adding locks to the hot `status()` path is
  net-negative; left as-is.

## Known limitations

- The legacy selection-driven **`Autolock dev`** flow **and** the slope-selection
  **`Optimization` / PID-optimization** flows are compatibility-disabled in this repo
  (`AUTOMATION_TEMP_DISABLED`), due to NumPy pickle compatibility between the gateway and
  some Linien server environments. Their endpoints and UI controls still exist but raise
  an error at runtime.
- Use the scan-based **`Autolock`** tab for the current automatic locking workflow.
