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

### `POST /api/devices/{key}/control/auto_lock_candidates?acquire=&timeout_s=&detector=&include_coarse=`

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
- `detector=strict` (default) | `coarse` | `auto`: which detector to run when no
  staged run owns the detection. `auto` is strict falling back to coarse, as a
  staged run does; `coarse` is the staged API's plausible coarse candidates only.
  All analyse the same frame, and the response's `detector` says which one
  produced `candidates`. A caller tracking features that a staged run
  identified (e.g. the orchestrator's live serrodyne order labels) asks for the
  detector the run used: otherwise a wide scan that only the coarse detector
  resolves returns no candidates at all once the run has ended. While a staged
  run owns the detection (active, idle, same geometry) it is ignored.
- `include_coarse=true` (optional `coarse_min_relative_score` in (0, 1],
  `coarse_max_candidates` 1–64): when `candidates` came from the strict
  detector, also returns `coarse_candidates` + `coarse_frame`, the coarse
  detector's plausible candidates. Outside a staged run they are computed on
  the same frame; inside one, right after the run's own detection
  (`coarse_frame.frame_id` says which frame). **Read-only, for serrodyne order
  identification only**: never folded into a staged run and never valid for
  `step`/`lock` `target_index`, `lockable_here` or IdentityGuard. Strict
  detection calibrated for a lockable target misses weak serrodyne orders (e.g.
  n = −2 at ~20 % of the main one), which coarse still finds.

Response:

```json
{
  "found": true,
  "candidate": { "...best-score AutoLockScanResult..." },
  "candidates": [ { "...every accepted crossing, desc score..." } ],
  "reason": null,
  "detector": "strict",
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
still reported whenever a frame was analysed. `detector` is `"strict"` or
`"coarse"` (see below).

A call made while a staged auto-lock run is active still advances that run's
notion of "latest frame" (`staged_autolock_observe_frame`), so a later
`step`/`lock` can reference the frame it returned by `frame_id` without an
extra `step` round-trip. More specifically: when the run is idle and the
analysed frame's geometry matches the run's current geometry exactly, this
endpoint detects with the RUN's own settings and current detector mode
(strict, falling back to coarse — see the staged section below) instead of
this call's own `settings_payload`/stored settings, and those same candidate
objects become the run's `latest_candidates` directly — so a `target_index`
picked from *this* response is guaranteed to exist in the run's candidate
list for a following `step`/`lock`. A call at a different geometry, while
the run is busy, or with no active run, is unaffected: plain strict-only
detection with this call's own settings, exactly as before.

### Staged (step-by-step) auto-lock API

Lets the caller choose the candidate to refine at every stage, instead of
the one-shot walk's implicit best-score choice. Never falls back to
best-score; if the chosen candidate is missing or fails `IdentityGuard`
(slope orientation / sideband spacing / morphology), the stage fails.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/devices/{key}/control/staged_autolock/begin` | `{settings?: AutoLockScanSettings, ttl_s: float}` | Requires the device unlocked + sweeping, and the sweep-center actuator free (see "Concurrency" below) — all checked *before* the restart-and-capture trigger that acquires the first frame runs, since that trigger switches the lock off. Saves the current geometry for restore. Detects strict on the fresh frame, falling back to the coarse detector when strict finds nothing (the wide-scan case trajectory refinement exists for); only when *both* find nothing are `candidates` empty. Returns `{token, expires_at, geometry, frame, candidates, detector, detail, stage_index: 0}` — `detector` is `"strict"` or `"coarse"`; `detail` is non-null only when both detectors found nothing. **409** if a staged run is already active, the device is locked, or a one-shot refinement walk holds the sweep-center actuator. |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/renew` | `{ttl_s: float}` | → `{expires_at}`. |
| GET | `/api/devices/{key}/control/staged_autolock/` | — | Current run state, or `{"active": false}`. |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/step` | `{selected: {frame_id: int, target_index: int}}` | `frame_id` must be the most recent frame this run has returned (from `begin`/`step`, or an `auto_lock_candidates?acquire=true` call made while the run is active) — a stale `frame_id` is **409**. Plans + applies the next geometry from the selected candidate (existing planner: safe centre-move bounds, narrowing) under the same sweep-center exclusivity `begin` checks (**409** if a one-shot walk holds it), then detects strict on the fresh frame with the same strict-then-coarse fallback `begin` uses, and returns **every** candidate on it, each annotated `identity_ok`/`identity_reason` (`IdentityGuard.check`, evaluated **without** mutating the guard baseline unless that candidate is later actually selected) and `lockable_here` (true only when the strict detector itself produced that candidate at the current geometry and its own sideband spacing is not too wide for this scan — never inferred, never score-based). Response: `{stage_index, geometry, frame, candidates, expires_at, needs_more_refinement, planner}`. A planner abort is **422** `{detail}`, but the run stays active so the caller can still `abort` it (restoring geometry). If the geometry write lands but detection then fails (no candidate at the new geometry, or no fresh sweep arrived), the run's geometry/frame bookkeeping is brought back in line with the device's ACTUAL (already-moved) geometry — never left pointing at the pre-move one — `stage_index` advances, `candidates` is cleared, the run stays active, and this is **422** ("no candidate at the new geometry — abort or retry"). |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/lock` | `{selected: {frame_id, target_index}}` | **422** unless the run's current detector is `"strict"` and the selected candidate's own sideband spacing is not too wide to lock at the run's current geometry (`"not lockable at this geometry -- step first"` — the same rule `lockable_here` and the one-shot loop apply). Otherwise, runs the existing final strict verification on the selected candidate at the **current** (geometry-unchanged) frame, aligned with the one-shot's own final verification there: `check_sideband=False` (a freshly-narrowed run's own sideband estimate is the thing under test, not a gate on it), same slope, `IdentityGuard` ok, nearest crossing within a small tolerance — required on **two consecutive fresh frames**, not one, before the actuator is ever moved. Then the existing lock handoff. **422** and no lock if ambiguous/missing/inconsistent across the two frames. Response is an `AutoLockScanResult`-shaped dict plus a `refinement` log. Ends the run. |
| POST | `/api/devices/{key}/control/staged_autolock/{token}/abort` | — | Restores the saved geometry, ends the run. `{"restored": true}`. |

`StagedAutolockError.status_code` (`app/session.py`) maps every staged-API
failure to **404** (unknown/expired token — no active run, or the token
doesn't match one), **409** (a conflicting run/lock state — e.g. a second
`begin`, `start_lock`/`start_autolock` while a run is active, or a one-shot
walk holding the sweep-center actuator), or **422** (a planner abort, a
failed stage detection, an unlockable geometry, or a failed final-lock
verification). TTL expiry auto-aborts (restoring geometry) even with no
further calls arriving — either a background check or on next access; a
`renew` that races the old timer's callback (the callback started running
before `renew` could cancel it) is honoured — the callback re-checks
`expires_at` under the same lock and is a no-op once the run has already
been extended. While a staged run is active: one-shot `auto_lock_scan`,
another `begin`, and `start_lock`/`start_autolock` on that device are all
refused with 409; read-only calls (status, telemetry, `auto_lock_candidates`)
are always allowed.

**Concurrency with a one-shot walk.** The one-shot refinement loop
(`auto_lock_from_scan`) already refuses outright while a staged run exists,
but the reverse case — a one-shot walk already in flight when `begin` is
called — is possible, since both drive the same sweep-center actuator over
several seconds. `begin`, and the geometry-writing part of `step`/`lock`
(including the final `_move_and_lock` handoff), all take the same
non-blocking `_center_move_lock` a one-shot walk holds for its whole
duration; finding it held is a 409 ("Another sweep-center move is already
running..."), never a queued wait.

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
