# Linien Simulator

Virtual Linien device simulator with:

- physics-based PDH-like error generation (first-order sidebands),
- reflection/transmission monitor signal,
- lock/sweep modes with PID feedback,
- disturbance controls for unlock/relock testing,
- RPyC API compatibility for `linien_client`.

Manual lock handover uses the current `sweep_center` as the initial control bias.

## Setup

```powershell
cd linien-sim
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Run

```powershell
linien-sim --host 127.0.0.1 --port 18863 --username root --password root
```

Start with the interactive Textual UI:

```powershell
linien-sim --ui tui --host 127.0.0.1 --port 18863 --username root --password root
```

Optional linewidth overrides at startup:

```powershell
linien-sim --linewidth-hz 6000000 --linewidth-v 0.07 --fsr-hz 1000000000 --jitter-v 0.002
```

`scan_hz_per_v` is derived (not independent):

```text
scan_hz_per_v = linewidth_hz / linewidth_v
```

If you want to skip auth hash checking for local testing:

```powershell
linien-sim --no-auth
```

## Connect From Linien Web

Add a normal device in the web UI:

- Host: `127.0.0.1`
- Port: `18863` (or your chosen port)
- Username/password: match simulator args

Then click `Connect` (do not use `Start server`).

## Interactive CLI

### REPL mode (`--ui repl`, default)

At runtime, use commands:

- `status`
- `lock`
- `sweep`
- `noise <electronics_sigma_v>`
- `drift <v_per_s>`
- `walk <sigma_v_per_sqrt_s>`
- `jitter <sigma_v>`
- `step <delta_v>`
- `kick <delta_v>`
- `ramp <delta_v> <seconds>`
- `monitor <reflection|transmission>`
- `phase <deg> [a|b|active]`
- `modfreq <hz>`
- `modamp <vpp>`
- `linewidthhz <hz>`
- `linewidthv <v>`
- `fsrhz <hz>`
- `pid <p> <i> <d>`
- `seed <int>`
- `exit`

`jitter` (laser/cavity detuning jitter) is now the dominant source of locked PDH error fluctuations;
`noise` sets a smaller additive electronics-noise floor.

### TUI mode (`--ui tui`)

- Click a parameter row or move with Up/Down arrows.
- Left/Right arrows decrease/increase by the row step.
- Enter edits the value directly.
- `L` starts lock mode.
- `S` starts sweep mode.
- `Q` quits the simulator UI.
