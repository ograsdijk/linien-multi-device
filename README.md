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
  - auto-relock controller with configurable trigger / verify / cooldown behavior.
- Connection-drop diagnosis: when a device drops, the gateway probes it out-of-band
  (TCP + SSH) to classify the cause (crash vs reboot vs unreachable) and infer whether
  the hardware lock is likely still held.
- Multi-device operations: a device overview grid, simultaneous (multi-device) sweep,
  fresh-trace acquisition, and per-device sweep-speed control.
- Optional lock logging to Postgres (`pdh_lock_results`) for manual, auto-lock-from-scan,
  and auto-relock actions.
- Optional InfluxDB logging control from the UI (credentials, interval, loggable
  parameter multiselect), resumed on reconnect.
- In-app logs: tail, clear, and a live structured log-event stream surfaced as toasts.

## Repo structure

- `linien-gateway`: FastAPI backend (Python).
- `linien-web`: React UI (Vite + TypeScript).
- `linien-sim`: virtual Linien-compatible simulator for local testing.
- `docker/`: optional Docker stacks (gateway + UI; Postgres + pgAdmin).

## Architecture overview

- The gateway keeps one long-lived **session** per device (`app/session.py`). A session
  owns the RPyC connection to the Linien server, a background poll thread, the persistent
  settings snapshot, and the per-device lock/auto-relock/diagnosis state.
- The web UI talks to the gateway over a REST API (`/api/...`) for control and over
  per-device WebSockets (`/api/devices/{key}/stream`) for live plot/status frames. A
  separate WebSocket (`/api/logs/stream`) carries structured log events.
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

## Auto-relock

A per-device controller that re-establishes a lost lock. It is configured with
trigger / verify / cooldown behavior and exposes its state over REST and in the live
stream. Auto-relock-driven locks are logged to Postgres with `lock_source = "auto_relock"`.

## Connection diagnosis

When a device's RPyC connection drops, a background probe (`app/diagnosis.py`) classifies
the cause out-of-band and surfaces it in the device status as a `diagnosis` object
(category + lock-state inference + message), rendered as a badge in the UI. The probe
needs SSH access to the Red Pitaya. Note that the gateway does **not** auto-reconnect — the
"recovering" wording is informational only, and reconnect is operator-driven.

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

## Optional Postgres lock logging

Configure in the UI:

- Use the `Postgres` chip in the top header.
- Enable logging, set host/port/db/user/password/ssl/timeout.
- Use `Test connection` and `Save`.

Logging behavior:

- Best effort, non-blocking for lock actions.
- Writes include `lock_source` — one of `manual_lock`, `auto_lock_scan`, or
  `auto_relock` — plus error/monitor traces.

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
    monitor_trace_y_units TEXT NOT NULL DEFAULT 'V'
);
```

For a local Dockerized Postgres/pgAdmin setup, see [docker/README.md](docker/README.md).
The shipped init SQL is `docker/postgres/postgres-init/01-init.sql`.

## Optional InfluxDB logging

- Use the `InfluxDB` chip in the top header.
- Select a device, configure credentials, interval, and logged parameters.
- Start/stop logging from the same popover.

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
  down the linien-server).
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
