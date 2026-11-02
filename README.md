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
- Lock-acquisition records: one record per manual, scan, staged or auto-relock lock
  attempt, with a stable `lock_acquisition_id`, the unlocked sweep it was decided on,
  the Linien configuration and the verification outcome, stored in PostgreSQL
  (`linien.pdh_lock_results`) through a durable local spool and readable over the API.
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
- Node.js 22.22+ (24 or 26 also fine). Running the web tests on Node 20 fails
  outright: `jsdom` 30 requires `^22.22.2 || ^24.15.0 || >=26.0.0`. CI runs 24.
  The Docker image still builds the UI on `node:20-bookworm-slim`, which works
  because that stage only runs `npm ci && npm run build` and never the tests.
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
  `manual_lock_postgres.json` under `linien_common.config.USER_DATA_PATH`. The
  lock-record spool `lock_acquisition_spool.sqlite3` (plus its `-wal`/`-shm` files)
  lives next to it.
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
python -m uvicorn app.main:app
```

Open `http://localhost:8000` for the UI and `/api` for backend endpoints
(`/docs` for interactive API docs).

For LAN access (run from the `linien-gateway` directory so `app.main:app` resolves):

```powershell
cd linien-gateway
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
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

## Auto-relock

A per-device controller that re-establishes a lost lock. It is configured with
trigger / verify / cooldown behavior and exposes its state over REST and in the live
stream. Every relock attempt, successful or not, is recorded as a lock acquisition with
`lock_source = "auto_relock"` (see
[Lock-acquisition records](#lock-acquisition-records-optional-postgresql)); the status
reports the latest one as `last_lock_acquisition_id`.

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
- the `rp-telemetry` journal;
- the Zynq reset-cause register.

### Reset cause

`journalctl` cannot tell you why a board reset, because a board that lost power
wrote nothing before it went. SLCR `REBOOT_STATUS` can: a watchdog timeout, a
software `reboot`, an external reset and a power-on each leave a different bit.

Reading it needed five attempts before one worked on a real board. Field images
turned out to carry no `devmem`, no `devmem2`, no Red Pitaya `monitor` and no
busybox at all; and `dd` on `/dev/mem` fails with `EFAULT` no matter the image,
because `read()` copies from `__va(phys)`, which is valid only for RAM — SLCR
is IO space and the kernel reaches it only through `ioremap`, i.e. `mmap`. So
the reader that works is a python3 `mmap` of the SLCR page, and python3 is a
fair assumption on a board whose Linien server is itself a Python process. Each
reader is tried whenever the previous produced nothing, rather than whenever its
tool is absent: one that exists and fails silently used to end the chain.

**The bits accumulate.** A field board read `0x00410000` — a system watchdog
timeout and a power-on standing together, which is impossible if each reset
cleared the register first. So a reading is every cause recorded *since the
register was last cleared*, in no order and with no timestamp; the board
timeline is what dates the restart. This corrected an earlier reading in this
codebase that was exactly backwards: an empty register was reported as "the
board lost power", when in fact a power-on **sets** bit 22 and an empty register
means only that something cleared it.

Because they accumulate, `POST /api/devices/{key}/diagnostics/clear-reset-cause`
zeroes them, so the next incident stands alone. It is the only hardware write in
the feature and stays an explicit operator action rather than something
`collect` does — an automatic clear on every read would let a second collect
erase a cause nobody had looked at yet. The pre-clear reading goes into the
timeline before it is destroyed. SLCR's write protection is lifted and put back
in a `finally`, and since the manual does not settle whether these bits are
write-one-to-clear or plain read/write, the script tries the first, reads back,
falls back to the second, and reports which one worked rather than assuming.
Bits 31:24 are the bootloader's scratch space and are preserved.

Bits 16–19 are as documented in UG585; bit 22 was confirmed in the field. Bits
20 and 21 follow the order the TRM lists the reset sources in and are unverified
— a decode resting on them says so, and the raw value stays in the section
output either way.

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

## Lock-acquisition records (optional PostgreSQL)

The gateway keeps one record per PDH lock-acquisition attempt: which path started it,
the unlocked sweep the lock was decided on, the Linien configuration it was engaged
with, whether Linien accepted the lock command, and whether the lock was then
verified. Records go to PostgreSQL through a durable local spool. This is a record of
*acquisitions*, not continuous telemetry: continuous PDH signals stay in
[InfluxDB](#optional-influxdb-logging).

Configure it in the UI with the `Postgres` chip in the top header: enable logging, set
host, port, database, user, password, SSL mode, timeout and PostgreSQL schema, then
`Test connection` and `Save`. The settings live in `manual_lock_postgres.json` under
`linien_common.config.USER_DATA_PATH`. The password is write-only: the API returns it
blank with `password_set: true`, and saving a blank password keeps the stored one.

### What is recorded

One record per attempt, on each of the four acquisition paths:

| `lock_source` | Started by | One attempt is |
|---|---|---|
| `manual_lock` | `POST /api/devices/{key}/control/start_lock` | one call |
| `auto_lock_scan` | `POST /api/devices/{key}/control/auto_lock_scan` | one call, including its refinement walk |
| `staged_autolock` | `POST /api/devices/{key}/control/staged_autolock/{token}/lock` | one `/lock` call (`begin`/`step`/`renew`/`abort` are not attempts) |
| `auto_relock` | the auto-relock controller | one relock attempt; with `max_attempts > 1` each try is its own record |

Failed attempts are recorded too (failed detection, aborted refinement, rejected lock
command, no usable trace), with `failure_stage` (`trace_capture`, `detection`,
`refinement`, `lock_command`) and `failure_reason`.

Not recorded:

- Requests refused by a precheck before any hardware command (device not connected,
  already locked, actuator busy, a staged run active, a stale staged frame).
- Anything that keeps an existing lock: parameter changes while locked, register
  writes, recentring, lock-indicator or lock-state updates.
- Read-only probes: `auto_lock_candidates`, `acquire_scan`, calibration, staged
  `begin`/`step`.

### `lock_acquisition_id`

Each attempt gets a UUID4 `lock_acquisition_id` once it passes its prechecks and
before any hardware command, whether or not PostgreSQL is enabled or reachable. It
identifies that attempt everywhere: in the API response, in the board/log events, in
the PostgreSQL row (its primary key), and in later updates of that row. It never
changes, and no two attempts share one.

Where a client gets it:

- `start_lock` returns `{"ok": true, "lock_acquisition_id": "…"}`.
- `auto_lock_scan` and staged `/lock` responses carry a `lock_acquisition_id` field.
- On failure: if the error `detail` is an object it gains `lock_acquisition_id`;
  a string `detail` stays a string and the ID is in the `X-Lock-Acquisition-Id`
  response header. A precheck refusal has no ID (it was not an attempt).
- Auto-relock: `last_lock_acquisition_id` in the auto-relock status, and in the
  `auto_relock_action_*` log events.

A client that keeps its own records stores the ID next to them and fetches the
gateway's record when it needs it. The gateway knows nothing about the client's
records; the link lives on the client's side only.

```python
import requests

GATEWAY = "http://127.0.0.1:8000"

r = requests.post(f"{GATEWAY}/api/devices/laser-a/control/auto_lock_scan", json={})
acquisition_id = r.json().get("lock_acquisition_id") or r.headers.get("X-Lock-Acquisition-Id")
my_measurement["lock_acquisition_id"] = acquisition_id      # in your own store

# later
record = requests.get(f"{GATEWAY}/api/lock-acquisitions/{acquisition_id}").json()
print(record["verification_status"], record["sweep_center_v"], record["linien_config"])
```

### Accepted, verified, persisted

Three separate facts, each with its own field:

| Question | Field | Meaning |
|---|---|---|
| Did Linien take the lock command? | `lock_command_status` (`not_sent` / `accepted` / `rejected`), `lock_command_at` | `success` is kept for compatibility and means exactly `accepted`. It is **not** proof of a stable lock. |
| Did a stable lock follow? | `verification_status`, `verification_reason`, `verification_completed_at` | `verified`: the gateway lock indicator said "locked" continuously for auto-relock's `verify_hold_s` (default 1.2 s) after the command. `failed`: the indicator reported "lost", the lock dropped, or nothing was confirmed within 15 s. `unavailable`: lock indicator disabled, no plot frames, or a new attempt started first. `not_attempted`: the command was not accepted. `pending` until decided. |
| Is the record in PostgreSQL? | spool state, `persistence` in the read API | `pending`/`retrying` until PostgreSQL acknowledges the upsert, then `persisted`. Enqueued never means persisted. |

Verification runs off the plot frames that already drive the lock indicator; nothing
waits for it. Its outcome updates the same row by `lock_acquisition_id`.

### The stored trace

Each record stores the unlocked sweep the acquisition was decided on, paired with the
sweep geometry of that same sweep:

- **Manual lock:** the cached unlocked sweep, copied *before* the lock command. If it is
  older than 5 s, or the device was already locked, no trace is stored.
- **Scan auto-lock / auto-relock:** the frame whose detection produced the result the
  lock was started from. After a refinement walk that is the narrowed scan's verified
  frame, with the narrowed geometry, not the scan the attempt started from and not the
  lock point written to `sweep_center` afterwards.
- **Staged lock:** the second confirmation frame measured at the final geometry; the
  caller's `selected_frame_id` and `selected_target_index` are in `trace_meta`.
- **Failed attempts:** the last frame the attempt analysed (`trace_meta.purpose =
  "last_analysed_frame"`), or no trace.

| Column | Content |
|---|---|
| `trace_y` | Combined error signal Linien locks on (`trace_y_role = "combined_error"`), in plot volts: ADC counts / 8192. |
| `secondary_trace_y` | The second input, with `secondary_trace_role`: `monitor` (photodiode) or `error_signal_2` (dual-channel mode: a second demodulated error signal, **not** a monitor). |
| `trace_x` | Sweep axis. `trace_x_source = "reconstructed_from_sweep"`: volts, sample *k* of *n* at `sweep_center_v − sweep_amplitude_v + 2·sweep_amplitude_v·k/(n−1)`. The board does not report per-sample sweep voltages; this is the same mapping the auto-lock uses to command a lock point, so it is validated by every auto-lock that lands on target. `sample_index` (units `sample`) when the geometry is unknown or the sweep is paused. |
| `sweep_center_v`, `sweep_amplitude_v` | The geometry of the stored trace, read back from the device right after the trace was copied (`trace_meta.geometry_read_at`). |
| `trace_meta` (JSONB) | `purpose`, `detector`, `frame_id`, `frame_processed_at` (gateway processing time, not board acquisition time), `trace_age_at_attempt_s`, `n_samples`, `secondary_n_samples`, `secondary_length_mismatch`, `dual_channel`, `channel_mixing`, `sweep_paused`, `x_axis_source`, `x_axis_model`, `y_scale`. |

A missing trace is `NULL`, never a placeholder waveform, and `trace_unavailable_reason`
says why (`no_trace_captured`, `stale_unlocked_trace (… s old)`,
`device_already_locked`, `no_unlocked_trace_cached`, …). A second trace of a different
length is stored as measured, not padded.

### Configuration snapshot (`linien_config`)

Raw Linien parameter values in Linien's own units (register counts, internal scales),
read from the linien-client cache just before the lock command. That is after any
refinement; a failed attempt keeps the copy taken when it started
(`captured_at_stage`). Included: output routing (`mod_channel`, `control_channel`,
`sweep_channel`, `slow_control_channel`, output polarities), modulation and
demodulation (frequency, amplitude, phases, multipliers, inversion), error-signal
combination (`dual_channel`, `channel_mixing`, offsets, `combined_offset`), all filter
settings, the controller (`p`, `i`, `d`, `target_slope_rising`, slow-PID settings,
`pid_only_mode`), the sweep (`sweep_center`, `sweep_amplitude`, `sweep_speed`,
`sweep_pause`), Linien's lock watching and autolock preferences, and `lock`.

```json
{"schema_version": 1, "captured_at": 1793600000.1, "captured_at_stage": "pre_lock_command",
 "linien_client_version": "2.1.0", "units": "raw Linien parameter values (internal units)",
 "parameters": {"modulation_frequency": 33554432, "p": 50, "...": "..."},
 "missing": [], "gateway": {"auto_lock_scan": {}, "lock_acceptance": {}, "lock_indicator": {}}}
```

Parameters that do not exist or cannot be read are listed in `missing`; non-finite
values are stored as `null`. Plot buffers, histories, task/optimization/PSD state and
credentials are never included. The converted columns (`modulation_frequency_hz`,
`demod_phase_deg`, `signal_offset_volts`, `modulation_amplitude`, `pid_p/i/d`) remain
for convenient SQL.

### Trace characterization

A diagnostic description of the stored trace, computed with the auto-lock detector's
own functions. It never influences a lock decision, and a failure here never blocks a
lock or a record. Auto-lock records describe the detection the lock was started from;
manual-lock records run the detector read-only on the copied sweep.

| Field | Units |
|---|---|
| `zero_crossing_sweep_v` | sweep volts |
| `slope_error_v_per_sweep_v` | error plot-volts per sweep volt (fit over half the detector's `half_range_sweep_v` window, so it depends on that calibration) |
| `discriminator_slope_v_per_mhz` | error plot-volts per MHz; **only** when the sidebands calibrate the sweep axis (PDH, known modulation frequency, sidebands resolved). `characterization.frequency_calibration` then holds `hz_per_sweep_v`. Otherwise `null`. |
| `peak_to_peak_v` | lobe peak-to-peak around the carrier, plot volts |
| `characterization.baseline_v`, `.feature_width_sweep_v` | median error away from the feature; separation of the two carrier lobe peaks. `null` when not measurable, with a note. |

`characterization_status` is `ok`, `no_feature_detected` (not a resolvable
discriminator signal), `not_applicable` (sweep paused or geometry unknown),
`unavailable` (no trace) or `error`, with `characterization.reason`.

### Database setup

Everything lives in one PostgreSQL schema, `linien` by default (configurable in the
`Postgres` settings as `db_schema`):

- `linien.pdh_lock_results`: one row per attempt, primary key `lock_acquisition_id`.
- `linien.pdh_lock_schema`: a single row holding `schema_version` (currently 1).

Two ways to create them, with identical results:

1. Start the bundled container (`docker/postgres`), whose init script
   `docker/postgres/postgres-init/01-init.sql` creates them on a fresh data volume.
2. Point the gateway at an empty database: it creates the schema on the first
   `Test connection` or write.

Both come from one definition, `TABLE_COLUMNS` in
`linien-gateway/app/manual_lock_postgres.py`. `01-init.sql` is generated by
`render_init_sql()`, and a test fails if the two differ. After changing the definition,
regenerate the file:

```powershell
cd linien-gateway
python -c "from app.manual_lock_postgres import render_init_sql; open('../docker/postgres/postgres-init/01-init.sql','w',newline='\n').write(render_init_sql())"
```

**The gateway never alters an existing table.** If it finds a table without a version
row, a different `schema_version`, or a column set other than the one it writes, it
reports `schema_ok: false` with the reason in the Postgres status, writes nothing, and
keeps the records in the spool. A future schema change will ship an explicit migration
keyed on `schema_version`.

Earlier gateway versions defined a `public.pdh_lock_results` table with a different
shape. The new writer neither reads nor touches it. If you have such a table and want
its rows, copy them across by hand. They have no acquisition IDs or trace metadata,
so generate IDs and leave the new columns `NULL` rather than inventing values.

### Local spool and recovery

Every record is written first to a local SQLite file (WAL mode),
`lock_acquisition_spool.sqlite3`, next to `manual_lock_postgres.json`. That takes a few
milliseconds and never touches the network, so hardware control never waits for
PostgreSQL. A background writer then upserts due records by `lock_acquisition_id`, so
retries and updates never create a second row.

| Spool state | Meaning |
|---|---|
| `pending` | new, updated, or recovered after a restart; waiting to be sent |
| `retrying` | the last attempt hit a temporary error (connection, timeout, schema mismatch); next try after exponential backoff, 1 s doubling to a 5 min cap |
| `persisted` | PostgreSQL acknowledged the latest version |
| `failed` | PostgreSQL rejected it permanently (data or constraint error); kept for inspection, not retried |

- The Postgres status (`GET /api/postgres/manual-lock`) reports `pending_count`,
  `retrying_count`, `persisted_count`, `failed_count`, `queue_size`,
  `dropped_count`, `schema_ok` and `schema_error`.
- On shutdown the writer finishes the record in flight and stops. Anything unsent stays
  in the spool and goes out after the next start; no drain timeout loses records.
- Retention, configurable: `spool_max_records` (10,000) and `spool_max_age_days` (30).
  Persisted records are pruned first. Unsent records never expire by age and are only
  dropped (oldest first, with a warning, counted in `dropped_count`) if unsent records
  alone exceed the limit.
- WAL with `synchronous=NORMAL` survives a gateway crash; an OS crash or power loss can
  lose the last few spooled records.
- With PostgreSQL logging disabled nothing is recorded or spooled. The acquisition IDs
  are still assigned and returned.

### Reading the history

Read-only endpoints, useful to any client:

- `GET /api/devices/{key}/lock-acquisitions`: newest attempt first. Filters: `since`,
  `until` (Unix seconds), `lock_source`, `verification_status`, `lock_command_status`,
  `success`; paging with `limit` (≤ 1000) and `offset`. Metadata only by default;
  `include=traces,config,refinement` adds the large fields.
- `GET /api/lock-acquisitions/{lock_acquisition_id}`: one full record, with its
  configuration snapshot, traces and refinement history.

Responses come from PostgreSQL when it is reachable (`"source": "postgres"`), otherwise
from the local spool (`"source": "spool"`, with a generic
`postgres_unavailable_reason`). A list from PostgreSQL does not include records still
waiting in the spool; `unsent_records` counts them, and fetching one by ID falls back
to the spool. Each record carries `persistence` (`persisted`, or its spool state).
Timestamps are Unix seconds, and `NaN` samples are `null`. Responses never contain
database credentials or connection strings.

### Querying in SQL

```sql
-- Last 20 attempts on one device, with outcome
SELECT lock_acquisition_id, attempt_started_at, lock_source, lock_command_status,
       verification_status, failure_stage, failure_reason
FROM linien.pdh_lock_results
WHERE device_key = 'laser-a'
ORDER BY attempt_started_at DESC LIMIT 20;

-- Verified locks with their discriminator slope and PID settings
SELECT attempt_started_at, discriminator_slope_v_per_mhz, slope_error_v_per_sweep_v,
       pid_p, pid_i, pid_d, linien_config->'parameters'->>'demodulation_phase_a' AS phase_a_raw
FROM linien.pdh_lock_results
WHERE verification_status = 'verified' AND device_key = 'laser-a';

-- One acquisition's trace as (x, y) pairs
SELECT x, y
FROM linien.pdh_lock_results,
     unnest(trace_x, trace_y) AS t(x, y)
WHERE lock_acquisition_id = '7f0c1a52-5d0e-4b8e-9a51-3f7d2c4c9e10';

-- Acquisitions without a usable trace, and why
SELECT lock_source, trace_unavailable_reason, count(*)
FROM linien.pdh_lock_results
WHERE trace_y IS NULL
GROUP BY 1, 2;
```

The writer is tested against an in-memory stand-in. To also run its tests against a
real PostgreSQL, start the bundled container and set `LINIEN_TEST_POSTGRES_DSN`. Each
test uses and drops its own throwaway schema:

```powershell
$env:LINIEN_TEST_POSTGRES_DSN = "host=127.0.0.1 port=5432 dbname=experiment_db user=admin password=adminpassword"
python -m pytest linien-gateway/tests/test_lock_records_real_postgres.py
```

For a local Dockerized Postgres/pgAdmin setup, see [docker/README.md](docker/README.md).

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
