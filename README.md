# Linien Multi-Device

Monorepo with a FastAPI gateway and a React (Vite) web UI for controlling multiple Linien devices from one interface.

## Highlights

- Multi-device management with groups and shared live state.
- Web-native locking workflow:
  - `Manual` lock tab.
  - `Autolock` (scan-based target detection + lock).
  - `Autolock dev` (legacy selection flow; currently compatibility-disabled, see Known limitations).
- Lock quality tooling:
  - configurable lock indicator (error/control/monitor based),
  - auto-relock controller with configurable trigger/verify/cooldown behavior.
- Optional lock logging to Postgres (`pdh_lock_results`) for manual lock and auto-lock-from-scan actions.
- Optional InfluxDB logging control from UI (credentials, interval, loggable parameter multiselect).
- Improved sweep control interaction:
  - visual updates via `requestAnimationFrame`,
  - throttled backend parameter writes during drag,
  - final exact commit on drag end.
- Device card drag-and-drop:
  - live reorder preview while dragging in the device list,
  - reorder finalizes on release,
  - drop a card into a group panel to add that device to the group.

## Repo Structure

- `linien-gateway`: FastAPI backend.
- `linien-web`: React UI (Vite).
- `linien-sim`: virtual Linien-compatible simulator for local testing.

## Prerequisites

- Python 3.10+
- Node.js 18+

## Development

### Port Configuration

Edit `config.json` at repo root:

- `apiHost`: FastAPI bind host (use `0.0.0.0` for LAN access)
- `apiPort`: FastAPI port
- `webDevPort`: Vite dev server port

### Backend (FastAPI)

```powershell
python .\linien-gateway\run.py
```

### Frontend (Vite)

```powershell
cd linien-web
npm install
npm run dev
```

Default dev UI URL is `http://localhost:5175`.
If UI should target a different gateway host, set `VITE_API_URL` (for example `http://192.168.1.10:8000/api`).

## Serve Pre-built UI From FastAPI

```powershell
cd linien-web
npm install
npm run build

cd ..\linien-gateway
python -m uvicorn app.main:app --reload
```

Open `http://localhost:8000` for the UI and `/api` for backend endpoints.

For LAN access:

```powershell
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Optional Postgres Lock Logging

Configure in UI:

- Use the `Postgres` chip in the top header.
- Enable logging, set host/port/db/user/password/ssl/timeout.
- Use `Test connection` and `Save`.

Logging behavior:

- Best effort, non-blocking for lock actions.
- Writes include `lock_source` (`manual_lock`, `auto_lock_scan`) plus error/monitor traces.

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

For local Dockerized Postgres/pgAdmin setup, see `docker/README.md`.

## Optional InfluxDB Logging

- Use the `InfluxDB` chip in the top header.
- Select a device, configure credentials, interval, and logged parameters.
- Start/stop logging from the same popover.

## Shared Settings and Persistence

### Repo-root config files

- `config.json`: API/web port and host settings.
- `device_settings.json`: per-device custom settings persisted by gateway:
  - `auto_lock_scan_settings`
  - `lock_indicator_config`
  - `auto_relock_config`

These settings are broadcast to all connected clients for the same device via websocket `config_update` events.

### User-data config (linien-common path)

- `manual_lock_postgres.json` is persisted under `linien_common.config.USER_DATA_PATH`.

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

Then add a normal device in the web UI pointing to `127.0.0.1:18863`.

## Known Limitations

- Legacy selection-driven autolock/optimization in the `Autolock dev` flow is currently compatibility-disabled in this repo due NumPy pickle compatibility between gateway and some Linien server environments.
- Use the scan-based `Autolock` tab for current automatic locking workflow.
