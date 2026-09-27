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

## Auto-lock candidate detection and identity-aware refinement

`app/auto_lock_scan.py` is the single detector implementation
(`find_auto_lock_candidates`/`find_coarse_auto_lock_candidates`). Ordinary
one-shot auto-lock (`POST /api/devices/{key}/control/auto_lock_scan`,
`start_autolock`) is **unchanged** — it always picks the best-score
candidate, pinned by golden characterization tests recorded before the
refinement-loop refactor (`find_auto_lock_target` /
`find_coarse_auto_lock_target` are thin best-score wrappers around the
`_candidates` functions and raise the same `ValueError`s as before).

Two additions on top of that unchanged path let an external caller
(the centrex `apps/laser-orchestrator` serrodyne jobs — see
`docs/serrodyne.md` in that repo) resolve which of several
morphologically-identical PDH features on one scan corresponds to a specific
NLTL serrodyne order, without ever locking on the wrong one.

### `POST /api/devices/{key}/control/auto_lock_candidates?acquire=&timeout_s=`

Read-only detection, never locks and never moves the sweep center/amplitude
beyond the sweep restart `acquire=true` itself performs to capture a fresh
frame; never persists settings. Body: optional `AutoLockScanSettings`.

- `acquire=false` (default): detects on the latest cached frame.
- `acquire=true`: atomically triggers a new frame (same restart-and-capture
  mechanism as `acquire_scan`) and detects on exactly that frame;
  `timeout_s` bounds the wait. The analysed frame is always at least as new
  as the one the acquire triggered (the sweep keeps running, so it may be a
  slightly newer frame at the same geometry) — never an older one.
  **409** if the device is locked — checked *before* the restart-and-capture
  trigger runs, since that trigger switches the lock off; this is refused
  even while a staged run is active, since it is otherwise legitimate for
  the orchestrator to probe with `acquire=true` during a run.

Response:

```json
{
  "found": true,
  "candidate": { "...best-score AutoLockScanResult..." },
  "candidates": [ { "...every accepted crossing, desc score..." } ],
  "reason": null,
  "frame": {
    "frame_id": 123, "acquired_at": 1790000000.12,
    "sweep_center_v": 0.1, "sweep_amplitude_v": 0.5, "n_points": 2048,
    "modulation_frequency_hz": 10000000.0,
    "sideband_spacing_samples": 57.3,
    "noise_floor": 0.0012
  }
}
```

`sideband_spacing_samples` is the median of the candidates' own
`sideband_offset_samples` (null if none resolved). Each candidate carries
`feature_amplitude` (PDH lobe peak-to-peak on the unsmoothed trace —
deliberately independent of `score`: it measures feature *strength*, not
lock-target *quality*), `sideband_offset_samples` (carrier→sideband spacing
in samples, independent of a known modulation frequency), `monitor_contrast`,
and `crossing_index` (sub-sample interpolated position). No candidates found
→ `found:false, candidate:null, candidates:[]` with `reason`; `frame` is
still reported whenever a frame was analysed.

A call made while a staged auto-lock run is active still advances that run's
notion of "latest frame" (`staged_autolock_observe_frame`), so a later
`step`/`lock` can reference the frame it returned by `frame_id` without an
extra `step` round-trip.

### Staged (step-by-step) auto-lock API

Lets the caller choose the candidate to refine at every stage, instead of
the one-shot walk's implicit best-score choice. Never falls back to
best-score; if the chosen candidate is missing or fails `IdentityGuard`
(slope orientation / sideband spacing / morphology), the stage fails.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/devices/{key}/control/staged_autolock/begin` | `{settings?: AutoLockScanSettings, ttl_s: float}` | Requires the device unlocked + sweeping. Saves the current geometry for restore, acquires a fresh frame. Returns `{token, expires_at, geometry, frame, candidates, stage_index: 0}`. **409** if a staged run is already active or the device is locked. |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/renew` | `{ttl_s: float}` | → `{expires_at}`. |
| GET | `/api/devices/{key}/control/staged_autolock/` | — | Current run state, or `{"active": false}`. |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/step` | `{selected: {frame_id: int, target_index: int}}` | `frame_id` must be the most recent frame this run has returned (from `begin`/`step`, or an `auto_lock_candidates?acquire=true` call made while the run is active) — a stale `frame_id` is **409**. Plans + applies the next geometry from the selected candidate (existing planner: safe centre-move bounds, narrowing), acquires a fresh frame, and returns **every** candidate on it, each annotated `identity_ok`/`identity_reason` (`IdentityGuard.check`, evaluated **without** mutating the guard baseline unless that candidate is later actually selected) and `lockable_here` (true only when the strict detector itself produced that candidate at the current geometry — never inferred, never score-based). Response: `{stage_index, geometry, frame, candidates, expires_at, needs_more_refinement, planner}`. A planner abort is **422** `{detail}`, but the run stays active so the caller can still `abort` it (restoring geometry). |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/lock` | `{selected: {frame_id, target_index}}` | Runs the existing final strict verification on the selected candidate at the **current** (geometry-unchanged) frame — same slope, `IdentityGuard` ok, nearest crossing within a small tolerance — then the existing lock handoff. **422** and no lock if ambiguous/missing. Response is an `AutoLockScanResult`-shaped dict plus a `refinement` log. Ends the run. |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/abort` | — | Restores the saved geometry, ends the run. `{"restored": true}`. |

`StagedAutolockError.status_code` (`app/session.py`) maps every staged-API
failure to **404** (unknown/expired token — no active run, or the token
doesn't match one), **409** (a conflicting run/lock state — e.g. a second
`begin`, or `start_lock`/`start_autolock` while a run is active), or **422**
(a planner abort or a failed final-lock verification). TTL expiry
auto-aborts (restoring geometry) even with no further calls arriving —
either a background check or on next access. While a staged run is active:
one-shot `auto_lock_scan`, another `begin`, and `start_lock`/`start_autolock`
on that device are all refused with 409; read-only calls (status, telemetry,
`auto_lock_candidates`) are always allowed.

### `IdentityGuard`

`app/lock_refinement.py::IdentityGuard` tracks whether a refinement walk (or
a staged run) is still following the crossing it started on: an exact slope
match, and — once a per-detector baseline sideband spacing exists (coarse
and strict detectors are never compared against each other's baseline; they
have different systematic biases) — a sideband spacing within tolerance of
that baseline. A **better-resolved** reading (narrower scan → cleaner
spacing measurement) replaces its own baseline rather than being judged
against a worse one. `evaluate()` runs the identical check against a scratch
clone without mutating the real guard, which is what lets every candidate on
a `step` response be annotated without an unrequested selection ever
becoming the tracked identity.
