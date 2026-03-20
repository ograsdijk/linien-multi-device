# TODO

## Autolock Hysteresis Safety

Problem: large sweep-center jumps can miss the intended lock point on hysteretic piezo/cavity systems.

1. Add guarded center move before lock:
- `max_direct_jump_v`: use direct center set only below this threshold.
- For larger jumps, perform anti-backlash move:
  - overshoot by `approach_offset_v`
  - approach target from fixed direction with ramp (`ramp_step_v`, `ramp_step_delay_ms`)
  - wait `settle_ms`.

2. Pre-lock verification sweep:
- After move, run one short sweep and re-detect crossing near expected center (`verify_window_v`).
- Reuse existing error/monitor thresholds to accept/reject.

3. Retry strategy:
- If verification fails, retry once with opposite approach direction.
- If still failing, abort lock and surface clear reason to UI.

4. Config/tunables:
- `max_direct_jump_v`
- `approach_offset_v`
- `ramp_step_v`
- `ramp_step_delay_ms`
- `settle_ms`
- `verify_window_v`
