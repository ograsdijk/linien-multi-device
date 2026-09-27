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
- `serrodyne <on|off|freq|power|popt|sign|orders|powerdep|status> [...]` --
  see [Imperfect-serrodyne orders](#imperfect-serrodyne-orders-optional)
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

## Imperfect-serrodyne orders (optional)

The simulator can optionally add extra "optical order" PDH/monitor features on
top of the ordinary single-order signal, to emulate an imperfect serrodyne
(NLTL) frequency shifter that leaks power into the carrier and neighbouring
orders instead of putting it all into the desired first order. **Disabled by
default** -- with it off, `error_signal_*`/`monitor_signal` are computed
exactly as before this feature existed (see
`linien_sim/model.py::_pdh_error`/`_monitor_signal`, which just call through
to the original single-order implementation).

### Model

```
S(detuning) = sum_n  w_n(P) * PDH(detuning + offset_n)
```

reusing the simulator's existing single-order PDH/monitor computation
(`VirtualPdhModel._pdh_error_single_order` / `_monitor_signal_single_order`)
for every order `n` -- see `linien_sim/model.py::_serrodyne_order_sum`.

**Feature offsets** (`linien_sim.serrodyne.serrodyne_feature_offsets_hz`):
order-`n` optical component is at `nu_0 + n*f_serrodyne`; it resonates where
`nu_0 = nu_cav - n*f_serrodyne`, i.e. at optical detuning `-n*f_serrodyne`.
`sweep_frequency_sign` (`s in {+1,-1}`) maps the simulator's internal
detuning/sweep coordinate to true optical detuning
(`optical_detuning = s * internal_detuning`), so the order-`n` feature sits at

```
offset_n = -n * s * f_serrodyne_hz
```

in the internal coordinate. Moving `f_serrodyne_hz` by `delta_f` therefore
moves order `n`'s feature by `-n*s*delta_f` Hz, i.e. by
`-n*s*N_SB*delta_f/f_PDH` sweep samples (`N_SB` = carrier-to-sideband spacing
in samples, `f_PDH` = the modulation frequency).

**Order weights vs RF power** (`linien_sim.serrodyne.serrodyne_order_weights`),
power-dependent mode (default): let `d = rf_power_dbm - p_opt_dbm`.

- `w[+1] = desired_peak_weight * exp(-d^2 / (2*weight_sigma_db^2))` -- the
  desired first order, peaking exactly at the known optimum `p_opt_dbm`.
- `w[-1] = asymmetry_ratio * w[+1]` -- the unwanted first order, suppressed by
  a fixed ratio relative to the desired one (imperfect-serrodyne asymmetry).
- `w[0] = carrier_floor_weight + carrier_growth_per_db2 * d^2` -- carrier
  leakage, minimal at the optimum, growing quadratically away from it.
- `w[+2] = w[-2] = second_order_floor_weight + second_order_growth_per_db2 *
  d^2` -- second-order leakage, same shape with its own floor/growth. Any
  other configured order uses this same formula.

Fixed mode (`use_power_dependence=False`): every configured order's weight
comes straight from `fixed_base_weights`, e.g. the documented
`{0: 0.10, 1: 0.82, -1: 0.03, 2: 0.05, -2: 0.02}` (0.0 if an order is absent).

### Control surface

Same mechanism as every other tunable (`noise`, `drift`, `linewidthhz`, ...):
a `VirtualPdhModel.configure_serrodyne(...)` method, wrapped by
`VirtualLinienControlService.cli_set_serrodyne_*`/`cli_configure_serrodyne`/
`cli_get_serrodyne_status`, exposed in the REPL as `serrodyne ...`
sub-commands:

```
serrodyne <on|off>              # enable/disable the whole model
serrodyne freq <hz>             # serrodyne (NLTL) frequency, Hz
serrodyne power <dbm>           # RF power driving the serrodyne, dBm
serrodyne popt <dbm>            # known power optimum for the desired order
serrodyne sign <1|-1>           # sweep_frequency_sign (s)
serrodyne orders <n1,n2,...>    # which orders to sum, e.g. -2,-1,0,1,2
serrodyne powerdep <on|off>     # power-dependent weights vs fixed weights
serrodyne status                # print the current serrodyne config
```

The remaining shape parameters (`weight_sigma_db`, `desired_peak_weight`,
`asymmetry_ratio`, `carrier_floor_weight`, `carrier_growth_per_db2`,
`second_order_floor_weight`, `second_order_growth_per_db2`,
`fixed_base_weights`) are reachable via `VirtualLinienControlService.
cli_configure_serrodyne(**kwargs)` (same keyword names as
`VirtualPdhModel.configure_serrodyne`) for scripted/test use; they don't have
dedicated REPL shortcuts.

## Red Pitaya telemetry simulator

`linien-rp-telemetry-sim` stands in for the `rp-telemetry` daemon that normally
runs on a Red Pitaya, so the gateway's die-temperature polling, caching,
staleness handling, and UI can be developed without hardware. It speaks the same
line protocol (see [`rp-telemetry/`](../rp-telemetry/README.md)).

```powershell
linien-rp-telemetry-sim --port 18864
linien-rp-telemetry-sim --port 18864 --base 62 --swing 3 --period 120
linien-rp-telemetry-sim --port 18864 --fail            # always answer ERR XADC
linien-rp-telemetry-sim --port 18864 --version 0.9.0   # look like an old build
linien-rp-telemetry-sim --port 18864 --cpu 92 --mem-used 95  # a board in trouble
linien-rp-telemetry-sim --port 18864 --no-metrics       # a board still on 1.1.0
```

Point a device's host at the machine running this and the gateway polls it like
any other board. Note that the gateway reports `not_installed` until an install
record exists for that device — over TCP alone it cannot distinguish a stopped
daemon from an absent one — which is expected in simulation.

This is a development tool only: it is deliberately *not* what gets deployed,
since the whole point of the C daemon is that no Python process has to run on
the Red Pitaya.
