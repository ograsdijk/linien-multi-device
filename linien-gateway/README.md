# Linien Gateway

FastAPI gateway for controlling multiple Linien servers.

## Run (dev)

```powershell
python .\linien-gateway\run.py
```

Ensure `linien-common` and `linien-client` are available (install editable or run from repo root).

## Stream Tuning Knobs

Add these optional keys to repo-root `config.json` to tune websocket plot streaming behavior:

```json
{
  "plotStreamDefaultFps": 60,
  "plotStreamMaxFpsCap": 60,
  "plotStreamDropOldFrames": true
}
```

- `plotStreamDefaultFps`: applied when a client doesn't provide `max_fps`.
- `plotStreamMaxFpsCap`: hard upper cap applied to all client `max_fps` values.
- `plotStreamDropOldFrames`: when `true`, each socket keeps only the newest pending plot frame.

## Load Probe Script

Use `scripts/stream_load_probe.py` to run a quick websocket fan-out probe:

```powershell
python .\linien-gateway\scripts\stream_load_probe.py --device-key YOUR_DEVICE_KEY --clients 20 --duration-s 20 --max-fps 10
```
