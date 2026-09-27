from __future__ import annotations

import dataclasses
import logging
import math
import pickle
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Dict, List

import numpy as np
from pydantic import ValidationError
from linien_client.connection import LinienClient
from linien_client.device import Device
from linien_client.exceptions import (
    GeneralConnectionError,
    InvalidServerVersionException,
    RPYCAuthenticationException,
    ServerNotRunningException,
)
from linien_common.common import get_lock_point
from linien_common.communication import unpack
from linien_common.influxdb import InfluxDBCredentials

from . import device_store
from . import schemas
from .auto_lock_scan import (
    AutoLockCalibration,
    AutoLockScanResult,
    AutoLockScanSettings,
    _robust_noise,
    calibrate_auto_lock_settings,
    feature_resolution_samples,
    find_auto_lock_candidates,
    find_coarse_auto_lock_candidates,
    find_plausible_coarse_candidates,
    find_coarse_auto_lock_target,
    find_auto_lock_target,
    scan_too_wide_to_lock,
)
from .auto_relock import AutoRelockConfig, AutoRelockController
from .device_recovery import RecoveryCancelled, reboot_device
from .lock_acceptance import (
    AcceptanceSettings,
    acceptance_window_v,
    capture_tolerance_v,
)
from .lock_refinement import (
    _MAX_REFINEMENT_STAGES,
    _REFINEMENT_MIN_MEASURABLE_FRACTION,
    _TrackingIdentityChanged,
    IdentityGuard,
    bounded_recenter_v,
    center_step_allowance_v,
    min_safe_amplitude_v,
    plan_refinement_step,
)
from .lock_indicator import LockIndicatorConfig, LockIndicatorEvaluator
from .manual_lock_record import ADC_SCALE, build_manual_lock_row, modulation_raw_to_hz
from .plot_processing import PlotState, V, build_plot_frame
from .signal_stats import SignalStats, compute_signal_stats
from .serializers import UNSERIALIZABLE, to_jsonable
from .stream import WebsocketManager

# Sentinel for `_read_param_fast` to distinguish "cache miss" from a
# legitimately-cached `None` value. `getattr(..., _UNSET)` returns this
# object when `_cached_value` is absent on the RemoteParameter.
_UNSET = object()

# How stale the plot stream may get (seconds since the last frame) before
# status() flags `stalled` while auto-relock is enabled. Comfortably above the
# normal plot-poll cadence so ordinary jitter never trips it. The auto-relock
# state machine only advances on frame arrival (see tick() call site), so a
# stalled stream means the controller is frozen and not actually guarding the lock.
AUTO_RELOCK_STREAM_STALL_S = 5.0

IGNORED_PARAMS = {
    "to_plot",
    "signal_stats",
    "acquisition_raw_data",
    "psd_data_partial",
    "psd_data_complete",
    "control_signal_history",
    "monitor_signal_history",
    "task",
    "ping",
}

FILTER_AUTOMATIC_PARAMS = {
    "filter_automatic_a",
    "filter_automatic_b",
}

NORMALIZED_PARAMS_ON_CONNECT = (
    "filter_automatic_a",
    "filter_automatic_b",
    "channel_mixing",
    "modulation_frequency",
)
PERSISTENT_SETTINGS_SNAPSHOT_KEY = "linien_settings_snapshot"
PERSISTENT_SETTINGS_SNAPSHOT_VERSION = 1
RECOVERY_STATE_KEY = "gateway_recovery"
# When we last had a working connection to this board, and which kernel boot it
# was in. Persisted because both halves are evidence about the *board*, and a
# gateway restart is not a board event: held only in memory, every device looked
# like one we had never connected to after a restart, the exact reboot test
# (`uptime < our absence`) could not run, and a board that had rebooted two
# hours ago was classified from a 600 s uptime threshold as "server crashed,
# FPGA still running".
LAST_HEALTHY_KEY = "gateway_last_healthy"
EXTRA_PERSISTENT_SETTINGS = {
    # Upstream linien-server 2.1.0 does not mark this as restorable, but it is a
    # user setting that controls the sign of the PID gains written to the FPGA.
    "target_slope_rising",
}

# Temporary compatibility switch.
# Set to False to re-enable the original autolock/optimization implementations below.
AUTOMATION_TEMP_DISABLED = True
AUTOMATION_TEMP_DISABLED_REASON = (
    "temporarily disabled due to NumPy pickle compatibility between gateway and server."
)
DEFAULT_INFLUX_LOGGING_INTERVAL_S = 1.0
logger = logging.getLogger(__name__)


class TrajectoryRefinementAborted(RuntimeError):
    """A failed staged scan with machine-readable diagnostics for the API."""

    def __init__(self, message: str, refinement: dict[str, Any]):
        super().__init__(message)
        self.refinement = refinement

    @property
    def failure_kind(self) -> str:
        """Which kind of failure ended the walk, for callers and operators.

        ``"identity"`` means a changed slope or sideband spacing -- the walk
        lost the feature and was tracking a different crossing, which is the
        failure this machinery exists to catch. ``"position"`` means the same
        feature simply would not hold still long enough to be verified, which
        points at drift or settle time, not at the detector. ``"other"`` is
        anything else (no fresh sweep, a lost connection).
        """
        return str(self.refinement.get("failure_kind", "other"))


# _TrackingIdentityChanged, and the constants governing the trajectory
# refinement walk (narrowing factors, stage budget, rail/epsilon tolerances),
# live in lock_refinement.py now -- see the imports above. The walk itself
# (_trajectory_refine_auto_lock, below) is the only remaining consumer.

# How long a disconnect waits for an in-flight relock action to finish before
# going ahead anyway. A relock scan takes seconds; abandoning one part-way
# leaves the sweep center on an arbitrary set-point.
RELOCK_ACTION_DRAIN_TIMEOUT_S = 5.0

# Default TTL for a staged auto-lock run (see StagedAutolockRun), matching the
# spec default. A run left untouched this long auto-aborts and restores
# geometry, so a dropped orchestrator client cannot strand the sweep narrowed.
STAGED_AUTOLOCK_DEFAULT_TTL_S = 60.0
STAGED_AUTOLOCK_MAX_TTL_S = 3600.0
# Position tolerance for the `lock` stage's same-geometry consistency check,
# in samples. Position comparison is otherwise forbidden across a geometry
# change (see lock_refinement.py's hysteresis note); at the SAME geometry
# (the fresh verification frame taken at the run's final, unmoved centre and
# amplitude) it is the only invariant available, so it gets a documented,
# generous-but-specific tolerance rather than reusing a narrower cross-
# geometry bound by accident.
STAGED_AUTOLOCK_LOCK_TOLERANCE_SAMPLES = 3.0


class StagedAutolockError(RuntimeError):
    """A staged auto-lock request could not be carried out.

    ``status_code`` tells the route which HTTP status to answer with -- 404
    (unknown/expired token), 409 (conflicting run/lock state), or 422 (a
    stale frame reference, a missing candidate, a planner refusal, or a
    failed lock-time verification). The staged path never substitutes another
    candidate for the one the caller asked for: every one of those is reported
    here rather than silently falling back to a different target.
    """

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclasses.dataclass
class StagedAutolockRun:
    """State for one in-progress staged (step-by-step) auto-lock run.

    One run per device at a time (see DeviceSession._staged_autolock,
    guarded by _state_lock). ``latest_frame_id``/``latest_candidates`` are the
    candidate list of the most recent frame the run has seen -- from `begin`,
    a `step`, or an `auto_lock_candidates?acquire=true` call made while the
    run is active (see `staged_autolock_observe_frame`) -- so `step`/`lock`
    can locate a selected candidate by `target_index` WITHOUT re-detecting,
    per the spec. ``identity`` is created from the first candidate the caller
    ever selects and is mutated only by the candidate actually chosen at each
    subsequent stage -- never by the other candidates on a frame, which are
    merely annotated via `IdentityGuard.evaluate` (non-mutating).
    """

    token: str
    settings: AutoLockScanSettings
    acceptance: AcceptanceSettings
    restore_center_v: float
    restore_amplitude_v: float
    trace_length: int
    created_at: float
    expires_at: float
    ttl_s: float
    center_v: float
    amplitude_v: float
    latest_frame: dict[str, Any]
    latest_detector: str
    latest_resolution: float
    latest_candidates: list[Any]
    stage_index: int = 0
    identity: Any | None = None
    shift_per_fraction: float | None = None
    narrow_count: int = 0
    stages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    timer: threading.Timer | None = None
    locked: bool = False
    # Set under `_state_lock` at the start of a `step`/`lock` call (which then
    # release it for slow I/O -- a geometry write, a fresh-frame wait) and
    # cleared in that call's `finally`. While True, a concurrent `step`/`lock`
    # call on this token, or `abort`, is refused with 409 rather than racing
    # the in-flight operation or letting the TTL timer restore geometry out
    # from under it -- see `_get_idle_staged_run_or_raise`,
    # `_staged_autolock_expire` and `_staged_autolock_finish_busy`.
    busy: bool = False
    # Width-shift measurement deferred from the geometry-changing stage that
    # produced the run's CURRENT frame, until the caller's selection on that
    # frame is known (the next `step` or `lock` call) -- see
    # `_consume_pending_shift_measurement`. Never the best-score candidate:
    # using it would charge the width-shift estimate to a serrodyne order the
    # caller never selected. None when the current frame was not produced by
    # a geometry change (e.g. right after `begin`, or a "done" stage).
    pending_shift: dict[str, Any] | None = None

# Frames a verification sweep must observe before it trusts what it sees. One is
# not enough: the freshness stamp records when a frame was PROCESSED, not when
# the board acquired it. See _wait_for_fresh_unlocked_trace.
VERIFY_TRACE_FRAMES = 2


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _lock_error_mhz(
    error_std_v: float | None, slope_v_per_mhz: float | None
) -> float | None:
    """In-loop lock error [MHz] = error-signal std / discriminator slope.

    None unless both inputs are finite and the slope is a usable magnitude."""
    if error_std_v is None or slope_v_per_mhz is None:
        return None
    if not math.isfinite(error_std_v) or not math.isfinite(slope_v_per_mhz):
        return None
    if slope_v_per_mhz <= 0.0:
        return None
    return abs(error_std_v) / slope_v_per_mhz


def _frame_summary(
    error_trace: np.ndarray,
    frame_id: int,
    acquired_at: float | None,
    center_v: float,
    amplitude_v: float,
    mod_hz: float | None,
    candidates: list[Any],
) -> dict[str, Any]:
    """The C1 ``frame`` shape shared by `auto_lock_detect` and the staged API.

    ``sideband_spacing_samples`` is the median of the candidates' own
    per-candidate spacing (None if none resolved); ``noise_floor`` is the
    MAD-based robust point noise of the error trace.
    """
    sideband_samples = [
        c.sideband_offset_samples
        for c in candidates
        if c.sideband_offset_samples is not None
    ]
    return {
        "frame_id": frame_id,
        "acquired_at": acquired_at,
        "sweep_center_v": center_v,
        "sweep_amplitude_v": amplitude_v,
        "n_points": int(len(error_trace)),
        "modulation_frequency_hz": mod_hz,
        "sideband_spacing_samples": (
            float(np.median(sideband_samples)) if sideband_samples else None
        ),
        "noise_floor": float(_robust_noise(np.asarray(error_trace, dtype=float))),
    }


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def flo_to_max_decimation(
    f_lo: float | None, min_dec: int = 8, max_dec: int = 24
) -> int:
    """Smallest decimation (multiple of 4) whose LPSD low-frequency edge reaches
    ``f_lo``. The device sweeps decimations 0,4,8,… up to this value, so a higher
    ``f_lo`` means a shallower (much faster) sweep.

    LPSD ``fmin_d = (125e6 / 2^d) / 16384 * 10`` ≤ f_lo  ⟹  2^d ≥ 125e6·10 / (16384·f_lo).
    """
    if f_lo is None or f_lo <= 0:
        return max_dec
    need = 125e6 * 10.0 / (16384.0 * float(f_lo))
    d = math.ceil(math.log2(need)) if need > 1.0 else 0
    d = ((d + 3) // 4) * 4  # round up to a multiple of 4
    return int(min(max(d, min_dec), max_dec))


class DeviceSession:
    @staticmethod
    def _normalize_influx_param_names(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        names: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            names.append(candidate)
        return names

    def __init__(
        self,
        device: Device,
        manager: WebsocketManager,
        lock_result_postgres_service: Any | None = None,
        log_event_callback: Callable[
            [int, str, str, str, str | None, dict[str, Any] | None], None
        ]
        | None = None,
        diagnosis_request_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.device = device
        self.manager = manager
        self._lock_result_postgres = lock_result_postgres_service
        self._log_event_callback = log_event_callback
        self._diagnosis_request_callback = diagnosis_request_callback
        # Forwards decoded PSD measurements (partial + complete) out of the
        # poll thread to the global PSD stream. Set via set_psd_event_callback.
        self._psd_event_callback: Callable[[str, dict[str, Any]], None] | None = None
        self.client: LinienClient | None = None
        self.control = None
        self.parameters = None
        self.connected = False
        self.connecting = False
        self.last_error: str | None = None
        self.last_plot_frame: Dict[str, Any] | None = None
        self.last_plot_timestamp: float | None = None
        # Set while _register_callbacks() replays every parameter with
        # call_immediately=True. Suppresses the ~90 individual param_update
        # publishes that burst would otherwise push into each subscriber's
        # bounded reliable queue; one coalesced param_snapshot is published
        # instead once registration finishes.
        self._suppress_param_publish = False
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Reentrant: connect() holds it across its whole body, and the connect
        # failure path re-enters it via _reset_connection_state().
        self._lock = threading.RLock()
        self._rpyc_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._relock_action_lock = threading.Lock()
        # Serialises everything that drives the sweep center over time. A
        # refinement walk moves the actuator for seconds; interleaved runs would
        # each measure offsets the other caused.
        self._center_move_lock = threading.Lock()
        # (center_v, amplitude_v) to put back when the sweep next starts, after
        # a refined auto-lock narrowed it. Deferred rather than written at lock
        # time: while locked, sweep_center is the lock's operating point, and
        # writing the old center then would drag the laser off the feature.
        # Guarded by _rpyc_lock.
        self._deferred_sweep_geometry: tuple[float, float] | None = None
        # The single active staged (step-by-step) auto-lock run, if any.
        # Guarded by _state_lock. See StagedAutolockRun.
        self._staged_autolock: StagedAutolockRun | None = None
        self.param_cache: Dict[str, Any] = {}
        self.param_cache_serialized: Dict[str, Any] = {}
        self._param_metadata_cache: List[Dict[str, Any]] | None = None
        self.plot_state = PlotState()
        self.auto_lock_scan_settings = self._initial_auto_lock_scan_settings()
        self.lock_acceptance_settings = self._initial_lock_acceptance_settings()
        self.lock_indicator = LockIndicatorEvaluator(
            self._initial_lock_indicator_config()
        )
        self.auto_relock = AutoRelockController(
            self._initial_auto_relock_config(),
            event_hook=self._on_auto_relock_event,
        )
        self._last_lock_indicator_state: str | None = None
        self._last_auto_relock_state: str | None = None
        self.influx_logging_state = self._initial_influx_logging_state()
        self._persistent_param_names: set[str] = set(EXTRA_PERSISTENT_SETTINGS)
        self._persistent_replay_active = False
        self._pending_gateway_param_writes: dict[str, Any] = {}
        # Locally-mirrored value of `control.exposed_get_logging_status()`.
        # Refreshed on connect and whenever we call exposed_start_logging /
        # exposed_stop_logging. status() reads from this cache so the
        # /api/devices/statuses poll doesn't have to do an RPyC round-trip
        # per device every 5 s — the only callers that can change the
        # server-side state route through this session, so the cache stays
        # authoritative.
        self._logging_active_cache: bool | None = None
        # PDH discriminator slope (error plot-units per MHz) from the last
        # auto-lock scan. Slowly-varying calibration; combined with the live
        # per-frame error_std_v it yields the in-loop lock error in MHz. Reset
        # on disconnect so a stale slope isn't reported after a reconnect.
        self._discriminator_slope_v_per_mhz: float | None = None
        # Out-of-band connection diagnosis (populated by DiagnosisProbe while
        # disconnected). `_wants_diagnosis` gates re-probing so intentionally
        # disconnected devices are not probed forever.
        self._last_connected_at: float | None = None
        self._last_healthy_boot_id: str | None = None
        self._restore_last_healthy()
        self._diagnosis_cache: dict[str, Any] | None = None
        self._last_diagnosis_category: str | None = None
        self._wants_diagnosis: bool = False
        persisted_recovery = self._device_parameters().get(RECOVERY_STATE_KEY)
        self._recovery: dict[str, Any] | None = (
            dict(persisted_recovery) if isinstance(persisted_recovery, dict) else None
        )
        if self._recovery and self._recovery.get("phase") not in {
            "completed",
            "failed",
            "cancelled",
        }:
            self._recovery = {
                **self._recovery,
                "phase": "failed",
                "updated_at": time.time(),
                "error": "Gateway restarted before reboot verification completed",
            }
            self._device_parameters()[RECOVERY_STATE_KEY] = dict(self._recovery)
            device_store.save_device(self.device)
        self._recovery_cancel = threading.Event()
        self._recovery_thread: threading.Thread | None = None
        self._removed = False
        # Supplies the cached Red Pitaya telemetry fields (die temperature +
        # service state) to status(). A pure in-memory dict read owned by
        # RpTelemetryManager -- status() must stay free of remote calls.
        self._telemetry_provider: Callable[[str], dict[str, Any]] | None = None

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _settings_values_equal(left: Any, right: Any) -> bool:
        return to_jsonable(left) == to_jsonable(right)

    @staticmethod
    def _serializable_setting_value(value: Any) -> Any:
        encoded = to_jsonable(value)
        if encoded is UNSERIALIZABLE:
            raise ValueError("Persistent setting value is not JSON serializable")
        return encoded

    def _device_parameters(self) -> dict[str, Any]:
        parameters = self.device.parameters
        if not isinstance(parameters, dict):
            parameters = {}
            self.device.parameters = parameters
        return parameters

    def _persistent_settings_snapshot(self) -> dict[str, Any] | None:
        snapshot = self._device_parameters().get(PERSISTENT_SETTINGS_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict):
            return None
        values = snapshot.get("values")
        if not isinstance(values, dict):
            return None
        return snapshot

    def _persist_settings_snapshot_values(self, values: dict[str, Any]) -> None:
        normalized_values: dict[str, Any] = {}
        for name, value in values.items():
            if name not in self._persistent_param_names:
                continue
            try:
                normalized_values[name] = self._serializable_setting_value(value)
            except ValueError:
                logger.debug(
                    "Skipping unserializable persistent setting device=%s param=%s",
                    self.device.key,
                    name,
                )
        parameters = self._device_parameters()
        parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY] = {
            "version": PERSISTENT_SETTINGS_SNAPSHOT_VERSION,
            "updated_at": self._utc_now_iso(),
            "values": normalized_values,
        }
        device_store.save_device(self.device)

    def _update_persistent_setting(self, name: str, value: Any) -> bool:
        if name not in self._persistent_param_names:
            return False
        try:
            encoded = self._serializable_setting_value(value)
        except ValueError:
            return False
        parameters = self._device_parameters()
        snapshot = self._persistent_settings_snapshot()
        if snapshot is None:
            snapshot = {
                "version": PERSISTENT_SETTINGS_SNAPSHOT_VERSION,
                "values": {},
            }
            parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY] = snapshot
        values = snapshot.setdefault("values", {})
        if not isinstance(values, dict):
            values = {}
            snapshot["values"] = values
        if values.get(name) == encoded:
            return False
        values[name] = encoded
        snapshot["version"] = PERSISTENT_SETTINGS_SNAPSHOT_VERSION
        snapshot["updated_at"] = self._utc_now_iso()
        device_store.save_device(self.device)
        return True

    def _refresh_persistent_param_names_locked(self) -> None:
        if self.parameters is None:
            self._persistent_param_names = set(EXTRA_PERSISTENT_SETTINGS)
            return
        names = set(EXTRA_PERSISTENT_SETTINGS)
        for name, param in self.parameters:
            if bool(getattr(param, "restorable", False)):
                names.add(name)
        names.difference_update(IGNORED_PARAMS)
        self._persistent_param_names = names

    def _current_persistent_remote_values_locked(self) -> dict[str, Any]:
        if self.parameters is None:
            return {}
        values: dict[str, Any] = {}
        for name in sorted(self._persistent_param_names):
            try:
                param = getattr(self.parameters, name)
                values[name] = self._serializable_setting_value(param.value)
            except (AttributeError, ValueError):
                continue
        return values

    def _seed_or_replay_persistent_settings_locked(self) -> None:
        if self.parameters is None or self.control is None:
            return
        self._refresh_persistent_param_names_locked()
        snapshot = self._persistent_settings_snapshot()
        if snapshot is None:
            values = self._current_persistent_remote_values_locked()
            self._persist_settings_snapshot_values(values)
            logger.info(
                "Seeded Linien settings snapshot device=%s count=%s",
                self.device.key,
                len(values),
            )
            return

        values = snapshot.get("values")
        if not isinstance(values, dict):
            return
        changed = 0
        self._persistent_replay_active = True
        try:
            for name, value in values.items():
                if name not in self._persistent_param_names:
                    continue
                try:
                    param = getattr(self.parameters, name)
                except AttributeError:
                    continue
                if self._settings_values_equal(param.value, value):
                    continue
                normalized_value = self._normalize_param_value(name, value)
                param.value = normalized_value
                changed += 1
                self._pending_gateway_param_writes[name] = normalized_value
            if changed:
                self.control.exposed_write_registers()
                logger.info(
                    "Replayed Linien settings snapshot device=%s count=%s",
                    self.device.key,
                    changed,
                )
        finally:
            self._persistent_replay_active = False

    def _adopt_persistent_setting_change(self, name: str, value: Any) -> None:
        if self._persistent_replay_active or name not in self._persistent_param_names:
            return
        pending = self._pending_gateway_param_writes.get(name, _UNSET)
        if pending is not _UNSET:
            if self._settings_values_equal(pending, value):
                self._pending_gateway_param_writes.pop(name, None)
                return
            self._pending_gateway_param_writes.pop(name, None)
        if self._update_persistent_setting(name, value):
            logger.info(
                "Adopted remote Linien setting change device=%s param=%s",
                self.device.key,
                name,
            )

    def _emit_log_event(
        self,
        *,
        level: int,
        source: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        callback = self._log_event_callback
        if callback is None:
            return
        try:
            callback(level, source, code, message, self.device.key, details)
        except Exception:
            logger.debug("Session log callback failed", exc_info=True)

    def set_log_event_callback(
        self,
        callback: Callable[
            [int, str, str, str, str | None, dict[str, Any] | None], None
        ]
        | None,
    ) -> None:
        self._log_event_callback = callback
        if hasattr(self.auto_relock, "set_event_hook"):
            self.auto_relock.set_event_hook(self._on_auto_relock_event)

    def set_psd_event_callback(
        self, callback: Callable[[str, dict[str, Any]], None] | None
    ) -> None:
        self._psd_event_callback = callback

    def set_diagnosis_request_callback(
        self, callback: Callable[[str], None] | None
    ) -> None:
        self._diagnosis_request_callback = callback

    def set_telemetry_provider(
        self, provider: Callable[[str], dict[str, Any]] | None
    ) -> None:
        self._telemetry_provider = provider

    def _restore_last_healthy(self) -> None:
        stored = self._device_parameters().get(LAST_HEALTHY_KEY)
        if not isinstance(stored, dict):
            return
        at = stored.get("at")
        if isinstance(at, (int, float)) and at > 0:
            # A stored time from the future means the clock moved, and treating
            # it as an absence would make every board look freshly rebooted.
            self._last_connected_at = min(float(at), time.time())
        boot_id = stored.get("boot_id")
        if isinstance(boot_id, str) and boot_id:
            self._last_healthy_boot_id = boot_id

    def _persist_last_healthy(self) -> None:
        if self._removed:
            return
        self._device_parameters()[LAST_HEALTHY_KEY] = {
            "at": self._last_connected_at,
            "boot_id": self._last_healthy_boot_id,
        }
        device_store.save_device(self.device)

    def last_healthy_boot_id(self) -> str | None:
        """The board's boot id from the last time the server was up, if known.

        Read by the diagnosis probe. It is only ever written while connected --
        an id read from an already-dead board would be the post-reboot one and
        would hide the very reboot it was meant to detect.
        """
        with self._state_lock:
            return self._last_healthy_boot_id

    def _record_healthy_boot_id(self) -> None:
        """Read and store the boot id of the board we just connected to.

        Off the connect path in its own thread: an SSH handshake is ~1 s and
        nothing about connecting should wait for it. A device with no usable
        SSH credentials simply never gets an id, and the uptime tests carry on
        as before.
        """
        device = self.device

        def worker() -> None:
            from .diagnosis import read_boot_id

            boot_id = read_boot_id(device)
            if not boot_id:
                return
            with self._state_lock:
                if not self.connected:
                    # Dropped again while we were asking. Storing the id now
                    # would date it to a connection that no longer exists.
                    return
                if boot_id == self._last_healthy_boot_id:
                    return
                self._last_healthy_boot_id = boot_id
            self._persist_last_healthy()

        threading.Thread(
            target=worker, name=f"boot-id-{self.device.key}", daemon=True
        ).start()

    def seconds_since_last_connected(self) -> float | None:
        with self._state_lock:
            ts = self._last_connected_at
        if ts is None:
            return None
        return max(0.0, time.time() - ts)

    def wants_diagnosis(self) -> bool:
        with self._state_lock:
            return self._wants_diagnosis

    def request_diagnosis_probe(self) -> None:
        """Mark this session for out-of-band diagnosis and enqueue a probe."""
        with self._state_lock:
            recovery = self._recovery
            if recovery and recovery.get("phase") not in {
                "completed",
                "failed",
                "cancelled",
            }:
                return
            self._wants_diagnosis = True
        callback = self._diagnosis_request_callback
        if callback is None:
            return
        try:
            callback(self.device.key)
        except Exception:  # noqa: BLE001 - never let the poll thread crash on this
            logger.debug("Diagnosis request callback failed", exc_info=True)

    def _clear_diagnosis(self) -> None:
        with self._state_lock:
            self._wants_diagnosis = False
            self._diagnosis_cache = None
            self._last_diagnosis_category = None

    def apply_diagnosis(self, diagnosis: dict[str, Any]) -> None:
        """Store a probe result (called from the DiagnosisProbe worker)."""
        category = diagnosis.get("category")
        with self._state_lock:
            recovery = self._recovery
            recovery_active = bool(
                recovery
                and recovery.get("phase") not in {"completed", "failed", "cancelled"}
            )
            if self.connected or recovery_active:
                # Reconnected between scheduling and probing — drop stale result.
                return
            previous = self._last_diagnosis_category
            self._diagnosis_cache = diagnosis
            self._last_diagnosis_category = category
        if category != previous:
            self._emit_log_event(
                level=logging.WARNING,
                source="diagnosis",
                code="connection_diagnosis",
                message=str(diagnosis.get("message", "Connection diagnosis updated.")),
                details=diagnosis,
            )
        self._publish_status()

    def _recovery_active(self) -> bool:
        with self._state_lock:
            recovery = self._recovery
            return bool(
                recovery
                and recovery.get("phase") not in {"completed", "failed", "cancelled"}
            )

    def recovery_active(self) -> bool:
        return self._recovery_active()

    def _set_recovery_phase(
        self, operation_id: str, phase: str, error: str | None = None
    ) -> bool:
        with self._state_lock:
            if self._recovery is None or self._recovery.get("operation_id") != operation_id:
                return False
            if self._recovery.get("phase") in {"completed", "failed", "cancelled"}:
                return False
            if self._recovery_cancel.is_set() or self._removed:
                return False
            self._recovery = {
                **self._recovery,
                "phase": phase,
                "updated_at": time.time(),
                "error": error,
            }
            self._persist_recovery_locked()
        self._publish_status()
        return True

    def _clear_finished_recovery_locked(self) -> None:
        """Drop a finished recovery record. Caller must hold ``_state_lock``.

        `failed`/`completed`/`cancelled` are terminal phases, and the record is
        persisted to devices.json, so nothing ever retracted it: a reboot that
        timed out at REBOOT_TIMEOUT_S on a board that came back a little later
        left "Reboot failed: Timed out waiting for the Red Pitaya to reboot" on
        the card forever, across gateway restarts. A successful connection is
        proof the board is back, which is the only thing that record reports.
        The failure itself stays in the log timeline (`device_reboot_failed`).
        """
        recovery = self._recovery
        if recovery is None:
            return
        if recovery.get("phase") not in {"completed", "failed", "cancelled"}:
            # A run still in progress owns the record; never clear it here.
            return
        self._recovery = None
        self._persist_recovery_locked()

    def _persist_recovery_locked(self) -> None:
        if self._removed:
            return
        parameters = self._device_parameters()
        if self._recovery is None:
            parameters.pop(RECOVERY_STATE_KEY, None)
        else:
            parameters[RECOVERY_STATE_KEY] = dict(self._recovery)
        device_store.save_device(self.device)

    def start_reboot(self) -> dict[str, Any]:
        with self._lock:
            with self._state_lock:
                if self._recovery_active():
                    raise RuntimeError("A device recovery operation is already running")
                if self.connecting:
                    raise RuntimeError("Cannot reboot while the device is connecting")
                if self._removed:
                    raise RuntimeError("Device has been removed")
                operation_id = str(uuid.uuid4())
                now = time.time()
                self._recovery = {
                    "operation_id": operation_id,
                    "action": "reboot",
                    "phase": "queued",
                    "started_at": now,
                    "updated_at": now,
                    "error": None,
                }
                self._persist_recovery_locked()
                self._recovery_cancel.clear()
                self._wants_diagnosis = False
                self._diagnosis_cache = None
                self._last_diagnosis_category = None
            worker = threading.Thread(
                target=self._run_reboot, args=(operation_id,), daemon=True
            )
            self._recovery_thread = worker
            worker.start()
        self._publish_status()
        return dict(self._recovery)

    def _run_reboot(self, operation_id: str) -> None:
        try:
            def update_phase(phase: str) -> None:
                if not self._set_recovery_phase(operation_id, phase):
                    raise RecoveryCancelled()
                if phase == "dispatching":
                    self._reset_connection_state(last_error=None, request_diagnosis=False)

            reboot_device(
                self.device,
                update_phase,
                self._recovery_cancel.is_set,
            )
            if not self._set_recovery_phase(operation_id, "completed"):
                return
            self._reset_connection_state(last_error=None, request_diagnosis=False)
            self._emit_log_event(
                level=logging.INFO,
                source="recovery",
                code="device_reboot_completed",
                message="Red Pitaya reboot completed.",
                details={"operation_id": operation_id},
            )
        except RecoveryCancelled:
            return
        except Exception as exc:
            if self._set_recovery_phase(operation_id, "failed", str(exc)):
                self._emit_log_event(
                    level=logging.ERROR,
                    source="recovery",
                    code="device_reboot_failed",
                    message="Red Pitaya reboot failed.",
                    details={"operation_id": operation_id, "error": str(exc)},
                )

    def cancel_recovery(self, *, removed: bool = False, wait: bool = False) -> None:
        self._recovery_cancel.set()
        with self._state_lock:
            recovery = self._recovery
            if recovery and recovery.get("phase") not in {
                "completed",
                "failed",
                "cancelled",
            }:
                self._recovery = {
                    **recovery,
                    "phase": "cancelled",
                    "updated_at": time.time(),
                    "error": None,
                }
                self._persist_recovery_locked()
            if removed:
                self._removed = True
        worker = self._recovery_thread
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join(timeout=7.0)

    def _on_auto_relock_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "attempt":
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_attempt",
                message="Auto-relock attempt started.",
                details=payload,
            )
            return
        if event == "success":
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_success",
                message="Auto-relock verified successfully.",
                details=payload,
            )
            return
        if event == "failure":
            self._emit_log_event(
                level=logging.ERROR,
                source="auto_relock",
                code="auto_relock_failure",
                message="Auto-relock attempt failed.",
                details=payload,
            )

    @staticmethod
    def _compact_indicator_metrics(
        signal_stats: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(signal_stats, dict):
            return {}
        keys = (
            "error_std_v",
            "error_mean_abs_v",
            "control_mean_v",
            "control_std_v",
            "monitor_mean_v",
        )
        return {key: signal_stats.get(key) for key in keys if key in signal_stats}

    def _emit_lock_transition_log(
        self,
        *,
        lock_enabled: bool,
        indicator_state: str | None,
        indicator_snapshot: dict[str, Any],
        signal_stats: dict[str, Any],
    ) -> None:
        previous_state = self._last_lock_indicator_state
        self._last_lock_indicator_state = indicator_state
        if (
            not lock_enabled
            or indicator_state is None
            or indicator_state == previous_state
        ):
            return
        details = {
            "from_state": previous_state,
            "to_state": indicator_state,
            "reasons": (
                indicator_snapshot.get("reasons")
                if isinstance(indicator_snapshot.get("reasons"), list)
                else []
            ),
            "metrics": self._compact_indicator_metrics(signal_stats),
        }
        if indicator_state == "lost":
            self._emit_log_event(
                level=logging.ERROR,
                source="lock_indicator",
                code="lock_lost",
                message="Lock indicator reports lock lost.",
                details=details,
            )
            return
        if indicator_state == "locked" and previous_state in {"lost", "marginal"}:
            self._emit_log_event(
                level=logging.INFO,
                source="lock_indicator",
                code="lock_acquired",
                message="Lock indicator reports lock acquired.",
                details=details,
            )

    def _emit_auto_relock_state_transition_log(
        self,
        auto_relock_status: dict[str, Any] | None,
    ) -> None:
        if not isinstance(auto_relock_status, dict):
            self._last_auto_relock_state = None
            return
        enabled = bool(auto_relock_status.get("enabled"))
        state = auto_relock_status.get("state")
        if not isinstance(state, str):
            self._last_auto_relock_state = None
            return
        previous_state = self._last_auto_relock_state
        self._last_auto_relock_state = state
        if not enabled or state == previous_state:
            return
        details = {
            "from_state": previous_state,
            "to_state": state,
            "attempts": auto_relock_status.get("attempts"),
            "max_attempts": auto_relock_status.get("max_attempts"),
            "last_error": auto_relock_status.get("last_error"),
        }
        if state == "lost_pending":
            self._emit_log_event(
                level=logging.WARNING,
                source="auto_relock",
                code="auto_relock_lost_pending",
                message="Auto-relock detected lost-lock condition.",
                details=details,
            )
            return
        if state == "waiting_unlocked_trace":
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_waiting_unlocked_trace",
                message="Auto-relock waiting for unlocked sweep trace.",
                details=details,
            )

    def _write_lock_result_to_postgres(
        self,
        *,
        lock_source: str,
        event_source: str,
        success: bool = True,
    ) -> None:
        if self._lock_result_postgres is None:
            self._emit_log_event(
                level=logging.WARNING,
                source="postgres",
                code="lock_result_postgres_unavailable",
                message="Lock-result postgres service unavailable.",
                details={"lock_source": lock_source, "event_source": event_source},
            )
            return

        service = self._lock_result_postgres
        try:
            device_name = (
                self.device.name
                if getattr(self.device, "name", None)
                else self.device.key
            )
            row = self.build_manual_lock_row(
                device_name=device_name,
                device_key=self.device.key,
                lock_source=lock_source,
                success=success,
            )
            enqueued = service.enqueue_lock_result(row)
            get_state = getattr(service, "get_state", None)
            state = get_state() if callable(get_state) else {}
            config = state.get("config", {}) if isinstance(state, dict) else {}
            status = state.get("status", {}) if isinstance(state, dict) else {}
            config_enabled = (
                bool(config.get("enabled")) if isinstance(config, dict) else False
            )
            details: dict[str, Any] = {
                "lock_source": lock_source,
                "event_source": event_source,
                "config_enabled": config_enabled,
            }
            if isinstance(status, dict):
                details["active"] = status.get("active")
                details["last_error"] = status.get("last_error")
            if enqueued:
                self._emit_log_event(
                    level=logging.INFO,
                    source="postgres",
                    code="lock_result_postgres_enqueued",
                    message="Lock-result row enqueued for Postgres writer.",
                    details=details,
                )
                return
            if not config_enabled:
                self._emit_log_event(
                    level=logging.INFO,
                    source="postgres",
                    code="lock_result_postgres_skipped_disabled",
                    message="Lock-result postgres disabled; skipping enqueue.",
                    details=details,
                )
                return
            details["error"] = "enqueue_rejected"
            self._emit_log_event(
                level=logging.ERROR,
                source="postgres",
                code="lock_result_postgres_enqueue_rejected",
                message="Lock-result postgres enqueue rejected.",
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - optional logging hook
            logger.warning(
                "Lock-result postgres enqueue failed device=%s source=%s",
                self.device.key,
                lock_source,
                exc_info=True,
            )
            self._emit_log_event(
                level=logging.ERROR,
                source="postgres",
                code="lock_result_postgres_enqueue_failed",
                message="Lock-result postgres enqueue failed.",
                details={
                    "lock_source": lock_source,
                    "event_source": event_source,
                    "error": str(exc),
                },
            )

    def _initial_lock_indicator_config(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        if not isinstance(parameters, dict):
            return {}
        payload = parameters.get("lock_indicator_config")
        return payload if isinstance(payload, dict) else {}

    def _initial_auto_lock_scan_settings(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        payload = (
            parameters.get("auto_lock_scan_settings")
            if isinstance(parameters, dict)
            else None
        )
        # Validate the persisted/device-stored block through the same Pydantic
        # schema the HTTP boundary uses, so out-of-band writes (linien desktop
        # client, hand-edited config) can't smuggle out-of-range or wrong-typed
        # values into the engine. There are no legacy-key aliases: a block using
        # old key names fails validation and falls back to defaults (the device
        # must be recalibrated). On any failure, fall back to defaults rather
        # than crashing the session.
        if isinstance(payload, dict):
            try:
                payload = schemas.AutoLockScanSettings.model_validate(
                    payload
                ).model_dump()
            except ValidationError as exc:
                logger.warning(
                    "Ignoring invalid stored auto_lock_scan_settings for device %s: %s",
                    getattr(self.device, "key", "?"),
                    exc,
                )
                payload = None
        else:
            payload = None
        settings = AutoLockScanSettings.from_mapping(payload)
        return settings.__dict__.copy()

    def _initial_auto_relock_config(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        if not isinstance(parameters, dict):
            return {}
        payload = parameters.get("auto_relock_config")
        return payload if isinstance(payload, dict) else {}

    def _initial_lock_acceptance_settings(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        payload = (
            parameters.get("lock_acceptance_settings")
            if isinstance(parameters, dict)
            else None
        )
        # Validated through the same Pydantic model the HTTP boundary uses, for
        # the reasons given in _initial_auto_lock_scan_settings: a stored block
        # that fails validation falls back to defaults rather than feeding
        # out-of-range acceptance settings to the refinement walk.
        if isinstance(payload, dict):
            try:
                payload = schemas.LockAcceptanceSettings.model_validate(
                    payload
                ).model_dump()
            except ValidationError as exc:
                logger.warning(
                    "Ignoring invalid stored lock_acceptance_settings for device %s: %s",
                    getattr(self.device, "key", "?"),
                    exc,
                )
                payload = None
        else:
            payload = None
        return AcceptanceSettings.from_mapping(payload).__dict__.copy()

    def _normalize_influx_logging_state(self, payload: Any) -> dict[str, Any]:
        interval = DEFAULT_INFLUX_LOGGING_INTERVAL_S
        enabled = False
        params: list[str] = []
        params_configured = False
        if isinstance(payload, bool):
            enabled = bool(payload)
            return {
                "enabled": enabled,
                "interval_s": interval,
                "params": params,
                "params_configured": params_configured,
            }
        if isinstance(payload, dict):
            enabled = bool(payload.get("enabled", False))
            interval_raw = self._coerce_float(payload.get("interval_s"))
            if interval_raw is not None and interval_raw > 0:
                interval = max(0.1, float(interval_raw))
            if "params" in payload:
                params_configured = True
                params = self._normalize_influx_param_names(payload.get("params"))
        return {
            "enabled": enabled,
            "interval_s": interval,
            "params": params,
            "params_configured": params_configured,
        }

    def _initial_influx_logging_state(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        if not isinstance(parameters, dict):
            return self._normalize_influx_logging_state(None)
        return self._normalize_influx_logging_state(
            parameters.get("influx_logging_state")
        )

    def sync_auto_lock_scan_settings_from_device(self) -> None:
        with self._state_lock:
            next_auto_lock_scan_settings = self._initial_auto_lock_scan_settings()
            if next_auto_lock_scan_settings != self.auto_lock_scan_settings:
                self.auto_lock_scan_settings = next_auto_lock_scan_settings

    def sync_lock_acceptance_settings_from_device(self) -> None:
        with self._state_lock:
            next_lock_acceptance_settings = self._initial_lock_acceptance_settings()
            if next_lock_acceptance_settings != self.lock_acceptance_settings:
                self.lock_acceptance_settings = next_lock_acceptance_settings

    def sync_lock_indicator_settings_from_device(self) -> None:
        with self._state_lock:
            next_lock_indicator_config = LockIndicatorConfig.from_mapping(
                self._initial_lock_indicator_config()
            ).to_dict()
            if next_lock_indicator_config != self.lock_indicator.get_config():
                self.lock_indicator.set_config(next_lock_indicator_config)

    def sync_auto_relock_config_from_device(self) -> None:
        with self._state_lock:
            next_auto_relock_config = AutoRelockConfig.from_mapping(
                self._initial_auto_relock_config()
            ).to_dict()
            if next_auto_relock_config != self.auto_relock.get_config():
                self.auto_relock.set_config(next_auto_relock_config)
            if hasattr(self.auto_relock, "set_event_hook"):
                self.auto_relock.set_event_hook(self._on_auto_relock_event)

    def sync_influx_logging_state_from_device(self) -> None:
        with self._state_lock:
            next_influx_logging_state = self._initial_influx_logging_state()
            if next_influx_logging_state != self.influx_logging_state:
                self.influx_logging_state = next_influx_logging_state

    def sync_configs_from_device(self) -> None:
        self.sync_auto_lock_scan_settings_from_device()
        self.sync_lock_acceptance_settings_from_device()
        self.sync_lock_indicator_settings_from_device()
        self.sync_auto_relock_config_from_device()
        self.sync_influx_logging_state_from_device()

    # Backwards-compatible alias retained for existing tests/callers.
    def sync_lock_indicator_config_from_device(self) -> None:
        self.sync_configs_from_device()

    def get_lock_indicator_config(self) -> dict[str, Any]:
        with self._state_lock:
            return self.lock_indicator.get_config()

    def update_lock_indicator_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            return self.lock_indicator.set_config(payload)

    def get_auto_lock_scan_settings(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self.auto_lock_scan_settings)

    def update_auto_lock_scan_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            settings = AutoLockScanSettings.from_mapping(payload)
            self.auto_lock_scan_settings = settings.__dict__.copy()
            return dict(self.auto_lock_scan_settings)

    def get_lock_acceptance_settings(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self.lock_acceptance_settings)

    def update_lock_acceptance_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            settings = AcceptanceSettings.from_mapping(payload)
            self.lock_acceptance_settings = settings.__dict__.copy()
            return dict(self.lock_acceptance_settings)

    def _unlocked_trace_timeout_s(self) -> float:
        """How stale an unlocked trace may be before callers must refuse it.

        Reuses the freshness definition the auto-relock subsystem already
        applies to this same trace rather than inventing a second one, with a
        little slack over the plot-poll cadence so a fresh sweep is never
        spuriously rejected between frames.
        """
        try:
            configured = float(
                self.auto_relock.get_config().get("unlocked_trace_timeout_s", 2.0)
            )
        except Exception:  # noqa: BLE001 - fall back to the schema default
            configured = 2.0
        return max(configured, 3.0)

    def calibrate_auto_lock_settings(
        self,
        *,
        include_monitor: bool,
        allow_single_side: bool,
    ) -> AutoLockCalibration:
        """Derive auto-lock settings from the current (good) PDH error trace.

        Pure compute: snapshots the live unlocked trace and analyses it. Does
        not start a lock and does not mutate stored settings; the caller
        persists the returned settings through ``update_auto_lock_scan_settings``.
        """
        if self.parameters is None:
            raise RuntimeError("Device not connected")
        error_trace, monitor_trace = self._snapshot_auto_lock_traces()

        # The unlocked trace (last_plot_data) is only refreshed while sweeping;
        # refuse to calibrate from a stale/locked trace.
        trace_timeout_s = self._unlocked_trace_timeout_s()
        with self._state_lock:
            last_unlocked_at = self.plot_state.last_unlocked_trace_at
        if last_unlocked_at is None or (time.time() - last_unlocked_at) > trace_timeout_s:
            raise RuntimeError(
                "No recent unlocked trace — start a sweep before calibrating."
            )

        with self._state_lock:
            base = AutoLockScanSettings.from_mapping(self.auto_lock_scan_settings)
        sweep_center, sweep_amplitude, preferred_slope_rising, modulation_frequency_hz = (
            self._snapshot_sweep_params(require_unlocked=True)
        )

        # Traces are in plot units (divided by ADC_SCALE in _snapshot_auto_lock_traces).
        return calibrate_auto_lock_settings(
            error_trace_v=error_trace,
            monitor_trace_v=monitor_trace,
            sweep_center_v=sweep_center,
            sweep_amplitude_v=sweep_amplitude,
            base=base,
            preferred_slope_rising=preferred_slope_rising,
            include_monitor=include_monitor,
            allow_single_side=allow_single_side,
            modulation_frequency_hz=modulation_frequency_hz,
        )

    def get_auto_relock_state(self) -> dict[str, Any]:
        with self._state_lock:
            return self.auto_relock.get_state()

    def update_auto_relock_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            self.auto_relock.set_config(payload)
            if hasattr(self.auto_relock, "set_event_hook"):
                self.auto_relock.set_event_hook(self._on_auto_relock_event)
            return self.auto_relock.get_state()

    def set_auto_relock_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._state_lock:
            self.auto_relock.set_enabled(enabled)
            if hasattr(self.auto_relock, "set_event_hook"):
                self.auto_relock.set_event_hook(self._on_auto_relock_event)
            return self.auto_relock.get_state()

    def get_influx_logging_state(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self.influx_logging_state)

    def set_influx_logging_state(
        self,
        *,
        enabled: bool | None = None,
        interval_s: float | None = None,
        params: list[str] | tuple[str, ...] | set[str] | None = None,
        params_configured: bool | None = None,
    ) -> dict[str, Any]:
        current = dict(self.influx_logging_state)
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if (
            interval_s is not None
            and math.isfinite(float(interval_s))
            and float(interval_s) > 0
        ):
            current["interval_s"] = max(0.1, float(interval_s))
        if params is not None:
            current["params"] = self._normalize_influx_param_names(params)
            if params_configured is None:
                current["params_configured"] = True
        if params_configured is not None:
            current["params_configured"] = bool(params_configured)
        self.influx_logging_state = self._normalize_influx_logging_state(current)
        return self.get_influx_logging_state()

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return numeric

    def _normalize_param_value(self, name: str, value: Any) -> Any:
        if name in FILTER_AUTOMATIC_PARAMS:
            numeric = self._coerce_float(value)
            return 2 if numeric is not None and numeric > 0 else 0

        if name == "channel_mixing":
            numeric = self._coerce_float(value)
            if numeric is None:
                return 0
            clamped = int(round(numeric))
            return max(-128, min(127, clamped))

        if name == "modulation_frequency":
            numeric = self._coerce_float(value)
            if numeric is None:
                return 0
            if numeric < 0:
                numeric = 0
            return int(round(numeric))

        return value

    def _apply_influx_logging_params_locked(self) -> None:
        if self.control is None or self.parameters is None:
            return
        if not bool(self.influx_logging_state.get("params_configured", False)):
            return
        selected = set(
            self._normalize_influx_param_names(self.influx_logging_state.get("params"))
        )
        for name, param in self.parameters:
            if not bool(getattr(param, "loggable", False)):
                continue
            should_log = name in selected
            self.control.exposed_set_parameter_log(name, should_log)
            if hasattr(param, "log"):
                param.log = should_log
                self._invalidate_param_metadata_cache()

    @staticmethod
    def _value_needs_normalization(current: Any, normalized: Any) -> bool:
        return current != normalized or type(current) is not type(normalized)

    def _sanitize_parameters_on_connect(self) -> None:
        if self.parameters is None:
            return
        changed = False
        for name in NORMALIZED_PARAMS_ON_CONNECT:
            try:
                param = getattr(self.parameters, name)
                current = param.value
            except Exception:
                continue
            normalized = self._normalize_param_value(name, current)
            if self._value_needs_normalization(current, normalized):
                param.value = normalized
                changed = True
        if changed and self.control is not None:
            self.control.exposed_write_registers()

    @staticmethod
    def _disconnect_client_safely(client: LinienClient | None) -> None:
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - best effort cleanup
            logger.warning("Failed to disconnect Linien client cleanly", exc_info=True)

    def _reset_connection_state(
        self, *, last_error: str | None = None, request_diagnosis: bool = False
    ) -> None:
        client_to_close: LinienClient | None = None
        thread_to_join: threading.Thread | None = None
        with self._lock:
            self._stop_event.set()
            client_to_close = self.client
            self.client = None
            self.control = None
            self.parameters = None
            self.connected = False
            self.connecting = False
            self.last_error = last_error
            self._last_lock_indicator_state = None
            self._last_auto_relock_state = None
            self._param_metadata_cache = None
            self._logging_active_cache = None
            self._discriminator_slope_v_per_mhz = None
            self._deferred_sweep_geometry = None
            thread_to_join = self._poll_thread
            self._poll_thread = None
        self._disconnect_client_safely(client_to_close)
        # Join the previous poll loop (best effort) so a torn-down session
        # doesn't leave a thread running against a dead client. Never join
        # ourselves — this can be reached from inside the poll thread via
        # _handle_poll_failure().
        if (
            thread_to_join is not None
            and thread_to_join is not threading.current_thread()
            and thread_to_join.is_alive()
        ):
            thread_to_join.join(timeout=1.0)
        if request_diagnosis:
            # Unexpected drop (poll failure / failed connect): probe to find out why.
            self.request_diagnosis_probe()
        else:
            # Intentional disconnect: stop probing and drop any stale diagnosis.
            self._clear_diagnosis()
        # Notify streaming clients of the disconnect so the UI flips back to its
        # "Not connected" state immediately. Done after the diagnosis bookkeeping
        # above so the published status reflects the final cleared/requested
        # diagnosis state.
        self._publish_status()

    def _handle_poll_failure(self, exc: Exception) -> None:
        logger.warning(
            "Device poll loop failed for key=%s",
            self.device.key,
            exc_info=True,
        )
        self._emit_log_event(
            level=logging.ERROR,
            source="session",
            code="poll_failure",
            message="Device poll loop failed.",
            details={"error": str(exc)},
        )
        self._reset_connection_state(last_error=str(exc), request_diagnosis=True)

    def _publish_status(self) -> None:
        """Broadcast the current status over the per-device stream.

        Called on every connection-state transition so clients with an open
        stream learn about connect/disconnect immediately, instead of waiting
        for the next /statuses backstop poll (which is skipped for actively
        streaming devices). publish() is non-blocking — it only schedules the
        broadcast on the event loop — so this is safe to call while holding
        self._lock or from the poll thread.
        """
        try:
            self.manager.publish(
                self.device.key, {"type": "status", **self.status()}
            )
        except Exception:  # noqa: BLE001 - status publish is best effort
            logger.debug(
                "Failed publishing status update device=%s",
                self.device.key,
                exc_info=True,
            )

    def connect_async(self, autostart_server: bool = False) -> None:
        with self._lock:
            if self._recovery_active():
                raise RuntimeError("Cannot connect while device recovery is running")
            if self.connected or self.connecting:
                return
            self.connecting = True
            thread = threading.Thread(
                target=self.connect, args=(autostart_server, True), daemon=True
            )
            thread.start()

    def connect(self, autostart_server: bool = False, reserved: bool = False) -> None:
        # Hold _lock across the ENTIRE connect so a concurrent disconnect()
        # (the only other _lock user) cannot interleave between the network
        # connect and the poll-thread start. Without this, a disconnect that
        # lands mid-connect was silently undone — connect went on to set
        # connected=True, clear the stop event, and start a second poll thread
        # against a client disconnect had just torn down. _lock is an RLock so
        # the failure path's _reset_connection_state() can re-enter it.
        with self._lock:
            if self._recovery_active():
                self.connecting = False
                raise RuntimeError("Cannot connect while device recovery is running")
            if self.connected or (self.connecting and not reserved):
                return
            self.connecting = True
            try:
                client = LinienClient(self.device)
                client.connect(
                    autostart_server=autostart_server,
                    use_parameter_cache=True,
                )
                self.client = client
                self.control = client.control
                self.parameters = client.parameters
                self.param_cache = {}
                self.param_cache_serialized = {}
                self._param_metadata_cache = None
                self.plot_state = PlotState()
                with self._rpyc_lock:
                    self._sanitize_parameters_on_connect()
                    self._seed_or_replay_persistent_settings_locked()
                    try:
                        self._apply_influx_logging_params_locked()
                    except Exception:  # noqa: BLE001 - optional logging setup path
                        logger.warning(
                            "Failed to apply influx logging parameter state for device=%s",
                            self.device.key,
                            exc_info=True,
                        )
                    should_resume_logging = bool(
                        self.influx_logging_state.get("enabled", False)
                    )
                    resume_interval = max(
                        0.1,
                        float(
                            self.influx_logging_state.get(
                                "interval_s", DEFAULT_INFLUX_LOGGING_INTERVAL_S
                            )
                        ),
                    )
                    if should_resume_logging:
                        try:
                            self.control.exposed_start_logging(resume_interval)
                            self._logging_active_cache = True
                        except Exception:  # noqa: BLE001 - optional resume path
                            logger.warning(
                                "Failed to resume influx logging for device=%s",
                                self.device.key,
                                exc_info=True,
                            )
                            self._logging_active_cache = None
                    else:
                        # Seed the cache once from the server so /statuses
                        # has an authoritative value without polling RPyC
                        # every 5 s.
                        try:
                            self._logging_active_cache = bool(
                                self.control.exposed_get_logging_status()
                            )
                        except Exception:  # noqa: BLE001 - best effort seed
                            self._logging_active_cache = None
                self.connected = True
                self.connecting = False
                self.last_error = None
                with self._state_lock:
                    self._last_connected_at = time.time()
                    self._diagnosis_cache = None
                    self._last_diagnosis_category = None
                    self._wants_diagnosis = False
                    # The board answered, so a finished reboot record has
                    # nothing left to report -- including a `failed` one whose
                    # board came back after the wait gave up.
                    self._clear_finished_recovery_locked()
                # Persist the moment, then go find out which boot it was. Both
                # are facts about the board that must outlive this process.
                self._persist_last_healthy()
                self._record_healthy_boot_id()
                self._register_callbacks()
                self._stop_event.clear()
                self._poll_thread = threading.Thread(
                    target=self._poll_loop, daemon=True
                )
                self._poll_thread.start()
                # Tell streaming clients we're connected now. Without this the
                # UI stays greyed-out ("Not connected") until a stream reopen
                # or the next backstop poll, even as plot frames flow.
                self._publish_status()
            except (
                ServerNotRunningException,
                GeneralConnectionError,
                InvalidServerVersionException,
                RPYCAuthenticationException,
                Exception,
            ) as exc:
                self._reset_connection_state(
                    last_error=str(exc), request_diagnosis=True
                )

    def start_server(self) -> None:
        self.connect_async(autostart_server=True)

    def disconnect(self) -> None:
        if self._recovery_active():
            raise RuntimeError("Cannot disconnect while device recovery is running")
        self._await_relock_action()
        self._reset_connection_state(last_error=self.last_error)

    def await_relock_action(
        self, timeout_s: float = RELOCK_ACTION_DRAIN_TIMEOUT_S
    ) -> None:
        """Public drain, for callers that want to wait BEFORE taking a lock.

        disconnect() drains too, but it runs under the registry's per-key lock,
        which would serialise every other request for that device behind the
        wait. Draining first leaves that join returning immediately.
        """
        self._await_relock_action(timeout_s)

    def _await_relock_action(self, timeout_s: float = RELOCK_ACTION_DRAIN_TIMEOUT_S) -> None:
        """Give an in-flight relock a moment to stop driving the sweep center.

        A relock takes seconds, so tearing the connection down underneath one
        can strand the center part-way through. Bounded: a stuck action must not
        block a disconnect indefinitely.

        Waits on `_relock_action_lock` rather than a thread handle: the lock is
        claimed synchronously before the worker thread even exists, so a
        thread-handle check has a narrow window where a relock has just been
        claimed but not yet published for this method to see.
        """
        if self._relock_action_lock.acquire(timeout=timeout_s):
            self._relock_action_lock.release()
            return
        logger.warning(
            "Relock action still running after %.1fs; disconnecting anyway "
            "(device=%s)",
            timeout_s,
            getattr(self.device, "key", "?"),
        )

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.parameters is not None:
                    with self._rpyc_lock:
                        self.parameters.check_for_changed_parameters()
                    if (
                        self.last_plot_timestamp is None
                        or (time.time() - self.last_plot_timestamp) > 1.0
                    ):
                        try:
                            with self._rpyc_lock:
                                raw = self.parameters.to_plot.value
                            self._on_to_plot(raw)
                        except Exception:  # noqa: BLE001 - keep polling on transient frame errors
                            logger.debug(
                                "Transient to_plot processing failure device=%s",
                                self.device.key,
                                exc_info=True,
                            )
                time.sleep(0.05)
            except Exception as exc:
                self._handle_poll_failure(exc)

    def _publish_param_snapshot(self) -> None:
        """Publish the whole serialized param cache as a single message.

        Replaces the per-parameter publish storm at connect time. Each
        subscriber's reliable queue is bounded (and a full queue drops the
        connection), so bursting ~90 messages through it on every connect
        risked evicting live subscribers.
        """
        with self._state_lock:
            params = dict(self.param_cache_serialized)
        if not params:
            return
        self.manager.publish(
            self.device.key, {"type": "param_snapshot", "params": params}
        )

    def _register_callbacks(self) -> None:
        if self.parameters is None:
            return
        self._suppress_param_publish = True
        try:
            self._add_parameter_callbacks()
        finally:
            self._suppress_param_publish = False
        self._publish_param_snapshot()

    def _add_parameter_callbacks(self) -> None:
        if self.parameters is None:
            return
        for name, param in self.parameters:
            if name == "to_plot":
                param.add_callback(self._on_to_plot)
            elif name == "psd_data_partial":
                # Surfaced via a dedicated callback (kept in IGNORED_PARAMS so
                # the giant pickled blob never enters param_cache / the per-device
                # param stream). psd_data_* are sync=True on the server, so the
                # remote listener is already registered and these fire through the
                # same poll loop as to_plot — no extra plumbing needed.
                param.add_callback(
                    lambda value: self._on_psd_data(value, complete=False),
                    call_immediately=False,
                )
            elif name == "psd_data_complete":
                param.add_callback(
                    lambda value: self._on_psd_data(value, complete=True),
                    call_immediately=False,
                )
            else:
                param.add_callback(
                    lambda value, n=name: self._on_param_changed(n, value),
                    call_immediately=True,
                )

    def _on_param_changed(self, name: str, value: Any) -> None:
        # Filter ignored params BEFORE caching. Some of these (e.g.
        # `to_plot`, `signal_stats`, `*_history`) are large blobs or
        # remote-reference objects, and stashing them in `param_cache`
        # would keep the RPyC reference alive on the server, leak memory,
        # and waste cycles on every subsequent snapshot.
        if name in IGNORED_PARAMS:
            return
        self._adopt_persistent_setting_change(name, value)
        with self._state_lock:
            self.param_cache[name] = value
        encoded = to_jsonable(value)
        if encoded is UNSERIALIZABLE:
            return
        with self._state_lock:
            self.param_cache_serialized[name] = encoded
        if self._suppress_param_publish:
            # Connect-time replay; _publish_param_snapshot() emits the whole
            # cache as one message once registration completes.
            return
        self.manager.publish(
            self.device.key,
            {"type": "param_update", "name": name, "value": encoded},
        )

    def _get_cached_param_values(self, names: tuple[str, ...]) -> dict[str, Any]:
        with self._state_lock:
            return {name: self.param_cache.get(name) for name in names}

    def _snapshot_cached_state(
        self,
    ) -> tuple[dict[str, Any], Dict[str, Any] | None, float | None, dict[str, Any]]:
        with self._state_lock:
            params_snapshot = deepcopy(self.param_cache_serialized)
            frame_snapshot = deepcopy(self.last_plot_frame)
            last_plot_timestamp = self.last_plot_timestamp
        # auto_relock.get_status() returns a freshly-constructed flat dict of
        # primitives, so we don't need to deepcopy it.
        auto_relock_status = self.auto_relock.get_status()
        return (
            params_snapshot,
            frame_snapshot,
            last_plot_timestamp,
            auto_relock_status,
        )

    def _snapshot_status_fields(
        self,
    ) -> tuple[float | None, bool | None, dict[str, Any], dict[str, Any]]:
        """Cheap snapshot for status() polls.

        Avoids deep-copying the cached plot frame (multi-MB) and the
        serialized param cache. Only reads the small primitives that
        status() actually needs.

        Also extracts the lock-indicator control metrics (computed per frame in
        `lock_indicator.py`) so REST status can expose a scalar control voltage;
        the values are otherwise only available over the plot WebSocket.
        """
        with self._state_lock:
            ts = self.last_plot_timestamp
            frame_lock: bool | None = None
            control_mean_v: float | None = None
            control_std_v: float | None = None
            error_std_v: float | None = None
            monitor_mean_v: float | None = None
            monitor_std_v: float | None = None
            indicator_state: str | None = None
            slope_v_per_mhz = self._discriminator_slope_v_per_mhz
            if isinstance(self.last_plot_frame, dict):
                fl = self.last_plot_frame.get("lock")
                if isinstance(fl, bool):
                    frame_lock = fl
                indicator = self.last_plot_frame.get("lock_indicator")
                if isinstance(indicator, dict):
                    state = indicator.get("state")
                    if isinstance(state, str):
                        indicator_state = state
                # Control/error stats come from the indicator-independent signal
                # stats so they survive the indicator being disabled.
                stats = self.last_plot_frame.get("signal_stats")
                if isinstance(stats, dict):
                    control_mean_v = _coerce_float(stats.get("control_mean_v"))
                    control_std_v = _coerce_float(stats.get("control_std_v"))
                    error_std_v = _coerce_float(stats.get("error_std_v"))
                    monitor_mean_v = _coerce_float(stats.get("monitor_mean_v"))
                    monitor_std_v = _coerce_float(stats.get("monitor_std_v"))
        auto_relock_status = self.auto_relock.get_status()
        control_metrics = {
            "control_mean_v": control_mean_v,
            "control_std_v": control_std_v,
            "error_std_v": error_std_v,
            "monitor_mean_v": monitor_mean_v,
            "monitor_std_v": monitor_std_v,
            "discriminator_slope_v_per_mhz": slope_v_per_mhz,
            "lock_error_mhz": _lock_error_mhz(error_std_v, slope_v_per_mhz),
            "lock_indicator_state": indicator_state,
        }
        return ts, frame_lock, auto_relock_status, control_metrics

    def _snapshot_auto_lock_traces(
        self,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        with self._state_lock:
            plot_data = self.plot_state.last_plot_data
            if plot_data is None or len(plot_data) < 3:
                raise RuntimeError("No unlocked trace available")
            error_trace_raw = plot_data[2]
            # The *true* monitor (None if the device has no monitor signal), not
            # last_plot_data[1] which is monitor_or_error_signal_2.
            monitor_trace_raw = self.plot_state.last_monitor_signal
        if error_trace_raw is None:
            raise RuntimeError("No error trace available")
        # Plot units: divide by ADC_SCALE (= plot_processing.V) so auto-lock thresholds
        # read on the same fixed scale as the plotted traces (not per-trace normalized).
        error_trace = np.array(error_trace_raw, copy=True) / ADC_SCALE
        monitor_trace = (
            np.array(monitor_trace_raw, copy=True) / ADC_SCALE
            if monitor_trace_raw is not None
            else None
        )
        return error_trace, monitor_trace

    @staticmethod
    def _mod_freq_to_hz(raw: Any) -> float | None:
        """Modulation frequency: raw device units -> Hz, or None if unusable
        (non-numeric / non-finite / <= 0, so the PDH Hz/V step is skipped)."""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0.0:
            return None
        return modulation_raw_to_hz(value)

    def _snapshot_sweep_params(
        self, *, require_unlocked: bool = False
    ) -> tuple[float, float, bool, float | None]:
        """Read the sweep params + modulation frequency the auto-lock paths need, in
        one rpyc-lock acquisition. With ``require_unlocked`` it first refuses if the
        device is already locked. Returns
        (sweep_center, sweep_amplitude, preferred_slope_rising, modulation_frequency_hz)."""
        with self._rpyc_lock:
            if require_unlocked and bool(self.parameters.lock.value):
                raise RuntimeError("Device is already locked. Start sweep first.")
            sweep_center = float(self.parameters.sweep_center.value)
            sweep_amplitude = float(self.parameters.sweep_amplitude.value)
            preferred_slope_rising = bool(self.parameters.target_slope_rising.value)
            modulation_raw = self.parameters.modulation_frequency.value
        return (
            sweep_center,
            sweep_amplitude,
            preferred_slope_rising,
            self._mod_freq_to_hz(modulation_raw),
        )

    def _snapshot_manual_lock_sources(
        self,
    ) -> tuple[list[np.ndarray | None] | None, Dict[str, Any] | None]:
        with self._state_lock:
            plot_data = self.plot_state.last_plot_data
            if plot_data is None:
                plot_data_snapshot = None
            else:
                plot_data_snapshot = [
                    np.array(item, copy=True) if item is not None else None
                    for item in plot_data
                ]
            frame_snapshot = deepcopy(self.last_plot_frame)
        return plot_data_snapshot, frame_snapshot

    def _decode_to_plot_payload(self, value: Any) -> dict[str, Any] | None:
        try:
            decoded = unpack(value)
            if isinstance(decoded, (bytes, bytearray)):
                maybe_plot = pickle.loads(decoded)
            elif isinstance(decoded, dict):
                maybe_plot = decoded
            else:
                maybe_plot = pickle.loads(bytes(decoded))
        except Exception:
            logger.debug(
                "Failed to decode to_plot payload device=%s",
                self.device.key,
                exc_info=True,
            )
            return None
        if isinstance(maybe_plot, dict):
            return maybe_plot
        logger.debug(
            "Decoded to_plot payload was not a mapping device=%s type=%s",
            self.device.key,
            type(maybe_plot).__name__,
        )
        return None

    def _on_psd_data(self, value: Any, *, complete: bool) -> None:
        """Decode a PSD payload from the server and forward it to the PSD stream.

        Runs on the poll thread (psd_data_partial/psd_data_complete are
        sync=True), so this must never raise. Strips the large raw `signals`
        and stitches the per-decimation PSDs into one log-log-ready curve.
        """
        if value is None:
            return
        try:
            payload = self._build_psd_payload(value, complete=complete)
        except Exception:
            logger.debug(
                "Failed to process PSD data device=%s", self.device.key, exc_info=True
            )
            return
        if payload is None:
            return
        callback = self._psd_event_callback
        if callback is None:
            return
        try:
            callback(self.device.key, payload)
        except Exception:
            logger.debug(
                "PSD event callback failed device=%s", self.device.key, exc_info=True
            )

    def _build_psd_payload(
        self, value: Any, *, complete: bool
    ) -> dict[str, Any] | None:
        decoded = self._decode_to_plot_payload(value)
        if decoded is None:
            return None
        curve = self._stitch_psd_curve(decoded.get("psds"))
        if not curve:
            return None
        return {
            "device_key": self.device.key,
            "uuid": decoded.get("uuid"),
            "time": _coerce_float(decoded.get("time")),
            "p": _coerce_int(decoded.get("p")),
            "i": _coerce_int(decoded.get("i")),
            "d": _coerce_int(decoded.get("d")),
            # Band-limited integrated RMS of the error signal in Volts:
            # sqrt(integral of ASD^2 over frequency). Physically meaningful and
            # the right "total noise" figure, replacing the raw uncalibrated sum.
            "rms_v": self._curve_rms(curve),
            # Raw upstream sum (uncalibrated, arbitrary units); kept for export,
            # not shown in the table.
            "fitness": _coerce_float(decoded.get("fitness")),
            "complete": bool(decoded.get("complete", complete)),
            "curve": curve,
        }

    @staticmethod
    def _clip_curve_to_band(
        curve: list[dict[str, float]],
        f_lo: float | None,
        f_hi: float | None,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Return (f, asd) arrays clipped to [f_lo, f_hi] with interpolated edges.

        The endpoints are linearly interpolated so the band integral / peaking
        metric don't depend on where the stitched samples happen to fall.
        """
        if not curve or len(curve) < 2:
            return None
        f = np.array([p["f"] for p in curve], dtype=np.float64)
        asd = np.array([p["psd"] for p in curve], dtype=np.float64)
        order = np.argsort(f)
        f = f[order]
        asd = asd[order]
        lo = f[0] if f_lo is None else max(float(f_lo), f[0])
        hi = f[-1] if f_hi is None else min(float(f_hi), f[-1])
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return None
        asd_lo = float(np.interp(lo, f, asd))
        asd_hi = float(np.interp(hi, f, asd))
        mask = (f > lo) & (f < hi)
        fb = np.concatenate(([lo], f[mask], [hi]))
        ab = np.concatenate(([asd_lo], asd[mask], [asd_hi]))
        return fb, ab

    @staticmethod
    def _curve_rms(
        curve: list[dict[str, float]],
        f_lo: float | None = None,
        f_hi: float | None = None,
    ) -> float | None:
        """Integrated RMS (V) from a stitched ASD curve: sqrt(∫ ASD^2 df).

        `curve` is the ascending [{f, psd}] list where psd is amplitude spectral
        density in V/Sqrt[Hz]. Restricted to [f_lo, f_hi] when given. Trapezoidal
        integration over the (non-uniform, log-spaced) grid yields V^2; the
        square root is the band-limited RMS in Volts.
        """
        clipped = DeviceSession._clip_curve_to_band(curve, f_lo, f_hi)
        if clipped is None:
            return None
        f, asd = clipped
        if f.size < 2:
            return None
        psd = asd**2  # power spectral density, V^2 / Hz
        # Trapezoidal integral over the (non-uniform) frequency grid, computed
        # directly to avoid numpy version differences around np.trapz.
        variance = float(np.sum(np.diff(f) * 0.5 * (psd[:-1] + psd[1:])))
        if not np.isfinite(variance) or variance < 0:
            return None
        return float(np.sqrt(variance))

    @staticmethod
    def _curve_peaking(
        curve: list[dict[str, float]],
        f_lo: float | None = None,
        f_hi: float | None = None,
    ) -> float | None:
        """Servo-bump / gain-peaking metric: max(ASD)/median(ASD) within the band.

        A tall narrow peak above the noise floor (a servo bump near the loop
        bandwidth) yields a large ratio; a flat/smooth spectrum yields ~1.
        """
        clipped = DeviceSession._clip_curve_to_band(curve, f_lo, f_hi)
        if clipped is None:
            return None
        _f, asd = clipped
        if asd.size < 3:
            return None
        median = float(np.median(asd))
        if median <= 0.0 or not np.isfinite(median):
            return None
        return float(np.max(asd) / median)

    @staticmethod
    def _stitch_psd_curve(psds: Any) -> list[dict[str, float]]:
        """Stitch per-decimation (f, psd) segments into one ascending curve.

        Ports the upstream PSDPlotWidget.plot_curve stitch: process decimations
        high->low (low frequency first), trim each segment to f greater than the
        highest frequency already plotted, and emit psd / V in V / Sqrt[Hz].
        Adjacent segments connect naturally as a single line series.
        """
        if not isinstance(psds, dict) or not psds:
            return []
        try:
            items = sorted(psds.items(), key=lambda kv: -int(kv[0]))
        except (TypeError, ValueError):
            return []
        out: list[dict[str, float]] = []
        highest_f = 0.0
        for _decimation, segment in items:
            try:
                f_raw, psd_raw = segment
            except (TypeError, ValueError):
                continue
            f = np.asarray(f_raw, dtype=np.float64)
            psd = np.asarray(psd_raw, dtype=np.float64)
            if f.size == 0 or psd.size != f.size:
                continue
            mask = f > highest_f
            f = f[mask]
            psd = psd[mask]
            if f.size == 0:
                continue
            highest_f = float(f[-1])
            scaled = psd / V
            for f_val, psd_val in zip(f.tolist(), scaled.tolist()):
                if (
                    np.isfinite(f_val)
                    and np.isfinite(psd_val)
                    and f_val > 0.0
                    and psd_val > 0.0
                ):
                    out.append({"f": float(f_val), "psd": float(psd_val)})
        return out

    def _read_param_fast(self, name: str, default: Any = None) -> Any:
        """Read a parameter without holding `_rpyc_lock` when cached.

        The linien-client cache is populated on connect for every
        cacheable param, then mutated only by
        `check_for_changed_parameters()` which runs on the poll thread.
        `_on_to_plot` (and thus the plot hot path) is invoked from that
        same poll thread, so a cache read here cannot race a cache
        write. Holding `_rpyc_lock` for these reads serialised the plot
        path against /api/devices/statuses and other API request
        handlers for no reason.

        Falls back to a locked RPyC read if the param is non-cacheable
        or the cache hasn't been populated yet (the first frame after
        connect, in pathological cases).
        """
        if self.parameters is None:
            return default
        try:
            param = getattr(self.parameters, name)
        except AttributeError:
            return default
        cached = getattr(param, "_cached_value", _UNSET)
        if cached is not _UNSET:
            return cached
        with self._rpyc_lock:
            try:
                return param.value
            except Exception:  # noqa: BLE001 - hot path, fall back to default
                logger.debug(
                    "Fast param read failed device=%s name=%s",
                    self.device.key,
                    name,
                    exc_info=True,
                )
                return default

    def _derive_lock_and_plot_params(
        self, to_plot: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """Snapshot the params needed to build a plot frame.

        Every param read here is a cacheable status/config value.
        `_read_param_fast` reads them straight from the linien-client
        cache without touching `_rpyc_lock`, eliminating contention
        with API request handlers (the previous design held the lock
        for ~13 reads per frame).
        """
        if self.parameters is None:
            lock_value = False
            if "error_signal" in to_plot and "control_signal" in to_plot:
                lock_value = True
            elif "error_signal_1" in to_plot:
                lock_value = False
            return lock_value, {"lock": lock_value}
        raw_lock = bool(self._read_param_fast("lock", False))
        params = {
            "dual_channel": self._read_param_fast("dual_channel"),
            "channel_mixing": self._read_param_fast("channel_mixing"),
            "combined_offset": self._read_param_fast("combined_offset"),
            "modulation_frequency": self._read_param_fast("modulation_frequency"),
            "pid_only_mode": self._read_param_fast("pid_only_mode"),
            "offset_a": self._read_param_fast("offset_a"),
            "offset_b": self._read_param_fast("offset_b"),
            "pid_on_slow_enabled": self._read_param_fast("pid_on_slow_enabled"),
            "autolock_preparing": self._read_param_fast("autolock_preparing"),
            "sweep_amplitude": self._read_param_fast("sweep_amplitude"),
            "autolock_initial_sweep_amplitude": self._read_param_fast(
                "autolock_initial_sweep_amplitude"
            ),
            "control_signal_history_length": self._read_param_fast(
                "control_signal_history_length"
            ),
        }
        if "error_signal" in to_plot and "control_signal" in to_plot:
            lock_value = True
        elif "error_signal_1" in to_plot:
            lock_value = False
        else:
            lock_value = raw_lock
        params["lock"] = lock_value
        return lock_value, params

    def _start_auto_relock(self) -> None:
        # The caller holds _relock_action_lock for the whole of this.
        try:
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_action_start",
                message="Auto-relock action started.",
            )
            self.auto_lock_from_scan(None)
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_action_success",
                message="Auto-relock action completed.",
            )
            self._write_lock_result_to_postgres(
                lock_source="auto_relock",
                event_source="auto_relock",
            )
        except Exception as exc:
            self._emit_log_event(
                level=logging.ERROR,
                source="auto_relock",
                code="auto_relock_action_failed",
                message="Auto-relock action failed.",
                details={"error": str(exc)},
            )
            raise

    def _on_to_plot(self, value: Any) -> None:
        if self.parameters is None:
            return
        if self.parameters.pause_acquisition.value:
            return
        if value is None:
            return
        to_plot = self._decode_to_plot_payload(value)
        if to_plot is None:
            return

        lock_value, params = self._derive_lock_and_plot_params(to_plot)

        # Decide what detail level to build at. When every connected
        # websocket subscriber only needs a summary frame (the common case
        # when many devices are displayed on the overview grid), building
        # the full frame's history/quadrature series wastes CPU and
        # allocates large lists that are immediately discarded by
        # `filter_plot_frame`.
        #
        # We build "full" only when at least one subscriber wants full
        # detail, or when auto-relock is active (it inspects
        # `last_plot_frame` for snapshots). When there are NO subscribers
        # at all we still build (so internal state — `last_plot_frame`,
        # `plot_state`, `lock_indicator`, `auto_relock` — stays current),
        # but at "summary" detail to avoid the heavy history/quadrature
        # work that nobody is consuming. REST snapshot consumers that read
        # `last_plot_frame` (e.g. manual lock trace fallback) only need
        # the `combined_error` and `monitor_signal`/`error_signal_2`
        # series, both of which are present in summary frames.
        # `peek_required_detail` walks the per-device connection set
        # under a lock. Subscriber set is stable for the duration of
        # one frame build, so we capture it once and reuse for both
        # the build-detail decision below and the publish-gate
        # decision further down.
        required_detail = self.manager.peek_required_detail(self.device.key)
        auto_relock_active = bool(self.auto_relock.get_status().get("enabled"))
        build_detail = (
            "full"
            if required_detail == "full" or auto_relock_active
            else "summary"
        )
        # When there are no subscribers AND auto-relock is idle, we
        # still build a frame so state-mutating side effects in
        # build_plot_frame stay current (histories, last_plot_data,
        # std stats), but skip the per-series (arr / V).tolist()
        # conversions that dominate plot CPU. Lock indicator and
        # auto-relock both read raw `to_plot` directly, so they
        # remain correct without the series payload.
        needs_series = required_detail is not None or auto_relock_active

        # Raw signal statistics are independent of the lock indicator: computed
        # here whenever locked (means are meaningless while sweeping) and surfaced
        # via frame["signal_stats"] so status()/logs see them even when the
        # indicator is disabled. The indicator consumes them for its thresholds.
        signal_stats = compute_signal_stats(to_plot) if lock_value else SignalStats()

        with self._state_lock:
            frame = build_plot_frame(
                to_plot,
                params,
                self.plot_state,
                detail=build_detail,
                build_series=needs_series,
            )
            if frame is None:
                return
            frame["signal_stats"] = asdict(signal_stats)
            # Surface the discriminator slope (last auto-lock scan) and the
            # derived in-loop lock error alongside the per-frame stats so the
            # plot stream carries everything the UI needs without a REST poll.
            slope_v_per_mhz = self._discriminator_slope_v_per_mhz
            frame["discriminator_slope_v_per_mhz"] = slope_v_per_mhz
            frame["lock_error_mhz"] = _lock_error_mhz(
                signal_stats.error_std_v, slope_v_per_mhz
            )
            frame_lock_indicator = self.lock_indicator.update(
                lock=lock_value,
                stats=signal_stats,
            )
            frame["lock_indicator"] = frame_lock_indicator
            indicator_state = (
                frame_lock_indicator.get("state")
                if isinstance(frame_lock_indicator, dict)
                else None
            )
            # NOTE: tick() is driven ONLY from this plot-frame handler — there is
            # no independent timer. All auto-relock holds/timeouts (trigger_hold_s,
            # verify_hold_s, unlocked_trace_timeout_s) therefore only advance when a
            # frame arrives. If the plot stream stalls entirely, the controller
            # freezes and cannot self-detect "frames stopped". That case is surfaced
            # to operators via the stream_age_s / stalled fields in status() (the
            # REST poll, which keeps updating while the websocket frames have ceased).
            relock_action = self.auto_relock.tick(
                lock=lock_value,
                indicator_state=indicator_state
                if isinstance(indicator_state, str)
                else None,
                unlocked_trace_at=self.plot_state.last_unlocked_trace_at,
            )
            # auto_relock.get_status() already returns a freshly-constructed
            # flat dict of primitives; no need to deepcopy it.
            auto_relock_status = self.auto_relock.get_status()
            frame["auto_relock"] = auto_relock_status
            self.last_plot_frame = frame
            self.last_plot_timestamp = time.time()

        # Perform the auto-relock device I/O OUTSIDE _state_lock so status()
        # and snapshot reads don't stall during a (possibly multi-second)
        # relock sweep/scan. complete_action() applies the result. (#26)
        if relock_action == "relock":
            # Off the poll thread, not just outside _state_lock. A refinement
            # walk waits for a fresh unlocked trace, and this callback IS the
            # thread that produces them -- run inline, it would block
            # waiting for output it is itself preventing, time out on every
            # attempt, and stall the plot pipeline meanwhile. Note that tick()
            # keeps handing out "relock" every frame until complete_action
            # lands, so the dispatch has to drop duplicates itself; see
            # _maybe_dispatch_relock_action.
            self._maybe_dispatch_relock_action(self._start_auto_relock)
        elif relock_action is not None:
            action_ok = True
            action_error: str | None = None
            try:
                if relock_action == "sweep":
                    self.stop_lock()
            except Exception as exc:  # noqa: BLE001 - recorded as a relock failure
                action_ok = False
                action_error = str(exc)
            with self._state_lock:
                self.auto_relock.complete_action(
                    relock_action, action_ok, action_error
                )
                auto_relock_status = self.auto_relock.get_status()

        self._emit_lock_transition_log(
            lock_enabled=bool(lock_value),
            indicator_state=indicator_state
            if isinstance(indicator_state, str)
            else None,
            indicator_snapshot=(
                frame_lock_indicator if isinstance(frame_lock_indicator, dict) else {}
            ),
            signal_stats=asdict(signal_stats),
        )
        self._emit_auto_relock_state_transition_log(auto_relock_status)

        auto_relock_enabled = bool(auto_relock_status.get("enabled"))
        if required_detail is not None or auto_relock_enabled:
            self.manager.publish(self.device.key, frame)

    def snapshot(self) -> Dict[str, Any]:
        params_snapshot, plot_frame_snapshot, _, _ = self._snapshot_cached_state()
        return {
            "params": params_snapshot,
            "plot_frame": plot_frame_snapshot,
            "status": self.status(),
        }

    def status(self) -> Dict[str, Any]:
        # Read entirely from cache. Previously this method did two
        # synchronous RPyC calls per device (`exposed_get_logging_status`
        # + `parameters.lock.value`) under `_rpyc_lock`, and
        # /api/devices/statuses fans this out across all 12 devices every
        # 5 s. That serialised the plot poll thread against the status
        # poll and was visible as cursor/button lag in the UI.
        #
        # Both values now come from session-local caches that are kept
        # current by the only callers that can change them
        # (logging_start/stop, and the linien-client change queue).
        logging_active: bool | None = None
        lock_value: bool | None = None
        psd_running: bool | None = None
        if self.connected:
            logging_active = self._logging_active_cache
            cached_lock = self._read_param_fast("lock")
            if cached_lock is not None:
                lock_value = bool(cached_lock)
            cached_psd = self._read_param_fast("psd_acquisition_running")
            if cached_psd is not None:
                psd_running = bool(cached_psd)
        last_plot_timestamp, frame_lock, auto_relock_status, control_metrics = (
            self._snapshot_status_fields()
        )
        if lock_value is None and frame_lock is not None:
            lock_value = frame_lock
        with self._state_lock:
            diagnosis = self._diagnosis_cache
            recovery = dict(self._recovery) if self._recovery is not None else None
        # Stream-stall visibility. The auto-relock state machine only advances on
        # plot-frame arrival, so a stale stream means it is frozen. Surfaced here
        # (REST status) rather than in the websocket frame, because during a stall
        # the frames are exactly what has stopped. `stalled` is only meaningful
        # while auto-relock is enabled.
        stream_age_s: float | None = None
        if last_plot_timestamp is not None:
            stream_age_s = max(0.0, time.time() - float(last_plot_timestamp))
        stalled = bool(
            auto_relock_status.get("enabled")
            and stream_age_s is not None
            and stream_age_s > AUTO_RELOCK_STREAM_STALL_S
        )
        telemetry_fields: Dict[str, Any] = {}
        provider = self._telemetry_provider
        if provider is not None:
            try:
                telemetry_fields = provider(self.device.key) or {}
            except Exception:  # noqa: BLE001 - telemetry must not break status
                logger.debug(
                    "Telemetry status lookup failed for device=%s",
                    self.device.key,
                    exc_info=True,
                )
                telemetry_fields = {}
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "last_error": self.last_error,
            "last_plot": last_plot_timestamp,
            "logging_active": logging_active,
            "lock": lock_value,
            # Mean/std control voltage (V) computed by the lock indicator. Lets a
            # poller (e.g. the EC recenter servo) read the control signal over
            # REST instead of subscribing to the plot stream. null when unlocked
            # / no frame yet.
            "control_mean_v": control_metrics["control_mean_v"],
            "control_std_v": control_metrics["control_std_v"],
            # Mean/std monitor-signal voltage (V), same cadence/nullability as
            # control_mean_v/control_std_v.
            "monitor_mean_v": control_metrics["monitor_mean_v"],
            "monitor_std_v": control_metrics["monitor_std_v"],
            # Error-signal std (error plot-units, a.u.), the PDH discriminator
            # slope from the last auto-lock scan (a.u./MHz), and the derived
            # in-loop lock error (MHz = error_std_v / slope). lock_error_mhz is
            # null when unlocked or no slope has been measured.
            "error_std_v": control_metrics["error_std_v"],
            "discriminator_slope_v_per_mhz": control_metrics[
                "discriminator_slope_v_per_mhz"
            ],
            "lock_error_mhz": control_metrics["lock_error_mhz"],
            "lock_indicator_state": control_metrics["lock_indicator_state"],
            "psd_running": psd_running,
            "auto_relock": auto_relock_status,
            # Seconds since the last plot frame (null if none yet), and whether the
            # stream is stale enough that auto-relock (if enabled) is effectively
            # frozen. See AUTO_RELOCK_STREAM_STALL_S.
            "stream_age_s": stream_age_s,
            "stalled": stalled,
            "diagnosis": diagnosis,
            "recovery": recovery,
            # Red Pitaya (Zynq die) temperature and telemetry-service state.
            # Cache-only; see app/rp_telemetry.py.
            **telemetry_fields,
        }

    def set_param(self, name: str, value: Any, write_registers: bool) -> None:
        if self.parameters is None or self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            param = getattr(self.parameters, name)
            normalized_value = self._normalize_param_value(name, value)
            if name in ("sweep_center", "sweep_amplitude"):
                # The operator's own geometry wins over a pending restore.
                self._deferred_sweep_geometry = None
            if name in self._persistent_param_names:
                self._pending_gateway_param_writes[name] = normalized_value
            try:
                param.value = normalized_value
            except Exception:
                self._pending_gateway_param_writes.pop(name, None)
                raise
            self._update_persistent_setting(name, normalized_value)
            if write_registers:
                self.control.exposed_write_registers()

    def write_registers(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_write_registers()

    def start_lock(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        self._ensure_no_staged_autolock_run("start_lock")
        with self._rpyc_lock:
            self.control.exposed_start_lock()

    def _snapshot_auto_lock_traces_with_frame(
        self,
    ) -> tuple[np.ndarray, np.ndarray | None, int, float | None]:
        """Same traces as ``_snapshot_auto_lock_traces``, plus the identity
        (frame_id, acquired_at) of the exact stored frame they came from,
        captured under the same lock so the two can never refer to different
        frames."""
        with self._state_lock:
            plot_data = self.plot_state.last_plot_data
            if plot_data is None or len(plot_data) < 3:
                raise RuntimeError("No unlocked trace available")
            error_trace_raw = plot_data[2]
            # The *true* monitor (None if the device has no monitor signal), not
            # last_plot_data[1] which is monitor_or_error_signal_2.
            monitor_trace_raw = self.plot_state.last_monitor_signal
            frame_id = int(self.plot_state.last_unlocked_frame_id)
            acquired_at = self.plot_state.last_unlocked_trace_at
        if error_trace_raw is None:
            raise RuntimeError("No error trace available")
        # Plot units: divide by ADC_SCALE (= plot_processing.V) so auto-lock thresholds
        # read on the same fixed scale as the plotted traces (not per-trace normalized).
        error_trace = np.array(error_trace_raw, copy=True) / ADC_SCALE
        monitor_trace = (
            np.array(monitor_trace_raw, copy=True) / ADC_SCALE
            if monitor_trace_raw is not None
            else None
        )
        return error_trace, monitor_trace, frame_id, acquired_at

    def auto_lock_detect(
        self, settings_payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Detect auto-lock candidates against the latest cached unlocked trace,
        WITHOUT locking. Read-only: does not persist settings, touch sweep_center,
        or start the lock. Intended for orchestration: probe, adjust the offset
        (e.g. NLTL), and re-probe before committing to a lock via auto_lock_scan.

        Returns ``{"found", "candidate", "candidates", "reason", "frame"}``:
        - ``candidates``: every accepted crossing (``find_auto_lock_candidates``,
          desc score), each an ``AutoLockScanResult`` dict.
        - ``candidate``: the best-score one (``candidates[0]``), or None.
        - ``reason``: the detector's ``ValueError`` message when nothing
          qualified, else None. Not raised -- "no candidate" is a normal
          diagnostic outcome, not a hard failure.
        - ``frame``: identity/geometry of the analysed frame (frame_id,
          acquired_at, sweep center/amplitude, n_points,
          modulation_frequency_hz, sideband_spacing_samples = median of the
          candidates' sideband_offset_samples, noise_floor = MAD-based robust
          point noise of the error trace). Always present when a frame was
          available to analyse -- including when no candidate qualified.

        Raises ``RuntimeError`` only for a hard failure that leaves no frame to
        report at all: device not connected, or no unlocked trace stored yet.
        """
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        error_trace, monitor_trace, frame_id, acquired_at = (
            self._snapshot_auto_lock_traces_with_frame()
        )

        with self._state_lock:
            if settings_payload is None:
                settings = AutoLockScanSettings.from_mapping(self.auto_lock_scan_settings)
            else:
                settings = AutoLockScanSettings.from_mapping(settings_payload)
        sweep_center, sweep_amplitude, preferred_slope_rising, modulation_frequency_hz = (
            self._snapshot_sweep_params()
        )

        reason: str | None = None
        candidates: list[Any] = []
        try:
            # Traces are in plot units (divided by ADC_SCALE above).
            candidates = find_auto_lock_candidates(
                error_trace_v=error_trace,
                monitor_trace_v=monitor_trace,
                sweep_center_v=sweep_center,
                sweep_amplitude_v=sweep_amplitude,
                settings=settings,
                preferred_slope_rising=preferred_slope_rising,
                modulation_frequency_hz=modulation_frequency_hz,
            )
        except ValueError as exc:
            reason = str(exc)

        sideband_samples = [
            c.sideband_offset_samples
            for c in candidates
            if c.sideband_offset_samples is not None
        ]
        frame = {
            "frame_id": frame_id,
            "acquired_at": acquired_at,
            "sweep_center_v": sweep_center,
            "sweep_amplitude_v": sweep_amplitude,
            "n_points": int(len(error_trace)),
            "modulation_frequency_hz": modulation_frequency_hz,
            "sideband_spacing_samples": (
                float(np.median(sideband_samples)) if sideband_samples else None
            ),
            "noise_floor": float(_robust_noise(np.asarray(error_trace, dtype=float))),
        }
        return {
            "found": bool(candidates),
            "candidate": candidates[0].to_dict() if candidates else None,
            "candidates": [c.to_dict() for c in candidates],
            "reason": reason,
            "frame": frame,
        }

    def auto_lock_candidates_detect(
        self, settings_payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        """`auto_lock_detect`, but run-aware (fix #2b).

        A staged run's `latest_candidates` must come from the run's OWN
        detector and settings -- otherwise a coarse-stage run's candidates
        get silently replaced by this endpoint's strict-only, request/stored
        -settings detection (via `staged_autolock_observe_frame`), and
        IdentityGuard ends up comparing strict against coarse baselines
        (~1.75x apart; see `IdentityGuard`'s own baseline-separation note).

        When a staged run is active, idle, and the CURRENT sweep geometry
        matches the run's, this detects with `run.settings` and the run's
        current detector mode (strict falling back to coarse, exactly like
        `staged_autolock_begin` -- see `_detect_strict_then_coarse`) instead
        of `settings_payload`, and folds the SAME candidate objects/frame/
        detector/resolution directly into the run's `latest_*` fields, so any
        `target_index` the caller picks from this response exists in
        `run.latest_candidates`. `settings_payload` is ignored in that case --
        a run's detection must never depend on what an unrelated read-only
        caller happened to pass. At a different geometry, while the run is
        busy, or with no active run, this is exactly `auto_lock_detect`
        (strict only, request/stored settings) -- today's behaviour --  with
        a `detector` key added to the result for shape consistency.
        """
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        with self._state_lock:
            run = self._staged_autolock
        if run is not None:
            try:
                center_v, amplitude_v, _rising, _mod_hz = self._snapshot_sweep_params()
            except Exception:  # noqa: BLE001 - fall through to the plain path
                center_v = amplitude_v = None
            if (
                not run.busy
                and center_v is not None
                and abs(float(center_v) - run.center_v) <= 1e-9
                and abs(float(amplitude_v) - run.amplitude_v) <= 1e-9
            ):
                return self._staged_run_aware_detect(run)
        result = self.auto_lock_detect(settings_payload)
        result.setdefault("detector", "strict")
        return result

    def _staged_run_aware_detect(self, run: "StagedAutolockRun") -> dict[str, Any]:
        """The run-owned half of `auto_lock_candidates_detect`.

        Detects with `run.settings` (never the endpoint's own settings) and
        adopts the result into the run directly -- rather than relying on
        `staged_autolock_observe_frame`'s round trip through
        `AutoLockScanResult.to_dict()`/reconstruction, which exists for an
        out-of-band caller and would otherwise be the only path folding this
        detection back into the run.
        """
        candidates, center_v, amplitude_v, resolution, detector, frame, reason = (
            self._detect_strict_then_coarse(run.settings, after=None)
        )
        with self._state_lock:
            active = self._staged_autolock
            if (
                active is not None
                and active.token == run.token
                and not active.busy
                and int(frame.get("frame_id", -1))
                > int(active.latest_frame.get("frame_id", -1))
            ):
                active.latest_candidates = candidates
                active.latest_frame = dict(frame)
                active.latest_detector = detector
                active.latest_resolution = resolution
        return {
            "found": bool(candidates),
            "candidate": candidates[0].to_dict() if candidates else None,
            "candidates": [c.to_dict() for c in candidates],
            "reason": reason,
            "frame": frame,
            "detector": detector,
        }

    def _wait_for_fresh_unlocked_trace(
        self, after: float, timeout_s: float, frames: int = VERIFY_TRACE_FRAMES
    ) -> bool:
        """Block until a sweep *acquired* after ``after`` has landed.

        Traces arrive on the plot poll thread; there is no way to demand one, so
        the verification step waits for the next sweep to come round rather than
        re-reading whatever was already in hand (which still shows the feature
        at its pre-move position).

        Two frames, not one. ``last_unlocked_trace_at`` records when a frame was
        PROCESSED, not when the board acquired it, and the poll loop reads
        ``to_plot`` under the same ``_rpyc_lock`` the ramp is using -- so an
        array pulled off the device mid-ramp, or before the settle finished, can
        be processed after ``after`` and would pass a single-frame check. That
        is precisely the un-settled state ``settle_ms`` exists to let decay. The
        second frame cannot have been acquired before the first was processed,
        so it post-dates the move for real.

        The budget is per frame, since each wait is independent.
        """
        wanted = max(1, int(frames))
        deadline = time.time() + max(0.0, float(timeout_s)) * wanted
        threshold = float(after)
        seen = 0
        while True:
            with self._state_lock:
                stamp = self.plot_state.last_unlocked_trace_at
            if stamp is not None and float(stamp) > threshold:
                seen += 1
                threshold = float(stamp)
                if seen >= wanted:
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


    def _maybe_dispatch_relock_action(self, run: Callable[[], None]) -> bool:
        """Claim the relock action and start it, or drop it if one is in flight.

        tick() hands out "relock" on every frame until complete_action lands,
        which was harmless only while the caller ran the action synchronously.
        A duplicate has to be dropped outright: completing it would record a
        spurious failure against the attempt already running and then discard
        that attempt's real result as stale.
        """
        if not self._relock_action_lock.acquire(blocking=False):
            logger.debug(
                "Skipping duplicate relock action for device %s; one is in flight",
                getattr(self.device, "key", "?"),
            )
            return False
        self._dispatch_relock_action(run)
        return True

    def _dispatch_relock_action(self, run: Callable[[], None]) -> None:
        """Run a relock action on its own thread and report it back when done.

        The caller must already hold ``_relock_action_lock``; the worker
        releases it once the result has been applied.
        """

        def _worker() -> None:
            action_ok = True
            action_error: str | None = None
            try:
                run()
            except Exception as exc:  # noqa: BLE001 - recorded as a relock failure
                action_ok = False
                action_error = str(exc)
            try:
                with self._state_lock:
                    self.auto_relock.complete_action(
                        "relock", action_ok, action_error
                    )
            except Exception:  # noqa: BLE001 - never kill the worker thread
                logger.warning(
                    "Failed recording the auto-relock action result", exc_info=True
                )
            finally:
                # Released only after the result is applied: releasing first
                # would let the next frame dispatch again while the controller
                # still reports the action as pending.
                self._relock_action_lock.release()

        thread = threading.Thread(
            target=_worker, name="auto-relock-action", daemon=True
        )
        try:
            thread.start()
        except Exception:
            # The worker's own release never runs if it never started.
            # Leaking the lock here would silently disable every future
            # relock for the life of the process.
            self._relock_action_lock.release()
            raise

    def _current_sweep_center(self) -> float | None:
        """The center as the device currently reports it, or None if unreadable."""
        try:
            with self._rpyc_lock:
                return float(self.parameters.sweep_center.value)
        except Exception:  # noqa: BLE001 - callers fall back to their snapshot
            return None

    @contextmanager
    def _exclusive_center_move(self, what: str):
        """Hold the center-move lock, or refuse rather than interleave.

        Non-blocking on purpose: a second request should be told the actuator is
        busy, not silently queued behind a move that takes seconds.
        """
        if not self._center_move_lock.acquire(blocking=False):
            raise RuntimeError(
                f"Another sweep-center move is already running; {what} was not started."
            )
        try:
            yield
        finally:
            self._center_move_lock.release()

    def _move_and_lock(
        self,
        result: Any,
        sweep_center: float,
    ) -> None:
        """Put the center on the detected target and start the lock.

        Split out of auto_lock_from_scan so the whole move-and-handover runs
        under the center-move lock.
        """
        current_center = self._current_sweep_center()
        if current_center is not None:
            sweep_center = current_center
        with self._rpyc_lock:
            self.parameters.sweep_center.value = float(result.target_voltage)
            self.control.exposed_write_registers()
            self.control.exposed_start_lock()


    # The three static helpers below used to hold this arithmetic directly.
    # It now lives in lock_refinement.py (moved unchanged) alongside the rest
    # of the trajectory-refinement planning; these delegates exist only so
    # existing callers -- production and test alike -- keep working unchanged.
    @staticmethod
    def _min_safe_amplitude_v(
        amplitude_v: float,
        offset_v: float,
        shift_per_fraction: float | None,
        floor_v: float,
    ) -> float | None:
        return min_safe_amplitude_v(amplitude_v, offset_v, shift_per_fraction, floor_v)

    @staticmethod
    def _center_step_allowance_v(
        settings: AutoLockScanSettings,
        amplitude_v: float,
        sideband_offset_v: float | None,
    ) -> float:
        return center_step_allowance_v(settings, amplitude_v, sideband_offset_v)

    @staticmethod
    def _bounded_recenter_v(
        center_v: float,
        target_v: float,
        amplitude_v: float,
        *,
        signal_width_v: float | None = None,
        max_signal_widths: float = 0.0,
    ) -> float:
        return bounded_recenter_v(
            center_v,
            target_v,
            amplitude_v,
            signal_width_v=signal_width_v,
            max_signal_widths=max_signal_widths,
        )

    def _set_sweep_geometry(
        self, center_v: float, amplitude_v: float, *, settle_s: float = 0.0
    ) -> float:
        """Atomically command both scan axes and return the completion timestamp.

        The refinement walk lets `settle_ms` decay before it believes a trace
        (see _apply_center_plan); the refinement walk did not, and started
        counting frames the instant the registers were written. Both axes are
        actuators with a settling tail, so the first frames after the write show
        the scan mid-transition. The timestamp is taken AFTER the settle so that
        the freshness check admits only frames acquired past it.
        """
        with self._rpyc_lock:
            self.parameters.sweep_center.value = float(center_v)
            self.parameters.sweep_amplitude.value = float(amplitude_v)
            self.control.exposed_write_registers()
        if settle_s > 0.0:
            time.sleep(float(settle_s))
        return time.time()

    def _restore_sweep_geometry(self, center_v: float, amplitude_v: float) -> bool:
        """Best-effort restoration after a trajectory refinement failure."""
        try:
            self._set_sweep_geometry(center_v, amplitude_v)
            return True
        except Exception:  # noqa: BLE001 - do not mask the detector failure
            logger.warning(
                "Failed restoring sweep geometry to center %.4f V, amplitude %.4f V",
                center_v,
                amplitude_v,
                exc_info=True,
            )
            return False

    def _capture_auto_lock_target(
        self,
        settings: AutoLockScanSettings,
        *,
        after: float | None = None,
        traces: tuple[Any, Any] | None = None,
    ) -> tuple[Any, float, float, float]:
        """Capture one fresh trace and detect it against its read-back geometry.

        Waiting after every geometry write makes the trace/detection pair atomic
        from the caller's perspective: no target from a previous scan window is
        ever carried into the next stage. Pass `traces` to reuse a trace the
        caller already fetched instead of taking a second snapshot.
        """
        if after is not None:
            timeout_s = self._unlocked_trace_timeout_s()
            if not self._wait_for_fresh_unlocked_trace(after, timeout_s):
                raise RuntimeError("No fresh sweep arrived after changing scan geometry.")
        if traces is not None:
            error_trace, monitor_trace = traces
        else:
            error_trace, monitor_trace = self._snapshot_auto_lock_traces()
        center_v, amplitude_v, rising, mod_hz = self._snapshot_sweep_params(
            require_unlocked=True
        )
        result = find_auto_lock_target(
            error_trace_v=error_trace,
            monitor_trace_v=monitor_trace,
            sweep_center_v=center_v,
            sweep_amplitude_v=amplitude_v,
            settings=settings,
            preferred_slope_rising=rising,
            modulation_frequency_hz=mod_hz,
        )
        return result, center_v, amplitude_v, feature_resolution_samples(
            settings, len(error_trace), amplitude_v
        )

    def _coarse_auto_lock_target(
        self, settings: AutoLockScanSettings, *, after: float | None = None
    ) -> tuple[Any, float, float, float, dict[str, Any]]:
        """A permissive detector used only to track a feature into a fresh scan.

        It can never start a lock. The final two detections always use the
        calibrated strict settings.
        """
        if after is not None:
            timeout_s = self._unlocked_trace_timeout_s()
            if not self._wait_for_fresh_unlocked_trace(after, timeout_s):
                raise RuntimeError("No fresh sweep arrived after changing scan geometry.")
        error_trace, monitor_trace = self._snapshot_auto_lock_traces()
        center_v, amplitude_v, rising, mod_hz = self._snapshot_sweep_params(
            require_unlocked=True
        )
        candidate = find_coarse_auto_lock_target(
            error_trace_v=error_trace,
            monitor_trace_v=monitor_trace,
            sweep_center_v=center_v,
            sweep_amplitude_v=amplitude_v,
            settings=settings,
            preferred_slope_rising=rising,
            modulation_frequency_hz=mod_hz,
        )
        return candidate.result, center_v, amplitude_v, feature_resolution_samples(
            settings, len(error_trace), amplitude_v
        ), candidate.metrics

    def _capture_auto_lock_candidates_strict(
        self,
        settings: AutoLockScanSettings,
        *,
        after: float | None = None,
    ) -> tuple[list[Any], float, float, float, dict[str, Any]]:
        """Every strict candidate on one fresh trace, plus its frame identity.

        Used by the staged API, which must hand the caller every accepted
        crossing to choose from -- never only the best-score one -- and must
        track which frame they came from (frame_id/acquired_at) so a later
        stage can be refused for referencing a stale one. Raises the same
        ``ValueError`` as `find_auto_lock_candidates` when none qualify. Does
        not replace `_capture_auto_lock_target`, which stays the one-shot
        loop's exact historical call (`find_auto_lock_target` is itself
        `find_auto_lock_candidates(...)[0]`, so the two never disagree).
        """
        if after is not None:
            timeout_s = self._unlocked_trace_timeout_s()
            if not self._wait_for_fresh_unlocked_trace(after, timeout_s):
                raise RuntimeError("No fresh sweep arrived after changing scan geometry.")
        error_trace, monitor_trace, frame_id, acquired_at = (
            self._snapshot_auto_lock_traces_with_frame()
        )
        center_v, amplitude_v, rising, mod_hz = self._snapshot_sweep_params(
            require_unlocked=True
        )
        candidates = find_auto_lock_candidates(
            error_trace_v=error_trace,
            monitor_trace_v=monitor_trace,
            sweep_center_v=center_v,
            sweep_amplitude_v=amplitude_v,
            settings=settings,
            preferred_slope_rising=rising,
            modulation_frequency_hz=mod_hz,
        )
        resolution = feature_resolution_samples(settings, len(error_trace), amplitude_v)
        frame = _frame_summary(
            error_trace, frame_id, acquired_at, center_v, amplitude_v, mod_hz, candidates
        )
        return candidates, center_v, amplitude_v, resolution, frame

    def _coarse_auto_lock_candidates(
        self, settings: AutoLockScanSettings, *, after: float | None = None
    ) -> tuple[list[Any], float, float, float, dict[str, Any], dict[str, Any]]:
        """Every accepted coarse extrema-pair candidate on one fresh trace.

        Unlike the one-shot loop's `_coarse_auto_lock_target` (which only
        ever surfaces the single best-score pair), the staged API must hand
        the caller every coarse candidate the tracker accepted -- several
        identical-morphology PDH features can be present, and the strict
        detector rejecting the frame must not collapse the caller's choice of
        serrodyne order down to whichever one happens to score highest (see
        `find_coarse_auto_lock_candidates`). ``metrics`` is the best-score
        candidate's (candidates[0]'s) metrics dict, matching what
        `_coarse_auto_lock_target` would have reported for this frame.
        """
        if after is not None:
            timeout_s = self._unlocked_trace_timeout_s()
            if not self._wait_for_fresh_unlocked_trace(after, timeout_s):
                raise RuntimeError("No fresh sweep arrived after changing scan geometry.")
        error_trace, monitor_trace, frame_id, acquired_at = (
            self._snapshot_auto_lock_traces_with_frame()
        )
        center_v, amplitude_v, rising, mod_hz = self._snapshot_sweep_params(
            require_unlocked=True
        )
        candidates = find_plausible_coarse_candidates(
            error_trace_v=error_trace,
            monitor_trace_v=monitor_trace,
            sweep_center_v=center_v,
            sweep_amplitude_v=amplitude_v,
            settings=settings,
            preferred_slope_rising=rising,
            modulation_frequency_hz=mod_hz,
        )
        if not candidates:
            raise ValueError("No plausible coarse candidate on this frame.")
        resolution = feature_resolution_samples(settings, len(error_trace), amplitude_v)
        results = [c.result for c in candidates]
        frame = _frame_summary(
            error_trace, frame_id, acquired_at, center_v, amplitude_v, mod_hz, results
        )
        return results, center_v, amplitude_v, resolution, candidates[0].metrics, frame

    @dataclasses.dataclass
    class _RefinementStageOutcome:
        """One `plan_refinement_step` decision, executed once.

        ``target`` is None for ``"done"``/``"refuse"`` (nothing was detected).
        The ``before_*`` fields are the geometry/detector in effect BEFORE this
        stage's write, handed back so the caller can measure the width- and
        centre-induced shift the same way regardless of which detection path
        produced the new target.
        """

        step: Any
        target: Any | None
        center_v: float
        amplitude_v: float
        resolution: float
        detector: str
        coarse_metrics: dict[str, Any] | None
        before_v: float
        before_amplitude: float
        before_center: float
        before_detector: str

    def _run_refinement_stage(
        self,
        settings: AutoLockScanSettings,
        *,
        center_v: float,
        amplitude_v: float,
        target: Any,
        detector: str,
        trace_length: int,
        shift_per_fraction: float | None,
        geometry_settle_s: float,
        detect_narrow: Callable[
            [float, float, float],
            tuple[Any, float, float, float, str, dict[str, Any] | None],
        ],
        detect_recenter: Callable[
            [float, float, float],
            tuple[Any, float, float, float, dict[str, Any] | None],
        ],
    ) -> "DeviceSession._RefinementStageOutcome":
        """Plan and execute exactly one refinement stage.

        THE single place that asks ``plan_refinement_step`` (the one planner)
        what to do next and acts on it. Shared by the one-shot auto-lock loop
        and the staged step-by-step API (see ``staged_autolock_step``): both
        call this; they differ only in HOW they detect after the geometry
        write, which is why detection is injected as the
        ``detect_narrow``/``detect_recenter`` callbacks rather than hard-coded
        here. The one-shot loop's callbacks are exactly its historical calls
        to ``_capture_auto_lock_target``/``_coarse_auto_lock_target``, so its
        behaviour -- and every existing test that mocks those two methods --
        is unchanged. The staged path's callbacks return every candidate on
        the fresh frame instead of picking one, for the caller to choose from.

        Bookkeeping that the two callers do differently -- stage-log entries,
        IdentityGuard ordering/mutation, ``shift_per_fraction`` updates,
        ``narrow_count``, the "did it actually narrow" guard -- stays with the
        caller; this method makes no I/O beyond the two callbacks and never
        raises on the planner's behalf (a ``"refuse"`` step is returned, not
        thrown).
        """
        step = plan_refinement_step(
            settings,
            center_v=center_v,
            amplitude_v=amplitude_v,
            target_v=float(target.target_voltage),
            sideband_offset_v=target.sideband_offset_v,
            detector=detector,
            trace_length=trace_length,
            shift_per_fraction=shift_per_fraction,
        )
        before_v = float(target.target_voltage)
        before_amplitude = abs(amplitude_v)
        before_center = float(center_v)
        before_detector = detector
        if step.action in ("done", "refuse"):
            return DeviceSession._RefinementStageOutcome(
                step=step,
                target=None,
                center_v=center_v,
                amplitude_v=amplitude_v,
                resolution=0.0,
                detector=detector,
                coarse_metrics=None,
                before_v=before_v,
                before_amplitude=before_amplitude,
                before_center=before_center,
                before_detector=before_detector,
            )
        if step.action == "recenter":
            new_target, new_center_v, new_amplitude_v, resolution, coarse_metrics = (
                detect_recenter(step.center_v, amplitude_v, geometry_settle_s)
            )
            new_detector = "coarse"
        else:  # "narrow" or "rail_escape" -- both write geometry and detect
            next_center = center_v if step.action == "rail_escape" else step.center_v
            (
                new_target, new_center_v, new_amplitude_v, resolution,
                new_detector, coarse_metrics,
            ) = detect_narrow(next_center, step.amplitude_v, geometry_settle_s)
        return DeviceSession._RefinementStageOutcome(
            step=step,
            target=new_target,
            center_v=new_center_v,
            amplitude_v=new_amplitude_v,
            resolution=resolution,
            detector=new_detector,
            coarse_metrics=coarse_metrics,
            before_v=before_v,
            before_amplitude=before_amplitude,
            before_center=before_center,
            before_detector=before_detector,
        )

    def _trajectory_refine_auto_lock(
        self,
        settings: AutoLockScanSettings,
        acceptance: AcceptanceSettings,
        start_center_v: float,
        start_amplitude_v: float,
        *,
        initial_target: Any,
        initial_center_v: float,
        initial_amplitude_v: float,
        initial_resolution: float,
        initial_detector: str = "coarse",
        trace_length: int,
    ) -> tuple[Any, dict[str, Any]]:
        """Track an under-resolved feature through gradual scan changes.

        The actuator's apparent carrier position may change with scan trajectory,
        so each stage throws away its predecessor's voltage and redetects on a
        newly acquired trace. A failed tracking run restores both axes.

        Reached only when the strict detector REJECTED the live trace, so the
        seed is normally a coarse candidate (``initial_detector="coarse"``) and
        the loop narrows until strict agrees. Refinement exists to rescue a scan
        that cannot be locked as it stands; it must never be handed a strict
        detection that already succeeded, because it can only discard it.
        """
        stages: list[dict[str, Any]] = []
        target, center_v, amplitude_v, resolution = (
            initial_target, initial_center_v, initial_amplitude_v, initial_resolution
        )
        detector = str(initial_detector)
        coarse_metrics: dict[str, Any] | None = None
        try:
            stages.append({
                "kind": "initial", "center_v": center_v, "amplitude_v": amplitude_v,
                "target_voltage": target.target_voltage, "resolution_samples": resolution,
                "detector": detector, "sideband_offset_v": target.sideband_offset_v,
                "metrics": coarse_metrics,
            })
            identity = IdentityGuard(
                target, resolution, trace_length=trace_length, detector=detector
            )

            # Narrow in <=25% reductions until the STRICT detector accepts a
            # trace -- that, not a sample count, is the thing refinement is
            # trying to obtain, and it is the only exit that can start a lock.
            # Narrowing further once strict agrees would spend sweeps (and,
            # on a feature that moves with geometry, accuracy) for nothing.
            # Centre moves are intentionally separate;
            # they are known to perturb this DFB's apparent feature position.
            narrow_count = 0
            # Both sweep axes are actuators, so a geometry write needs the same
            # `settle_ms` before trusting a trace; a refinement stage commands a
            # settle a centre move needs before the trace can be believed.
            geometry_settle_s = max(0.0, float(acceptance.settle_ms) / 1000.0)
            # Narrowing the scan moves the feature too: changing the ramp width
            # changes the actuator's trajectory, and the apparent resonance
            # follows. Measured on this device at 73 mV for one 2x narrowing --
            # larger than the 65 mV signal width the centre steps are bounded
            # by, so an unbounded width change is the bigger move of the two.
            # Each stage measures it (shift per unit fractional width change)
            # and the next stage is sized from what was actually observed.
            shift_per_fraction: float | None = None
            while detector == "coarse" or scan_too_wide_to_lock(
                settings, amplitude_v, target.sideband_offset_v,
                trace_points=trace_length,
            ):
                if narrow_count >= _MAX_REFINEMENT_STAGES:
                    raise ValueError(
                        f"No lockable scan after {_MAX_REFINEMENT_STAGES} "
                        "trajectory refinement stages."
                    )
                # Ask the planner what to do next -- see plan_refinement_step's
                # docstring for the constraint order. `_run_refinement_stage`
                # is the one place that calls it and acts on the result; the
                # detect_narrow/detect_recenter callbacks below are exactly
                # this loop's historical calls to `_capture_auto_lock_target`/
                # `_coarse_auto_lock_target`, so behaviour (and every test
                # mocking those two methods) is unchanged.
                def _detect_narrow(next_center_v, next_amplitude_v, settle_s):
                    moved_at = self._set_sweep_geometry(
                        next_center_v, next_amplitude_v, settle_s=settle_s
                    )
                    try:
                        t, c, a, r = self._capture_auto_lock_target(
                            settings, after=moved_at
                        )
                        return t, c, a, r, "strict", None
                    except ValueError:
                        t, c, a, r, m = self._coarse_auto_lock_target(
                            settings, after=moved_at
                        )
                        return t, c, a, r, "coarse", m

                def _detect_recenter(next_center_v, next_amplitude_v, settle_s):
                    moved_at = self._set_sweep_geometry(
                        next_center_v, next_amplitude_v, settle_s=settle_s
                    )
                    return self._coarse_auto_lock_target(settings, after=moved_at)

                outcome = self._run_refinement_stage(
                    settings,
                    center_v=center_v,
                    amplitude_v=amplitude_v,
                    target=target,
                    detector=detector,
                    trace_length=trace_length,
                    shift_per_fraction=shift_per_fraction,
                    geometry_settle_s=geometry_settle_s,
                    detect_narrow=_detect_narrow,
                    detect_recenter=_detect_recenter,
                )
                step = outcome.step
                if step.action == "done":
                    break
                if step.action == "refuse":
                    raise ValueError(step.reason)
                if step.action == "recenter":
                    # The planner reaches this only when no width change is
                    # safe yet. Move the centre alone and re-plan from what the
                    # next detection actually finds; carrying a pre-move
                    # amplitude across the write would size the cut from
                    # geometry that no longer exists.
                    target, center_v, amplitude_v, resolution, coarse_metrics = (
                        outcome.target, outcome.center_v, outcome.amplitude_v,
                        outcome.resolution, outcome.coarse_metrics,
                    )
                    detector = "coarse"
                    identity.check(target, amplitude_v=amplitude_v, detector="coarse", resolution_samples=resolution)
                    stages.append({"kind": "recenter", "center_v": center_v,
                                   "amplitude_v": amplitude_v, "target_voltage": target.target_voltage,
                                   "resolution_samples": resolution, "detector": "coarse",
                                   "sideband_offset_v": target.sideband_offset_v,
                                   "metrics": coarse_metrics, "bounds": step.bounds})
                    narrow_count += 1
                    continue
                if step.action == "rail_escape":
                    stages.append({
                        "kind": "rail_blocked", "center_v": center_v,
                        "amplitude_v": amplitude_v,
                        "target_voltage": target.target_voltage,
                        "resolution_samples": resolution, "detector": detector,
                        "sideband_offset_v": target.sideband_offset_v,
                        "rail_v": step.bounds.get("rail_v", 1.0 - abs(amplitude_v)),
                        "next_amplitude_v": step.amplitude_v,
                        "bounds": step.bounds,
                    })
                before_v = outcome.before_v
                before_amplitude = outcome.before_amplitude
                before_center = outcome.before_center
                before_detector = outcome.before_detector
                target, center_v, amplitude_v, resolution, detector, coarse_metrics = (
                    outcome.target, outcome.center_v, outcome.amplitude_v,
                    outcome.resolution, outcome.detector, outcome.coarse_metrics,
                )
                # What the width change actually did to the apparent position.
                width_shift_v = abs(float(target.target_voltage) - before_v)
                width_fraction = (
                    1.0 - (abs(amplitude_v) / before_amplitude)
                    if before_amplitude > 1e-12 else 0.0
                )
                # Only a same-detector pair measures the actuator. The strict
                # detector and the coarse tracker can settle on different
                # crossings of a multi-feature scan, and their difference --
                # 100 mV in the field -- is not a width-induced shift. Charging
                # it to the shift estimate spends the stage allowance on it,
                # which is what left a narrowing stage with no centre budget at
                # all: it took the width change and skipped the centring.
                # A narrowing stage moves the centre in the same register
                # write, and both changes move the apparent feature. Dividing
                # the WHOLE observed move by the width fraction alone charges
                # the centre's share to the width, and the smaller the cut the
                # larger the bogus coefficient: the field case cut 22.6 mV of
                # half-range while commanding the centre 40.3 mV, and the
                # resulting 27.8 mV move over a 0.096 fraction read as 0.289 V
                # per unit fraction -- double the 0.144 the same walk had
                # measured on stages where the width did dominate. The max()
                # below then made that permanent, and it throttled every
                # subsequent cut and centre step until the walk gave up.
                # So only a stage whose width change outweighs its centre
                # change is allowed to speak for the width. Both are sweep
                # volts, so the comparison carries no scale of its own.
                width_delta_v = max(0.0, before_amplitude - abs(amplitude_v))
                center_delta_v = abs(float(center_v) - before_center)
                if (
                    width_fraction > _REFINEMENT_MIN_MEASURABLE_FRACTION
                    and detector == before_detector
                    and width_delta_v >= center_delta_v
                ):
                    observed = width_shift_v / width_fraction
                    # Keep the worst seen: one gentle stage must not talk the
                    # walk back into a step a harsher one already showed is big.
                    shift_per_fraction = (
                        observed if shift_per_fraction is None
                        else max(shift_per_fraction, observed)
                    )
                stages.append({
                    "kind": "narrow", "center_v": center_v, "amplitude_v": amplitude_v,
                    "target_voltage": target.target_voltage, "resolution_samples": resolution,
                    "detector": detector, "metrics": coarse_metrics if detector == "coarse" else None,
                    "sideband_offset_v": target.sideband_offset_v,
                    "width_shift_v": width_shift_v,
                    "center_shift_v": center_delta_v,
                    "shift_per_fraction_v": shift_per_fraction,
                    "bounds": step.bounds,
                })
                identity.check(target, amplitude_v=amplitude_v, detector=detector, resolution_samples=resolution)
                # Checked after the identity guard: if the walk has lost the
                # feature, that is the more specific diagnosis. A narrowing that
                # did not narrow would otherwise be re-planned identically next
                # stage, and the walk would spend its whole budget asking for
                # the same width before reporting only that it ran out of
                # stages. The realized amplitude is read back from the device,
                # so a register that quantizes or clamps the request surfaces on
                # the first stage rather than the sixteenth.
                if (
                    step.action == "narrow"
                    and step.amplitude_v < before_amplitude
                    and abs(amplitude_v) >= before_amplitude
                ):
                    raise ValueError(
                        f"The scan width did not change: {before_amplitude:.4f} V "
                        f"was asked to narrow to {step.amplitude_v:.4f} V and read "
                        f"back {abs(amplitude_v):.4f} V. The sweep amplitude is not "
                        "following the commanded value."
                    )
                narrow_count += 1
                # A target still outside the inner window is the next stage's
                # business: plan_refinement_step weighs it against the crop
                # floor, the rails and the step allowance together. Correcting
                # it here spent a second full centre allowance inside the same
                # stage and handed the loop back a coarse detection, which is
                # what made the walk alternate detectors -- and the two then
                # disagreed about where the feature was.

            # The coarse result only guides geometry. Demand two fresh strict
            # detections at the final unchanged geometry before the handover.
            try:
                strict_one, center_v, amplitude_v, resolution = self._capture_auto_lock_target(
                    settings, after=time.time()
                )
            except (ValueError, RuntimeError) as first_final_error:
                # A geometry transition can have a short thermal/piezo tail.
                # Retry once after a real settle interval before declaring the
                # feature lost; never reuse the rejected frame.
                time.sleep(0.3)
                stages.append({"kind": "settle_retry", "detail": str(first_final_error)})
                strict_one, center_v, amplitude_v, resolution = self._capture_auto_lock_target(
                    settings, after=time.time()
                )
            # Sideband spacing is a derived quantity with its own measurement
            # noise. It earns its keep ACROSS geometry changes, where the target
            # voltage legitimately moves and another invariant is needed. These
            # two detections are at one unchanged geometry, where the position
            # check below is strictly stronger: same slope and same voltage is
            # the same crossing, whatever the sideband fit did. Applying it here
            # only adds a way to fail.
            identity.check(
                strict_one, amplitude_v=amplitude_v, resolution_samples=resolution, check_sideband=False
            )
            verify_after = time.time()
            one_at = time.time()
            strict_two, verify_center, verify_amplitude, _ = self._capture_auto_lock_target(
                settings, after=verify_after
            )
            two_at = time.time()
            identity.check(
                strict_two, amplitude_v=amplitude_v, resolution_samples=resolution, check_sideband=False
            )
            # One definition of "the feature moved too far", taken from the
            # acceptance settings rather than a second inline literal that
            # silently diverges from capture_fraction when anyone changes it.
            # Floored at one sample: nothing can be resolved finer than that.
            window = acceptance_window_v(
                acceptance, settings.half_range_sweep_v, strict_two.sideband_offset_v
            )
            tolerance = max(
                window.tolerance_v, 2.0 * abs(amplitude_v) / max(1, trace_length - 1)
            )
            # Four distinct failures. They used to share one message with no
            # numbers in it, which says nothing about which one fired.
            if abs(verify_center - center_v) > 1e-6:
                raise ValueError(
                    f"Sweep center moved between the two final detections: "
                    f"{center_v:.6f} V then {verify_center:.6f} V."
                )
            if abs(verify_amplitude - amplitude_v) > 1e-6:
                raise ValueError(
                    f"Sweep amplitude moved between the two final detections: "
                    f"{amplitude_v:.6f} V then {verify_amplitude:.6f} V."
                )
            if strict_one.target_slope_rising != strict_two.target_slope_rising:
                raise _TrackingIdentityChanged(
                    "The two final detections disagreed on the discriminator slope."
                )
            drift_v = abs(strict_one.target_voltage - strict_two.target_voltage)
            # Two questions, not one. They were conflated into a single
            # displacement test that a drifting laser cannot pass: verifying a
            # detection costs two fresh sweeps, so the two readings are seconds
            # apart, while the capture window is half a feature half-width. On
            # the characterization device -- 1-3 mV/s of drift against a 0.889 mV
            # window -- any honest pair of verifications is 2-3 mV apart and was
            # refused, however well the walk had converged.
            #
            # 1. Are these the same crossing? The scale for that is the distance
            #    to the next feature, which is what the neighbour guard already
            #    derives; the capture window says nothing about it.
            # rejection_bound_v is already defined as the offset beyond which a
            # re-detection is a different crossing rather than a moved one, so
            # it is the threshold -- not half of it. On the field device that
            # bound is 7.1 mV and an honest pair of verifications is 2-3 mV
            # apart; halving it would leave 7% of headroom.
            neighbour_bound = window.bound_v
            if neighbour_bound is not None and drift_v > neighbour_bound:
                raise _TrackingIdentityChanged(
                    f"The two final detections were {drift_v * 1e3:.3f} mV apart, "
                    f"past the {neighbour_bound * 1e3:.3f} mV neighbour bound: "
                    "they are not the same crossing."
                )
            # 2. Is it slow enough to hand over? What matters is not how far the
            #    feature moved while being verified, but how far it will move
            #    between the last detection and the lock engaging -- the settle
            #    the handover already waits. Comparing a multi-second drift
            #    against a capture window the handover never spans is what made
            #    this unsatisfiable.
            interval_s = max(0.0, two_at - one_at)
            handover_s = max(0.0, float(acceptance.settle_ms) / 1000.0)
            if interval_s > 1e-3:
                drift_rate_v_s = drift_v / interval_s
                predicted_v = drift_rate_v_s * handover_s
                if predicted_v > tolerance:
                    raise ValueError(
                        f"The feature is drifting at {drift_rate_v_s * 1e3:.2f} mV/s "
                        f"({drift_v * 1e3:.3f} mV between two detections "
                        f"{interval_s:.2f} s apart), so it moves "
                        f"{predicted_v * 1e3:.3f} mV during the {handover_s * 1e3:.0f} ms "
                        f"handover -- outside the {tolerance * 1e3:.3f} mV capture "
                        f"window (capture_fraction {float(acceptance.capture_fraction):g} "
                        f"x feature half-width "
                        f"{float(settings.half_range_sweep_v) * 1e3:.3f} mV). "
                        "Shorten settle_ms, or stabilise the laser."
                    )
            elif drift_v > tolerance:
                # No usable interval to derive a rate from: fall back to the
                # displacement, which is all that can be said.
                raise ValueError(
                    f"The two final detections were {drift_v * 1e3:.3f} mV apart, "
                    f"outside the {tolerance * 1e3:.3f} mV acceptance window "
                    f"(capture_fraction {float(acceptance.capture_fraction):g} x feature "
                    f"half-width {float(settings.half_range_sweep_v) * 1e3:.3f} mV)."
                )
            stages.append({
                "kind": "final_verify", "center_v": center_v, "amplitude_v": amplitude_v,
                "target_voltage": strict_two.target_voltage, "resolution_samples": resolution,
                "detector": "strict", "sideband_offset_v": strict_two.sideband_offset_v,
                "consistent": True,
            })
            return strict_two, {
                "attempted": True,
                "trigger": "under_resolved",
                "original_center_v": start_center_v,
                "original_amplitude_v": start_amplitude_v,
                "final_center_v": center_v,
                "final_amplitude_v": amplitude_v,
                "initial_resolution_samples": initial_resolution,
                "stages": stages,
                "restored": False,
            }
        except Exception as exc:
            restored = self._restore_sweep_geometry(start_center_v, start_amplitude_v)
            diagnostics = {
                "attempted": True,
                "original_center_v": start_center_v,
                "original_amplitude_v": start_amplitude_v,
                "initial_resolution_samples": initial_resolution,
                "stages": stages,
                "restored": restored,
                "failure": str(exc),
                "failure_kind": (
                    "identity"
                    if isinstance(exc, _TrackingIdentityChanged)
                    else "position"
                    if isinstance(exc, ValueError)
                    else "other"
                ),
            }
            raise TrajectoryRefinementAborted(
                f"Trajectory-aware auto-lock refinement failed: {exc} "
                f"(scan geometry {'restored' if restored else 'could not be restored'}).",
                diagnostics,
            ) from exc

    # ---------------------------------------------------------- staged autolock

    def staged_autolock_begin_precheck(self) -> None:
        """Refuse BEFORE any device I/O that would touch sweep/lock state.

        `staged_autolock/begin`'s route used to call `_trigger_and_acquire`
        (-> `_prepare_sweep` -> `start_sweep()`, which switches the lock OFF)
        before ever asking whether the request should be allowed at all -- so
        `begin` on a locked laser unlocked it, and a second `begin` during an
        active run restarted the sweep underneath that run. This runs the
        same refusals `staged_autolock_begin` itself applies -- already-active
        run, device locked -- plus the center-move-lock probe fix #3 needs
        (a one-shot refinement walk in flight), all off the event loop and
        with no side effect, so the route can 409 before ever triggering
        anything. Raises `RuntimeError` (mapped to 409 by the route).
        """
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        self._ensure_no_staged_autolock_run("staged auto-lock begin")
        # require_unlocked=True raises "Device is already locked..." without
        # writing anything.
        self._snapshot_sweep_params(require_unlocked=True)
        if not self._center_move_lock.acquire(blocking=False):
            raise RuntimeError(
                "Another sweep-center move is already running; staged "
                "auto-lock begin was not started."
            )
        self._center_move_lock.release()

    def auto_lock_candidates_acquire_precheck(self) -> None:
        """Refuse `auto_lock_candidates?acquire=true` BEFORE it triggers a
        sweep restart, if the device is locked (fix #1). Unlike `begin`, an
        active staged run does NOT refuse this call -- the orchestrator is
        explicitly allowed to probe with `acquire=true` while a staged run is
        active (see `staged_autolock_observe_frame`); only "locked" does.
        """
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        self._snapshot_sweep_params(require_unlocked=True)

    def _ensure_no_staged_autolock_run(self, what: str) -> None:
        with self._state_lock:
            active = self._staged_autolock
        if active is not None:
            raise RuntimeError(
                f"A staged auto-lock run is active (token {active.token}); "
                f"{what} was refused."
            )

    def _get_staged_run_or_raise(self, token: str) -> "StagedAutolockRun":
        """Caller must hold `_state_lock`."""
        run = self._staged_autolock
        if run is None:
            raise StagedAutolockError(
                "No staged auto-lock run is active.", status_code=404
            )
        if run.token != token:
            raise StagedAutolockError(
                "Unknown or expired staged auto-lock token.", status_code=404
            )
        return run

    def _get_idle_staged_run_or_raise(self, token: str) -> "StagedAutolockRun":
        """`_get_staged_run_or_raise`, also refusing a run that is `busy`.

        Caller must hold `_state_lock`. Used by `step`/`lock`/`abort`: while a
        run is busy with an in-flight `step`/`lock` (which released
        `_state_lock` for slow I/O -- a geometry write, a fresh-frame wait), a
        second call on the same token must not run concurrently with it or
        tear down the run underneath it, so it is refused with 409 rather than
        racing. See `StagedAutolockRun.busy`.
        """
        run = self._get_staged_run_or_raise(token)
        if run.busy:
            raise StagedAutolockError(
                "Staged auto-lock run busy with an in-flight step/lock.",
                status_code=409,
            )
        return run

    def _staged_autolock_finish_busy(self, token: str) -> None:
        """`step`/`lock`'s `finally`: clear `busy` and expire an overdue run.

        The TTL timer callback (`_staged_autolock_expire`) refuses to restore
        geometry while `busy` is set, so a run whose deadline passed during an
        in-flight operation is otherwise never expired. Checking the deadline
        here, right after the operation that held `busy` is done, is what
        actually expires it -- "expire right after the operation finishes if
        the deadline has passed" from the spec. A no-op if the run already
        ended (e.g. `lock` succeeded and cleared it itself).
        """
        expire_now = False
        with self._state_lock:
            run = self._staged_autolock
            if run is not None and run.token == token:
                run.busy = False
                if run.expires_at <= time.time():
                    expire_now = True
        if expire_now:
            self._staged_autolock_expire(token)

    def _consume_pending_shift_measurement(
        self, run: "StagedAutolockRun", selected: Any
    ) -> None:
        """Finish a width-shift measurement deferred by the prior stage.

        Caller must hold `_state_lock`. `staged_autolock_step` used to measure
        the width-induced shift (and derive `needs_more_refinement`) from
        `outcome.target` -- the best-score candidate on the new frame, which
        may be a different serrodyne order than the one the caller is about
        to select. The measurement is only meaningful for the SAME feature
        before and after the geometry change, so it must wait for the
        caller's selection on the new frame -- exactly the same formula and
        same-detector/width-dominates conditions the one-shot loop applies in
        `_trajectory_refine_auto_lock`. No-op if the current frame was not
        produced by a geometry change (`pending_shift` is None).
        """
        pending = run.pending_shift
        if pending is None:
            return
        run.pending_shift = None
        before_v = float(pending["before_v"])
        before_amplitude = float(pending["before_amplitude"])
        before_center = float(pending["before_center"])
        before_detector = str(pending["before_detector"])
        detector = str(pending["detector"])
        amplitude_v = float(pending["amplitude_v"])
        center_v = float(pending["center_v"])
        width_shift_v = abs(float(selected.target_voltage) - before_v)
        width_fraction = (
            1.0 - (abs(amplitude_v) / before_amplitude)
            if before_amplitude > 1e-12 else 0.0
        )
        width_delta_v = max(0.0, before_amplitude - abs(amplitude_v))
        center_delta_v = abs(center_v - before_center)
        if (
            width_fraction > _REFINEMENT_MIN_MEASURABLE_FRACTION
            and detector == before_detector
            and width_delta_v >= center_delta_v
        ):
            observed = width_shift_v / width_fraction
            run.shift_per_fraction = (
                observed if run.shift_per_fraction is None
                else max(run.shift_per_fraction, observed)
            )

    @staticmethod
    def _augment_candidates_with_lockable_here(
        annotated: list[dict[str, Any]],
        candidates: list[Any],
        *,
        settings: AutoLockScanSettings,
        detector: str,
        amplitude_v: float,
        trace_length: int,
    ) -> bool:
        """Adds `lockable_here` to each annotated candidate dict in place.

        ``lockable_here`` = the strict detector is the one that produced this
        frame AND this specific candidate's own measured sideband spacing
        does not make the scan too wide to lock from -- the same test the
        one-shot loop's refinement-while-loop applies, but per candidate
        instead of only for the best-score one (see spec C2 fix #3). Returns
        whether ANY candidate on the frame is lockable, which is what the
        top-level (frame-level) `needs_more_refinement` compatibility key is
        defined as the negation of.
        """
        any_lockable = False
        for payload, candidate in zip(annotated, candidates):
            lockable = detector == "strict" and not scan_too_wide_to_lock(
                settings, amplitude_v, candidate.sideband_offset_v,
                trace_points=trace_length,
            )
            payload["lockable_here"] = bool(lockable)
            any_lockable = any_lockable or lockable
        return any_lockable

    def _staged_autolock_expire(self, token: str) -> None:
        """Timer callback: auto-abort a run whose TTL elapsed with no call.

        Never restores geometry out from under an in-flight `step`/`lock`:
        while `busy`, the run is left alone and its own `finally`
        (`_staged_autolock_finish_busy`) expires it as soon as it completes,
        if the deadline has by then passed.

        Fix #5: also checks `expires_at`, not just `busy`. `renew` re-arms a
        new `threading.Timer` and cancels the old one, but `Timer.cancel()`
        cannot stop a callback that has already started running -- if this
        callback's thread was already past that point when `renew` ran
        (renew held `_state_lock` first), it lands here AFTER `renew` already
        extended `expires_at`. Without this check it would abort a run the
        client was just told was extended; the new timer armed by `renew`
        will handle the real expiry when it actually arrives.
        """
        with self._state_lock:
            run = self._staged_autolock
            if run is None or run.token != token:
                return
            if run.busy:
                return
            if time.time() < run.expires_at:
                return
            self._staged_autolock = None
        restored = self._restore_sweep_geometry(
            run.restore_center_v, run.restore_amplitude_v
        )
        logger.info(
            "Staged auto-lock run %s expired after %.1f s device=%s; "
            "geometry %s.",
            token, run.ttl_s, getattr(self.device, "key", "?"),
            "restored" if restored else "could not be restored",
        )

    def _staged_autolock_arm_timer(
        self, run: "StagedAutolockRun", ttl_s: float
    ) -> None:
        """Replace `run`'s TTL timer. Caller must hold `_state_lock`."""
        old_timer = run.timer
        timer = threading.Timer(
            max(0.001, float(ttl_s)), self._staged_autolock_expire, args=(run.token,)
        )
        timer.daemon = True
        run.timer = timer
        run.ttl_s = float(ttl_s)
        run.expires_at = time.time() + float(ttl_s)
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    @staticmethod
    def _annotate_candidates(
        candidates: list[Any],
        identity: IdentityGuard | None,
        *,
        amplitude_v: float,
        detector: str,
        resolution_samples: float,
    ) -> list[dict[str, Any]]:
        """Every candidate's dict, plus identity_ok/identity_reason.

        Uses `IdentityGuard.evaluate` (non-mutating): annotating every
        candidate on a frame must never let one the caller does not select
        become, or replace, the tracked identity.
        """
        out: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = candidate.to_dict()
            if identity is None:
                payload["identity_ok"] = True
                payload["identity_reason"] = None
            else:
                ok, reason = identity.evaluate(
                    candidate,
                    amplitude_v=amplitude_v,
                    detector=detector,
                    resolution_samples=resolution_samples,
                )
                payload["identity_ok"] = ok
                payload["identity_reason"] = reason
            out.append(payload)
        return out

    def _detect_strict_then_coarse(
        self, settings: AutoLockScanSettings, *, after: float | None
    ) -> tuple[list[Any], float, float, float, str, dict[str, Any], str | None]:
        """Strict detection, falling back to the staged coarse detector when
        strict finds nothing (fix #2). A staged run started on a wide scan --
        the exact case trajectory refinement exists for -- has no strict
        candidate at all; storing `[]`/`"strict"` from a bare strict attempt
        (the old behaviour) leaves the run with nothing to `step`/`lock` from
        and every subsequent call 422s. Only when BOTH detectors find nothing
        does this return an empty candidate list, with a non-None `detail`
        saying so; it never raises. Mirrors `_detect_narrow`'s per-stage
        fallback in `staged_autolock_step`, so `begin`'s first frame behaves
        exactly like a `step` stage would have detected it.

        Returns
        ``(candidates, center_v, amplitude_v, resolution, detector, frame, detail)``.
        """
        try:
            candidates, center_v, amplitude_v, resolution, frame = (
                self._capture_auto_lock_candidates_strict(settings, after=after)
            )
            return candidates, center_v, amplitude_v, resolution, "strict", frame, None
        except ValueError as strict_exc:
            try:
                candidates, center_v, amplitude_v, resolution, _metrics, frame = (
                    self._coarse_auto_lock_candidates(settings, after=after)
                )
                return (
                    candidates, center_v, amplitude_v, resolution, "coarse", frame, None
                )
            except ValueError as coarse_exc:
                error_trace, monitor_trace, frame_id, acquired_at = (
                    self._snapshot_auto_lock_traces_with_frame()
                )
                center_v, amplitude_v, _rising, mod_hz = self._snapshot_sweep_params()
                frame = _frame_summary(
                    error_trace, frame_id, acquired_at, center_v, amplitude_v, mod_hz, []
                )
                resolution = feature_resolution_samples(
                    settings, len(error_trace), amplitude_v
                )
                detail = (
                    f"No strict candidate ({strict_exc}); no coarse candidate "
                    f"either ({coarse_exc})."
                )
                return [], center_v, amplitude_v, resolution, "strict", frame, detail

    def staged_autolock_begin(
        self,
        settings_payload: dict[str, Any] | None,
        ttl_s: float,
    ) -> dict[str, Any]:
        """Start a staged (step-by-step) auto-lock run.

        Callers must have already triggered/waited for a fresh frame (the
        same restart-and-capture `acquire_scan` mechanism `auto_lock_scan`
        candidates uses with `acquire=true`) before calling this -- it
        detects on the CURRENT cached trace, exactly like
        `auto_lock_detect(acquire=false)` does. 409 if a run is already
        active or the device is locked.
        """
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        self._ensure_no_staged_autolock_run("staged auto-lock begin")
        ttl_s = min(max(float(ttl_s), 0.01), STAGED_AUTOLOCK_MAX_TTL_S)
        with self._state_lock:
            if settings_payload is None:
                settings = AutoLockScanSettings.from_mapping(self.auto_lock_scan_settings)
            else:
                settings = AutoLockScanSettings.from_mapping(settings_payload)
            acceptance = AcceptanceSettings.from_mapping(self.lock_acceptance_settings)
        # require_unlocked mirrors the one-shot path: refuse a locked device
        # up front rather than acquiring a frame it cannot use.
        self._snapshot_sweep_params(require_unlocked=True)
        candidates, center_v, amplitude_v, resolution, detector, frame, detail = (
            self._detect_strict_then_coarse(settings, after=None)
        )

        token = uuid.uuid4().hex
        now = time.time()
        run = StagedAutolockRun(
            token=token,
            settings=settings,
            acceptance=acceptance,
            restore_center_v=center_v,
            restore_amplitude_v=amplitude_v,
            trace_length=int(frame["n_points"]),
            created_at=now,
            expires_at=now + ttl_s,
            ttl_s=ttl_s,
            center_v=center_v,
            amplitude_v=amplitude_v,
            latest_frame=frame,
            latest_detector=detector,
            latest_resolution=resolution,
            latest_candidates=candidates,
        )
        with self._state_lock:
            if self._staged_autolock is not None:
                raise RuntimeError(
                    "A staged auto-lock run is already active (token "
                    f"{self._staged_autolock.token})."
                )
            self._staged_autolock = run
            self._staged_autolock_arm_timer(run, ttl_s)
        return {
            "token": token,
            "expires_at": run.expires_at,
            "geometry": {"center_v": center_v, "amplitude_v": amplitude_v},
            "frame": frame,
            "candidates": [c.to_dict() for c in candidates],
            "detector": detector,
            "detail": detail,
            "stage_index": 0,
        }

    def staged_autolock_renew(self, token: str, ttl_s: float) -> dict[str, Any]:
        ttl_s = min(max(float(ttl_s), 0.01), STAGED_AUTOLOCK_MAX_TTL_S)
        with self._state_lock:
            run = self._get_staged_run_or_raise(token)
            self._staged_autolock_arm_timer(run, ttl_s)
            expires_at = run.expires_at
        return {"expires_at": expires_at}

    def staged_autolock_state(self) -> dict[str, Any]:
        with self._state_lock:
            run = self._staged_autolock
            if run is None:
                return {"active": False}
            return {
                "active": True,
                "token": run.token,
                "stage_index": run.stage_index,
                "geometry": {"center_v": run.center_v, "amplitude_v": run.amplitude_v},
                "expires_at": run.expires_at,
                "frame": run.latest_frame,
                "busy": run.busy,
            }

    def staged_autolock_abort(self, token: str) -> dict[str, Any]:
        with self._state_lock:
            run = self._get_idle_staged_run_or_raise(token)
            self._staged_autolock = None
        if run.timer is not None:
            run.timer.cancel()
        restored = self._restore_sweep_geometry(
            run.restore_center_v, run.restore_amplitude_v
        )
        return {"restored": restored}

    def staged_autolock_observe_frame(
        self, frame: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> None:
        """Update the run's "latest seen frame" from an out-of-band read-only
        detection (an `auto_lock_candidates?acquire=true` call made on this
        device while a staged run is active) -- per the spec, a read-only call
        made during a run must still advance what the run considers its
        latest frame, so `step`/`lock` can reference it by `frame_id` without
        the caller having to drive another `step`.

        ``candidates`` are plain dicts in exactly `AutoLockScanResult.to_dict()`
        shape (i.e. `auto_lock_detect`'s own `result["candidates"]`), so they
        round-trip losslessly back into `AutoLockScanResult` instances -- the
        same objects `step`/`lock` need for target_index lookup and
        IdentityGuard checks.

        No-op if no run is active, the frame is not newer than the one the
        run already has, or this device's current geometry does not match the
        run's (the read-only call ran at different geometry -- not this
        run's frame). Never touches identity.
        """
        try:
            frame_id = int(frame.get("frame_id"))
        except (TypeError, ValueError):
            return
        with self._state_lock:
            run = self._staged_autolock
            if run is None:
                return
            if run.busy:
                # An in-flight step/lock owns the geometry right now; do not
                # adopt a frame concurrently with it (see StagedAutolockRun.busy).
                return
            if frame_id <= int(run.latest_frame.get("frame_id", -1)):
                return
            center_v = frame.get("sweep_center_v")
            amplitude_v = frame.get("sweep_amplitude_v")
            if center_v is None or amplitude_v is None:
                return
            if (
                abs(float(center_v) - run.center_v) > 1e-9
                or abs(float(amplitude_v) - run.amplitude_v) > 1e-9
            ):
                # Geometry moved out from under the run (someone else's
                # acquire_scan, or a stale call); do not adopt a frame that is
                # not at this run's current geometry.
                return
            try:
                rebuilt = [AutoLockScanResult(**item) for item in candidates]
            except (TypeError, ValueError):
                # Malformed payload -- keep the run's existing frame rather
                # than risk poisoning it with a partially-applied update.
                return
            run.latest_candidates = rebuilt
            run.latest_frame = dict(frame)

    def _recover_staged_run_after_failed_stage(
        self, token: str, exc: Exception
    ) -> None:
        """`step`'s failure path once a stage's geometry write has already
        landed but the detection that follows it then failed (fix #4): a
        `ValueError` from the coarse detector (`find_plausible_coarse_candidates`,
        reached when strict also rejects the new trace) or a `RuntimeError`
        ("No fresh sweep arrived after changing scan geometry.").

        Before this fix, a failure here left `run.center_v`/`amplitude_v`/
        `latest_frame` pointing at the OLD geometry while the device sat at
        the NEW one, so `lock`'s geometry-unchanged check failed forever
        after and `observe_frame` rejected every later frame as being at the
        "wrong" geometry. This re-reads the device's ACTUAL geometry,
        advances `stage_index`, clears `latest_candidates`/`pending_shift`,
        and records whatever frame is current if one is available -- the run
        stays ACTIVE so the caller can retry `step` from here, or `abort`.

        Always raises `StagedAutolockError(422)`; never returns normally.
        """
        with self._state_lock:
            run = self._staged_autolock
            if run is None or run.token != token:
                raise StagedAutolockError(
                    f"No candidate at the new geometry -- abort or retry ({exc})",
                    status_code=422,
                ) from exc
            try:
                center_v, amplitude_v, _rising, mod_hz = self._snapshot_sweep_params()
            except Exception:  # noqa: BLE001 - keep the run's last-known geometry
                center_v, amplitude_v, mod_hz = run.center_v, run.amplitude_v, None
            run.stage_index += 1
            run.center_v = float(center_v)
            run.amplitude_v = float(amplitude_v)
            run.latest_candidates = []
            run.pending_shift = None
            try:
                error_trace, _monitor_trace, frame_id, acquired_at = (
                    self._snapshot_auto_lock_traces_with_frame()
                )
                run.latest_frame = _frame_summary(
                    error_trace, frame_id, acquired_at, center_v, amplitude_v,
                    mod_hz, [],
                )
            except Exception:  # noqa: BLE001 - keep the previous latest_frame
                pass
        raise StagedAutolockError(
            f"No candidate at the new geometry -- abort or retry ({exc})",
            status_code=422,
        ) from exc

    def staged_autolock_step(
        self, token: str, selected_frame_id: int, selected_target_index: int
    ) -> dict[str, Any]:
        with self._state_lock:
            run = self._get_idle_staged_run_or_raise(token)
            if int(selected_frame_id) != int(run.latest_frame.get("frame_id", -1)):
                raise StagedAutolockError(
                    f"selected.frame_id {selected_frame_id} is stale; this run "
                    f"has since seen frame {run.latest_frame.get('frame_id')}.",
                    status_code=409,
                )
            selected = next(
                (
                    c for c in run.latest_candidates
                    if int(c.target_index) == int(selected_target_index)
                ),
                None,
            )
            if selected is None:
                raise StagedAutolockError(
                    f"target_index {selected_target_index} is not one of the "
                    f"candidates on frame {selected_frame_id}.",
                    status_code=422,
                )
            # The caller's selection on THIS frame is now known -- finish any
            # width-shift measurement the stage that produced this frame
            # deferred (see fix #3 / _consume_pending_shift_measurement),
            # before it is used below to plan the next stage.
            self._consume_pending_shift_measurement(run, selected)
            if run.identity is None:
                run.identity = IdentityGuard(
                    selected, run.latest_resolution,
                    trace_length=run.trace_length, detector=run.latest_detector,
                )
            else:
                try:
                    run.identity.check(
                        selected,
                        amplitude_v=run.amplitude_v,
                        detector=run.latest_detector,
                        resolution_samples=run.latest_resolution,
                    )
                except _TrackingIdentityChanged as exc:
                    raise StagedAutolockError(str(exc), status_code=422) from exc
            settings = run.settings
            geometry_settle_s = max(0.0, float(run.acceptance.settle_ms) / 1000.0)
            center_v, amplitude_v = run.center_v, run.amplitude_v
            trace_length = run.trace_length
            detector = run.latest_detector
            shift_per_fraction = run.shift_per_fraction
            narrow_count = run.narrow_count
            if narrow_count >= _MAX_REFINEMENT_STAGES:
                raise StagedAutolockError(
                    f"No lockable scan after {_MAX_REFINEMENT_STAGES} trajectory "
                    "refinement stages.",
                    status_code=422,
                )
            # From here the operation does slow I/O with `_state_lock`
            # released (geometry write, fresh-frame wait). `busy` keeps a
            # concurrent step/lock/abort on this token from racing it or
            # tearing the run down mid-flight -- see StagedAutolockRun.busy.
            run.busy = True
            self._staged_autolock_arm_timer(run, run.ttl_s)

        try:
            latest_frame_box: dict[str, Any] = {}
            latest_candidates_box: list[Any] = []
            # Set the instant a geometry write lands, inside either callback --
            # distinguishes "the center-move lock was busy" (raised before
            # this stage ever wrote anything -- 409, run untouched) from "the
            # write succeeded but detection then failed" (fix #4 -- 422, run
            # recovered to the device's ACTUAL geometry).
            geometry_written = {"done": False}

            def _detect_narrow(c, a, settle_s):
                moved_at = self._set_sweep_geometry(c, a, settle_s=settle_s)
                geometry_written["done"] = True
                try:
                    candidates, cc, aa, r, frame = self._capture_auto_lock_candidates_strict(
                        settings, after=moved_at
                    )
                    latest_frame_box.clear()
                    latest_frame_box.update(frame)
                    latest_candidates_box[:] = candidates
                    return candidates[0], cc, aa, r, "strict", None
                except ValueError:
                    candidates, cc, aa, r, m, frame = self._coarse_auto_lock_candidates(
                        settings, after=moved_at
                    )
                    latest_frame_box.clear()
                    latest_frame_box.update(frame)
                    latest_candidates_box[:] = candidates
                    return candidates[0], cc, aa, r, "coarse", m

            def _detect_recenter(c, a, settle_s):
                moved_at = self._set_sweep_geometry(c, a, settle_s=settle_s)
                geometry_written["done"] = True
                candidates, cc, aa, r, m, frame = self._coarse_auto_lock_candidates(
                    settings, after=moved_at
                )
                latest_frame_box.clear()
                latest_frame_box.update(frame)
                latest_candidates_box[:] = candidates
                return candidates[0], cc, aa, r, m

            # The geometry-writing part of a stage must not interleave with a
            # one-shot refinement walk driving the same actuator (fix #3) --
            # `auto_lock_from_scan` already refuses outright while a staged
            # run exists, so the only way this lock is held here is a walk
            # that was already in flight when this run began.
            try:
                with self._exclusive_center_move("staged auto-lock step"):
                    outcome = self._run_refinement_stage(
                        settings,
                        center_v=center_v,
                        amplitude_v=amplitude_v,
                        target=selected,
                        detector=detector,
                        trace_length=trace_length,
                        shift_per_fraction=shift_per_fraction,
                        geometry_settle_s=geometry_settle_s,
                        detect_narrow=_detect_narrow,
                        detect_recenter=_detect_recenter,
                    )
            except (ValueError, RuntimeError) as exc:
                if geometry_written["done"]:
                    # Always raises StagedAutolockError(422); the run's
                    # bookkeeping is brought back in line with the device's
                    # actual geometry first (fix #4).
                    self._recover_staged_run_after_failed_stage(token, exc)
                raise StagedAutolockError(str(exc), status_code=409) from exc
            step = outcome.step

            with self._state_lock:
                run = self._get_staged_run_or_raise(token)
                if step.action == "refuse":
                    # The run stays active so the caller can inspect state or abort.
                    raise StagedAutolockError(step.reason, status_code=422)
                if step.action == "done":
                    # Nothing moved: re-report the frame the caller already has,
                    # now annotated against the identity this call just
                    # established/advanced.
                    annotated = self._annotate_candidates(
                        run.latest_candidates, run.identity,
                        amplitude_v=run.amplitude_v, detector=run.latest_detector,
                        resolution_samples=run.latest_resolution,
                    )
                    any_lockable = self._augment_candidates_with_lockable_here(
                        annotated, run.latest_candidates, settings=settings,
                        detector=run.latest_detector, amplitude_v=run.amplitude_v,
                        trace_length=trace_length,
                    )
                    return {
                        "stage_index": run.stage_index,
                        "geometry": {"center_v": run.center_v, "amplitude_v": run.amplitude_v},
                        "frame": run.latest_frame,
                        "candidates": annotated,
                        "expires_at": run.expires_at,
                        "needs_more_refinement": not any_lockable,
                        "planner": {"action": step.action, "reason": step.reason, "bounds": step.bounds},
                    }
                # "narrow" / "recenter" / "rail_escape": geometry moved and a
                # fresh frame was detected -- adopt it as the run's new latest
                # frame. Assert the run is still the one this call started
                # with: `busy` guarantees it, but a geometry write is
                # dangerous enough to check rather than trust silently.
                if run.token != token:
                    raise StagedAutolockError(
                        "Staged auto-lock run changed identity mid-step; "
                        "refusing to adopt this stage's result.",
                        status_code=409,
                    )
                run.stage_index += 1
                run.narrow_count += 1
                run.center_v = outcome.center_v
                run.amplitude_v = outcome.amplitude_v
                run.latest_detector = outcome.detector
                run.latest_resolution = outcome.resolution
                run.latest_candidates = latest_candidates_box
                run.latest_frame = dict(latest_frame_box)
                # Width-induced shift bookkeeping is DEFERRED until the
                # caller selects a candidate on this new frame (see fix #3):
                # `outcome.target` is only the best-score candidate, which
                # may be a different serrodyne order than the one about to be
                # selected, and measuring the shift against the wrong order
                # would poison shift_per_fraction for every later stage.
                run.pending_shift = {
                    "before_v": outcome.before_v,
                    "before_amplitude": outcome.before_amplitude,
                    "before_center": outcome.before_center,
                    "before_detector": outcome.before_detector,
                    "detector": run.latest_detector,
                    "amplitude_v": run.amplitude_v,
                    "center_v": run.center_v,
                }
                annotated = self._annotate_candidates(
                    run.latest_candidates, run.identity,
                    amplitude_v=run.amplitude_v, detector=run.latest_detector,
                    resolution_samples=run.latest_resolution,
                )
                # `needs_more_refinement` is now a per-frame compatibility key
                # derived from the per-candidate `lockable_here` flags (fix
                # #3): "no candidate on this frame is lockable_here". The
                # orchestrator should prefer the selected candidate's own
                # `lockable_here` over this frame-level key.
                any_lockable = self._augment_candidates_with_lockable_here(
                    annotated, run.latest_candidates, settings=settings,
                    detector=run.latest_detector, amplitude_v=run.amplitude_v,
                    trace_length=trace_length,
                )
                return {
                    "stage_index": run.stage_index,
                    "geometry": {"center_v": run.center_v, "amplitude_v": run.amplitude_v},
                    "frame": run.latest_frame,
                    "candidates": annotated,
                    "expires_at": run.expires_at,
                    "needs_more_refinement": not any_lockable,
                    "planner": {"action": step.action, "reason": step.reason, "bounds": step.bounds},
                }
        finally:
            self._staged_autolock_finish_busy(token)

    def staged_autolock_lock(
        self, token: str, selected_frame_id: int, selected_target_index: int
    ) -> dict[str, Any]:
        """Verify the selected candidate on a fresh frame at the SAME (final)
        geometry, then hand off to the existing lock-engagement code path
        (`_move_and_lock`), exactly like a one-shot auto-lock's final
        verification + handover. Never substitutes another candidate: if the
        strict re-detection does not find a consistent, unambiguous match for
        the selection, this raises `StagedAutolockError` (422) and does not
        lock; the run stays active so the caller can retry `step`/`lock` or
        `abort`.
        """
        with self._state_lock:
            run = self._get_idle_staged_run_or_raise(token)
            if int(selected_frame_id) != int(run.latest_frame.get("frame_id", -1)):
                raise StagedAutolockError(
                    f"selected.frame_id {selected_frame_id} is stale; this run "
                    f"has since seen frame {run.latest_frame.get('frame_id')}.",
                    status_code=409,
                )
            selected = next(
                (
                    c for c in run.latest_candidates
                    if int(c.target_index) == int(selected_target_index)
                ),
                None,
            )
            if selected is None:
                raise StagedAutolockError(
                    f"target_index {selected_target_index} is not one of the "
                    f"candidates on frame {selected_frame_id}.",
                    status_code=422,
                )
            # As in `step`: the caller's selection on this frame is now known,
            # so finish any deferred width-shift measurement from the stage
            # that produced it (see fix #3). Harmless if `lock` is called
            # straight after a geometry-changing `step` with no intervening
            # `step` call -- the measurement is not used again once locked,
            # but recording it keeps the run's bookkeeping consistent for a
            # caller that inspects it after the fact.
            self._consume_pending_shift_measurement(run, selected)
            if run.identity is None:
                run.identity = IdentityGuard(
                    selected, run.latest_resolution,
                    trace_length=run.trace_length, detector=run.latest_detector,
                )
            else:
                try:
                    run.identity.check(
                        selected,
                        amplitude_v=run.amplitude_v,
                        detector=run.latest_detector,
                        resolution_samples=run.latest_resolution,
                    )
                except _TrackingIdentityChanged as exc:
                    raise StagedAutolockError(str(exc), status_code=422) from exc
            settings = run.settings
            center_v, amplitude_v = run.center_v, run.amplitude_v
            trace_length = run.trace_length
            restore_center_v = run.restore_center_v
            restore_amplitude_v = run.restore_amplitude_v
            identity = run.identity
            # From here the operation does slow I/O with `_state_lock`
            # released (a fresh-frame wait, then `_move_and_lock`) -- see
            # StagedAutolockRun.busy.
            run.busy = True
            self._staged_autolock_arm_timer(run, run.ttl_s)

        try:
            # Strict verification at the SAME geometry -- position comparison
            # is only ever valid here because nothing has moved since
            # `selected` was detected.
            try:
                candidates, verify_center, verify_amplitude, _resolution, _frame = (
                    self._capture_auto_lock_candidates_strict(settings, after=time.time())
                )
            except ValueError as exc:
                raise StagedAutolockError(
                    f"No strict candidate found at lock-verification time: {exc}",
                    status_code=422,
                ) from exc
            if (
                abs(verify_center - center_v) > 1e-6
                or abs(verify_amplitude - amplitude_v) > 1e-6
            ):
                raise StagedAutolockError(
                    "Sweep geometry moved before lock verification could run: "
                    f"expected center {center_v:.6f} V amplitude {amplitude_v:.6f} V, "
                    f"found {verify_center:.6f} V / {verify_amplitude:.6f} V.",
                    status_code=422,
                )
            tolerance_samples = STAGED_AUTOLOCK_LOCK_TOLERANCE_SAMPLES
            tolerance_v = tolerance_samples * 2.0 * abs(amplitude_v) / max(1, trace_length - 1)
            matches = [
                c for c in candidates
                if c.target_slope_rising == selected.target_slope_rising
                and abs(c.target_voltage - selected.target_voltage) <= tolerance_v
                and identity.evaluate(
                    c, amplitude_v=amplitude_v, detector="strict", resolution_samples=0.0,
                )[0]
            ]
            if not matches:
                raise StagedAutolockError(
                    "No fresh strict candidate is consistent with the selected "
                    f"target at {selected.target_voltage:.6f} V within "
                    f"{tolerance_v * 1e3:.3f} mV ({tolerance_samples:g} samples); "
                    "refusing to lock rather than substitute a different candidate.",
                    status_code=422,
                )
            if len(matches) > 1:
                raise StagedAutolockError(
                    f"{len(matches)} fresh strict candidates are within "
                    f"{tolerance_v * 1e3:.3f} mV of the selected target at "
                    f"{selected.target_voltage:.6f} V -- ambiguous; refusing to "
                    "lock rather than guess.",
                    status_code=422,
                )
            final = matches[0]
            try:
                identity.check(
                    final, amplitude_v=amplitude_v, detector="strict", resolution_samples=0.0,
                )
            except _TrackingIdentityChanged as exc:
                raise StagedAutolockError(str(exc), status_code=422) from exc

            refinement = {
                "attempted": True,
                "trigger": "staged",
                "original_center_v": restore_center_v,
                "original_amplitude_v": restore_amplitude_v,
                "final_center_v": center_v,
                "final_amplitude_v": amplitude_v,
                "stages": [],
                "restored": False,
            }
            # After the fresh-frame I/O above, re-check the run is still the
            # active one before actually engaging the lock. `busy` guarantees
            # no `abort`/second `lock` could have ended it in the meantime,
            # and the TTL timer defers rather than restores while busy (see
            # `_staged_autolock_expire`) -- so this can only ever pass -- but
            # a lock handoff is dangerous enough to assert rather than trust.
            with self._state_lock:
                active_check = self._staged_autolock
                if (
                    active_check is None
                    or active_check.token != token
                    or not active_check.busy
                ):
                    raise StagedAutolockError(
                        "Staged auto-lock run is no longer active; refusing "
                        "to lock.",
                        status_code=409,
                    )
            self._move_and_lock(final, center_v)
            timer_to_cancel: threading.Timer | None = None
            with self._state_lock:
                active_run = self._staged_autolock
                if active_run is not None and active_run.token == token:
                    active_run.locked = True
                    timer_to_cancel = active_run.timer
                    self._staged_autolock = None
                    self._deferred_sweep_geometry = (
                        float(restore_center_v), float(restore_amplitude_v),
                    )
            if timer_to_cancel is not None:
                timer_to_cancel.cancel()
            if final.discriminator_slope_v_per_mhz is not None:
                with self._state_lock:
                    self._discriminator_slope_v_per_mhz = float(
                        final.discriminator_slope_v_per_mhz
                    )
            payload = final.to_dict()
            payload["refinement"] = refinement
            payload["detail"] = "Staged auto-lock started."
            return payload
        finally:
            # No-op if `lock` already succeeded and cleared the run above.
            self._staged_autolock_finish_busy(token)

    def auto_lock_from_scan(
        self, settings_payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        self._ensure_no_staged_autolock_run("one-shot auto-lock from scan")

        with self._state_lock:
            if settings_payload is None:
                settings = AutoLockScanSettings.from_mapping(
                    self.auto_lock_scan_settings
                )
            else:
                settings = AutoLockScanSettings.from_mapping(settings_payload)
                self.auto_lock_scan_settings = settings.__dict__.copy()
        with self._state_lock:
            acceptance = AcceptanceSettings.from_mapping(self.lock_acceptance_settings)

        refinement: dict[str, Any] | None = None
        # A walk that aborted has already put the operator's geometry back, so
        # there is nothing left to defer.
        refinement_failed = False
        with self._exclusive_center_move("auto-lock from scan"):
            # Snapshot the restore-to geometry inside the lock: _move_and_lock
            # re-reads the center for the same reason (see its docstring) --
            # an unserialized set_param between an earlier snapshot and here
            # could otherwise make a later restore overwrite the operator's
            # own change with a stale value.
            sweep_center, sweep_amplitude, _preferred_slope_rising, _modulation_frequency_hz = (
                self._snapshot_sweep_params(require_unlocked=True)
            )
            # A strict detection is authoritative: if the calibrated detector
            # accepts this trace, lock on it. Refinement used to run whenever
            # the sample count was below a hardcoded 10, which meant it could
            # only ever discard a target the detector had just accepted -- and
            # on a feature that moves with scan geometry, its own verification
            # then refused the lock outright.
            error_trace, monitor_trace = self._snapshot_auto_lock_traces()
            try:
                direct, direct_center, direct_amplitude, direct_resolution = (
                    self._capture_auto_lock_target(
                        settings, traces=(error_trace, monitor_trace)
                    )
                )
            except ValueError as strict_error:
                # The detector REJECTED this trace -- the case refinement was
                # written for. Seed the walk with a coarse candidate and narrow
                # until strict agrees. A RuntimeError (no trace, already locked)
                # is not a detection problem and still propagates.
                coarse, coarse_center, coarse_amplitude, coarse_resolution, _metrics = (
                    self._coarse_auto_lock_target(settings)
                )
                try:
                    result, refinement = self._trajectory_refine_auto_lock(
                        settings,
                        acceptance,
                        sweep_center,
                        sweep_amplitude,
                        initial_target=coarse,
                        initial_center_v=coarse_center,
                        initial_amplitude_v=coarse_amplitude,
                        initial_resolution=coarse_resolution,
                        initial_detector="coarse",
                        trace_length=len(error_trace),
                    )
                except TrajectoryRefinementAborted as refine_error:
                    # Nothing to fall back to: the strict detector never
                    # accepted anything. Report the original rejection, which is
                    # what the operator has to act on, with the walk's history.
                    refine_error.refinement["strict_rejection"] = str(strict_error)
                    raise
            else:
                result = direct
                # The detector is happy, but on a scan this wide the centre move
                # that follows is one long hysteretic jump and lands on the
                # wrong feature. Narrow around the target first so the centre
                # walks there in bounded steps instead.
                if scan_too_wide_to_lock(
                    settings, direct_amplitude, direct.sideband_offset_v,
                    trace_points=len(error_trace),
                ):
                    try:
                        result, refinement = self._trajectory_refine_auto_lock(
                            settings,
                            acceptance,
                            sweep_center,
                            sweep_amplitude,
                            initial_target=direct,
                            initial_center_v=direct_center,
                            initial_amplitude_v=direct_amplitude,
                            initial_resolution=direct_resolution,
                            initial_detector="strict",
                            trace_length=len(error_trace),
                        )
                    except TrajectoryRefinementAborted as refine_error:
                        # Unlike the rejection path there IS something to fall
                        # back to. Narrowing is an improvement on a detection
                        # that already passed, so failing to narrow must not
                        # cost the lock -- except when the walk lost the feature
                        # itself, which is the one failure that means the target
                        # can no longer be trusted.
                        if refine_error.failure_kind == "identity":
                            raise
                        logger.warning(
                            "Auto-lock narrowing failed (%s); locking on the "
                            "direct detection at the original scan instead.",
                            refine_error,
                        )
                        refinement = dict(refine_error.refinement)
                        refinement["fell_back_to_direct"] = True
                        result = direct
                        refinement_failed = True
            try:
                # The refinement final verification leaves geometry untouched,
                # so the handover starts from the exact verified scan.
                self._move_and_lock(result, sweep_center)
            except Exception as exc:
                if refinement is not None:
                    self._restore_sweep_geometry(sweep_center, sweep_amplitude)
                    # The refinement stage history that got us here is otherwise
                    # lost, and it is the most informative diagnostic for this
                    # failure.
                    if not hasattr(exc, "refinement"):
                        exc.refinement = refinement
                raise
            else:
                if refinement is not None and not refinement_failed:
                    # Do NOT restore now: while locked, sweep_center is the
                    # lock's operating point (_move_and_lock set it to the
                    # target just before start_lock), so writing the old center
                    # would pull the laser off the feature. Put the operator's
                    # geometry back when the sweep next starts instead, so the
                    # free-running scan is not left narrowed.
                    with self._rpyc_lock:
                        self._deferred_sweep_geometry = (
                            float(sweep_center),
                            float(sweep_amplitude),
                        )
        # Cache the discriminator slope measured on this scan so status()/plot
        # frames can report the in-loop lock error in MHz. Only overwrite when the
        # scan resolved one (PDH + known modulation frequency); keep the previous
        # value otherwise rather than blanking a good calibration.
        if result.discriminator_slope_v_per_mhz is not None:
            with self._state_lock:
                self._discriminator_slope_v_per_mhz = float(
                    result.discriminator_slope_v_per_mhz
                )

        payload = result.to_dict()
        if refinement is not None:
            payload["refinement"] = refinement
        payload["detail"] = "Auto-lock started from scan."
        return payload

    def build_manual_lock_row(
        self,
        *,
        device_name: str | None = None,
        device_key: str,
        lock_source: str = "manual_lock",
        success: bool = True,
    ) -> dict[str, Any]:
        param_names = (
            "modulation_frequency",
            "modulation_amplitude",
            "demodulation_phase_a",
            "demodulation_phase_b",
            "offset_a",
            "offset_b",
            "control_channel",
            "p",
            "i",
            "d",
            "sweep_center",
            "sweep_amplitude",
        )
        params = self._collect_manual_lock_params(param_names)
        trace_values, monitor_trace_values = self._extract_manual_lock_traces()

        return build_manual_lock_row(
            device_name=device_name,
            device_key=device_key,
            lock_source=lock_source,
            success=success,
            params=params,
            trace_y=trace_values,
            monitor_trace_y=monitor_trace_values,
        )

    def _collect_manual_lock_params(self, names: tuple[str, ...]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.parameters is not None:
            missing_names: list[str] = []
            with self._rpyc_lock:
                for name in names:
                    try:
                        params[name] = getattr(self.parameters, name).value
                    except Exception:
                        missing_names.append(name)
            if missing_names:
                params.update(self._get_cached_param_values(tuple(missing_names)))
            return params
        return self._get_cached_param_values(names)

    def _extract_manual_lock_traces(
        self,
    ) -> tuple[list[float] | None, list[float] | None]:
        trace_values: list[float] | None = None
        monitor_trace_values: list[float] | None = None
        plot_data, last_plot_frame = self._snapshot_manual_lock_sources()
        if plot_data is not None and len(plot_data) >= 3:
            try:
                if plot_data[2] is not None:
                    combined_error = np.asarray(plot_data[2], dtype=float)
                    trace_values = (combined_error / ADC_SCALE).tolist()
                if plot_data[1] is not None:
                    monitor_trace = np.asarray(plot_data[1], dtype=float)
                    monitor_trace_values = (monitor_trace / ADC_SCALE).tolist()
            except Exception:
                logger.debug(
                    "Failed extracting lock traces from raw plot_data device=%s",
                    self.device.key,
                    exc_info=True,
                )
                trace_values = None
                monitor_trace_values = None
            return trace_values, monitor_trace_values

        if last_plot_frame is None:
            return None, None

        series = last_plot_frame.get("series", {})
        combined_series = series.get("combined_error")
        monitor_series = series.get("monitor_signal")
        if monitor_series is None:
            monitor_series = series.get("error_signal_2")

        def _series_to_list(value: Any) -> list[float] | None:
            # Plot frames now hold numpy arrays in `series`; legacy
            # list-with-Nones (from pre-#6 caches or tests) also supported.
            if isinstance(value, np.ndarray):
                arr = value.astype(float, copy=False)
                return [float(v) for v in arr]
            if isinstance(value, list):
                return [
                    float(v) if v is not None else float("nan") for v in value
                ]
            return None

        trace_values = _series_to_list(combined_series)
        monitor_trace_values = _series_to_list(monitor_series)
        return trace_values, monitor_trace_values

    def start_sweep(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_sweep()
            self._apply_deferred_sweep_geometry()

    def _apply_deferred_sweep_geometry(self) -> None:
        """Restore the pre-refinement scan once the lock is off. Caller holds
        _rpyc_lock and has just started the sweep, so the write can no longer
        move a lock's operating point."""
        pending = self._deferred_sweep_geometry
        if pending is None:
            return
        self._deferred_sweep_geometry = None
        self._restore_sweep_geometry(*pending)

    def set_csr_direct(self, key: str, value: int) -> None:
        """Directly write a single FPGA CSR (bypasses the write_registers diff cache).

        Used to force a ``logic_sweep_run`` 0->1 edge so a device restarts its
        sweep ramp from center. No-op on the simulator service.
        """
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_set_csr_direct(key, value)

    def _default_trace_timeout(self) -> float:
        """Timeout for capturing one complete sweep trace.

        On hardware each delivered frame spans one full sweep, so at slow
        sweep speeds a fresh frame only arrives once per sweep period
        (period ~= 2**sweep_speed / 3800 s). Scale the wait accordingly,
        with a floor for fast sweeps and a ceiling so a hung device can't
        block the request indefinitely.
        """
        try:
            speed = int(self._read_param_fast("sweep_speed", 8) or 8)
        except (TypeError, ValueError):
            speed = 8
        period = (2 ** max(0, min(15, speed))) / 3800.0
        return max(3.0, min(30.0, 2.5 * period + 1.0))

    def wait_for_fresh_trace(
        self, timeout_s: float | None = None, skip_frames: int = 1
    ) -> Dict[str, Any]:
        """Wait for the next complete sweep frame and return that trace.

        Call this right after (re)starting the sweep. ``skip_frames`` fresh
        frames are skipped first so a partial frame captured across the restart
        isn't returned -- the next complete sweep is returned instead. This does
        not change device state.
        """
        if self.parameters is None or self.control is None:
            raise RuntimeError("Device not connected")
        if timeout_s is None:
            timeout_s = self._default_trace_timeout()
        needed = max(1, skip_frames + 1)
        with self._state_lock:
            last_ts = self.last_plot_timestamp
        seen = 0
        deadline = time.time() + max(0.5, timeout_s)
        while time.time() < deadline:
            with self._state_lock:
                ts = self.last_plot_timestamp
            if ts is not None and ts != last_ts:
                last_ts = ts
                seen += 1
                if seen >= needed:
                    return self._build_trace_snapshot(ts)
            time.sleep(0.02)
        raise RuntimeError("Timed out waiting for a sweep trace")

    def _build_trace_snapshot(self, captured_at: float | None) -> Dict[str, Any]:
        """Build a JSON-able single-scan trace from the cached unlocked frame."""
        with self._state_lock:
            plot_data = self.plot_state.last_plot_data
            if plot_data is None or len(plot_data) < 3 or plot_data[2] is None:
                raise RuntimeError("No unlocked trace available")
            error_signal_1 = (
                np.array(plot_data[0], copy=True) if plot_data[0] is not None else None
            )
            monitor_or_error_2 = (
                np.array(plot_data[1], copy=True) if plot_data[1] is not None else None
            )
            combined_error = np.array(plot_data[2], copy=True)
            frame_id = int(self.plot_state.last_unlocked_frame_id)
            acquired_at = self.plot_state.last_unlocked_trace_at

        dual_channel = bool(self._read_param_fast("dual_channel", False))
        center = float(self._read_param_fast("sweep_center", 0.0) or 0.0)
        amplitude = float(self._read_param_fast("sweep_amplitude", 1.0) or 1.0)
        n_points = int(combined_error.shape[0])

        def to_volts(arr: "np.ndarray | None") -> "list[float] | None":
            if arr is None:
                return None
            # Match the volt scaling the UI applies to plot series.
            return (np.asarray(arr, dtype=float) / V).tolist()

        # Sweep voltage axis (same linear span the unlocked plot uses).
        if n_points > 1:
            x_axis = np.linspace(center - amplitude, center + amplitude, n_points).tolist()
        else:
            x_axis = [center]

        return {
            "timestamp": captured_at,
            "lock": False,
            "dual_channel": dual_channel,
            "sweep_center": center,
            "sweep_amplitude": amplitude,
            "n_points": n_points,
            "x": x_axis,
            "x_unit": "V",
            "combined_error": to_volts(combined_error),
            "error_signal_1": to_volts(error_signal_1),
            "error_signal_2": to_volts(monitor_or_error_2) if dual_channel else None,
            "monitor_signal": to_volts(monitor_or_error_2) if not dual_channel else None,
            # Additive: identity of the specific unlocked trace this snapshot
            # was built from, so a caller (e.g. auto_lock_candidates?acquire=true)
            # can confirm it analysed this frame and not an older cached one.
            "frame_id": frame_id,
            "acquired_at": acquired_at,
        }

    def start_autolock(self, x0: int, x1: int) -> None:
        if AUTOMATION_TEMP_DISABLED:
            raise RuntimeError(f"Autolock is {AUTOMATION_TEMP_DISABLED_REASON}")
        if self.control is None:
            raise RuntimeError("Device not connected")
        self._ensure_no_staged_autolock_run("start_autolock")
        with self._state_lock:
            if self.plot_state.last_plot_data is None:
                raise RuntimeError("No plot data available")
            combined_error = self.plot_state.last_plot_data[2]
            additional = list(self.plot_state.combined_error_cache)
        with self._rpyc_lock:
            self.control.exposed_start_autolock(
                x0,
                x1,
                pickle.dumps(combined_error),
                additional_spectra=pickle.dumps(additional),
            )
        try:
            (
                mean_signal,
                target_slope_rising,
                target_zoom,
                rolled_error_signal,
                line_width,
                peak_idxs,
            ) = get_lock_point(
                combined_error,
                *sorted([x0, x1]),
            )
            with self._state_lock:
                self.plot_state.autolock_ref_spectrum = rolled_error_signal
        except Exception:  # noqa: BLE001 - optional helper for lock target overlay
            logger.debug(
                "Failed computing autolock reference spectrum device=%s",
                self.device.key,
                exc_info=True,
            )
            with self._state_lock:
                self.plot_state.autolock_ref_spectrum = None

    def start_optimization(self, x0: int, x1: int) -> None:
        if AUTOMATION_TEMP_DISABLED:
            raise RuntimeError(f"Optimization is {AUTOMATION_TEMP_DISABLED_REASON}")
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        x0, x1 = sorted([int(x0), int(x1)])
        with self._rpyc_lock:
            dual_channel = bool(self.parameters.dual_channel.value)
            channel = int(self.parameters.optimization_channel.value)
        with self._state_lock:
            if self.plot_state.last_plot_data is None:
                raise RuntimeError("No plot data available")
            if not dual_channel:
                spectrum = self.plot_state.last_plot_data[0]
            else:
                spectrum = self.plot_state.last_plot_data[0 if channel == 0 else 1]
        cropped = np.array(spectrum[x0:x1], dtype=float)
        cropped = cropped[np.isfinite(cropped)]
        if cropped.size < 2:
            raise RuntimeError("Selected range is too small")
        if int(np.argmin(cropped)) == int(np.argmax(cropped)):
            raise RuntimeError("Selected range does not contain a slope")
        with self._rpyc_lock:
            self.control.exposed_start_optimization(x0, x1, pickle.dumps(spectrum))

    def start_pid_optimization(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_pid_optimization()

    def start_psd_acquisition(
        self,
        algorithm: int | None = None,
        max_decimation: int | None = None,
    ) -> None:
        """Trigger a server-side broadband PSD acquisition of the error signal.

        The device must already be locked. The acquisition runs as a background
        task on the device (sweeping decimations), so this returns immediately;
        results stream back via the psd_data_* callbacks. Safe across the
        numpy-pickle boundary because only scalar ints are sent and the result
        flows server->gateway (NOT gated by AUTOMATION_TEMP_DISABLED).
        """
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            if not bool(self.parameters.lock.value):
                raise RuntimeError("Laser must be locked before a PSD measurement.")
            if algorithm is not None:
                self.parameters.psd_algorithm.value = int(algorithm)
            if max_decimation is not None:
                self.parameters.psd_acquisition_max_decimation.value = int(
                    max_decimation
                )
            self.control.exposed_write_registers()
            self.control.exposed_start_psd_acquisition()

    def stop_psd_acquisition(self) -> None:
        """Abort a running PSD acquisition (delegates to the generic task stop)."""
        self.stop_task(use_new_parameters=False)

    def stop_lock(self) -> None:
        if self.parameters is None or self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.parameters.fetch_additional_signals.value = True
            task = self.parameters.task.value
            if task is not None:
                if hasattr(task, "stop"):
                    try:
                        task.stop()
                    except TypeError:
                        task.stop(False)
                elif hasattr(task, "exposed_stop"):
                    try:
                        task.exposed_stop()
                    except TypeError:
                        task.exposed_stop(False)
                self.parameters.task.value = None
            self.control.exposed_start_sweep()
            self._apply_deferred_sweep_geometry()

    def stop_task(self, use_new_parameters: bool = False) -> None:
        if self.parameters is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            task = self.parameters.task.value
        if task is None:
            return
        if hasattr(task, "stop"):
            try:
                with self._rpyc_lock:
                    task.stop(use_new_parameters)
                return
            except TypeError:
                with self._rpyc_lock:
                    task.stop()
                return
        if hasattr(task, "exposed_stop"):
            try:
                with self._rpyc_lock:
                    task.exposed_stop(use_new_parameters)
            except TypeError:
                with self._rpyc_lock:
                    task.exposed_stop()

    def shutdown_server(self) -> None:
        if self._recovery_active():
            raise RuntimeError("Cannot shut down the server while device recovery is running")
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_shutdown()

    def logging_start(self, interval: float) -> dict[str, Any]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        safe_interval = max(0.1, float(interval))
        with self._rpyc_lock:
            self._apply_influx_logging_params_locked()
            self.control.exposed_start_logging(safe_interval)
        self._logging_active_cache = True
        return self.set_influx_logging_state(enabled=True, interval_s=safe_interval)

    def logging_stop(self) -> dict[str, Any]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_stop_logging()
        self._logging_active_cache = False
        return self.set_influx_logging_state(enabled=False)

    def logging_set_param(self, name: str, enabled: bool) -> dict[str, Any]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        enabled_flag = bool(enabled)
        with self._rpyc_lock:
            self.control.exposed_set_parameter_log(name, enabled_flag)
            if self.parameters is not None:
                try:
                    getattr(self.parameters, name).log = enabled_flag
                except Exception:
                    logger.debug(
                        "Failed to mirror log flag for parameter=%s device=%s",
                        name,
                        self.device.key,
                        exc_info=True,
                    )
                self._invalidate_param_metadata_cache()
        current_params = self._normalize_influx_param_names(
            self.influx_logging_state.get("params")
        )
        if enabled_flag:
            if name not in current_params:
                current_params.append(name)
        else:
            current_params = [item for item in current_params if item != name]
        return self.set_influx_logging_state(
            params=current_params,
            params_configured=True,
        )

    def logging_set_params(
        self, names: list[str] | tuple[str, ...] | set[str]
    ) -> dict[str, Any]:
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        selected = set(self._normalize_influx_param_names(names))
        with self._rpyc_lock:
            loggable_names: list[str] = []
            for param_name, param in self.parameters:
                if bool(getattr(param, "loggable", False)):
                    loggable_names.append(param_name)
            loggable_set = set(loggable_names)
            unknown = sorted(name for name in selected if name not in loggable_set)
            if unknown:
                raise ValueError(
                    f"Unknown or non-loggable parameters: {', '.join(unknown)}"
                )

            applied_names: list[str] = []
            for param_name in loggable_names:
                should_log = param_name in selected
                self.control.exposed_set_parameter_log(param_name, should_log)
                try:
                    getattr(self.parameters, param_name).log = should_log
                except Exception:
                    logger.debug(
                        "Failed to mirror log flag for parameter=%s device=%s",
                        param_name,
                        self.device.key,
                        exc_info=True,
                    )
                if should_log:
                    applied_names.append(param_name)
            self._invalidate_param_metadata_cache()
        return self.set_influx_logging_state(
            params=applied_names,
            params_configured=True,
        )

    def logging_get_credentials(self) -> InfluxDBCredentials:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            return self.control.exposed_get_influxdb_credentials()

    def logging_update_credentials(
        self, credentials: InfluxDBCredentials
    ) -> tuple[bool, str]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            return self.control.exposed_update_influxdb_credentials(credentials)

    def _invalidate_param_metadata_cache(self) -> None:
        self._param_metadata_cache = None

    def param_metadata(self) -> List[Dict[str, Any]]:
        if self.parameters is None:
            raise RuntimeError("Device not connected")
        # `restorable`, `loggable`, and `log` are stable across reads for
        # the lifetime of a connection (the only mutator of `log` is this
        # module, and those code paths invalidate this cache). Caching
        # avoids ~3 RPyC round trips per parameter (~240 RTTs for the
        # ~80-param namespace) every time the Influx popover is opened.
        cached = self._param_metadata_cache
        if cached is not None:
            return cached
        data: List[Dict[str, Any]] = []
        with self._rpyc_lock:
            for name, param in self.parameters:
                data.append(
                    {
                        "name": name,
                        "restorable": bool(param.restorable),
                        "loggable": bool(param.loggable),
                        "log": bool(param.log),
                    }
                )
        self._param_metadata_cache = data
        return data
