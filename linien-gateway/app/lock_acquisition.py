"""One PDH lock-acquisition attempt and the unlocked trace it was decided on.

A `LockAcquisition` is created when an attempt passes its prechecks (device
connected, not already locked, actuator free) and before any hardware command
is issued. Its `lock_acquisition_id` (UUID4) identifies exactly one attempt for
its whole life -- the API response, the log events, the PostgreSQL row and any
later update of that row all carry the same ID -- whether or not PostgreSQL is
configured.

The auto-lock paths analyse several traces (direct detection, refinement
stages, final verification). Each capture helper in session.py reports the
trace it analysed together with the sweep geometry it was detected against
through `note_capture`; at the end the attempt keeps the capture that produced
the result it actually locked on (`capture_for`), so a trace from one sweep is
never paired with the geometry of another. The current attempt is found through
a context variable, so the capture helpers need no extra parameters and an
operation that is not an acquisition (candidate probes, calibration, staged
`step`) records nothing.
"""
from __future__ import annotations

import contextvars
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

# Bumped whenever the meaning of a stored field changes.
ACQUISITION_RECORD_VERSION = 1

LOCK_SOURCES = ("manual_lock", "auto_lock_scan", "staged_autolock", "auto_relock")
LOCK_COMMAND_STATUSES = ("not_sent", "accepted", "rejected")
FAILURE_STAGES = (
    "trace_capture",
    "detection",
    "refinement",
    "lock_command",
)

# Trace roles. The primary trace is always the combined error signal Linien
# locks on. The secondary trace is whatever the board sent on the second input:
# a monitor photodiode, or -- in dual-channel mode -- a second demodulated error
# signal, which must never be labelled a monitor.
TRACE_ROLE_COMBINED_ERROR = "combined_error"
SECONDARY_ROLE_MONITOR = "monitor"
SECONDARY_ROLE_ERROR_SIGNAL_2 = "error_signal_2"

# How many detection captures one attempt keeps. A refinement walk analyses a
# handful of frames; the one that matters is always among the most recent.
MAX_CAPTURES = 16


@dataclass(frozen=True)
class TraceSnapshot:
    """An immutable copy of one unlocked sweep and the geometry it was taken at.

    Traces are in volts (ADC counts / 8192, the plot scale). ``frame_id`` and
    ``frame_processed_at`` identify the gateway plot frame (processed, not
    board-acquisition, time). ``sweep_center_v``/``sweep_amplitude_v`` are the
    geometry the auto-lock detector used for this trace: the one stored with
    the frame when it arrived where available, else a read-back right after the
    trace was copied (``geometry_source`` says which; ``geometry_read_at`` is
    when it was taken).
    """

    error_v: np.ndarray | None
    secondary_v: np.ndarray | None
    secondary_role: str | None
    frame_id: int | None
    frame_processed_at: float | None
    sweep_center_v: float | None
    sweep_amplitude_v: float | None
    geometry_read_at: float | None
    purpose: str
    detector: str | None = None
    sweep_paused: bool | None = None
    unavailable_reason: str | None = None
    # "frame_callback": the geometry stored with the frame when it arrived
    # (DeviceSession._unlocked_geometry_by_frame). "device_readback": read
    # from the device after the trace was copied, when no frame geometry was
    # stored.
    geometry_source: str | None = None

    @property
    def available(self) -> bool:
        return self.error_v is not None and self.unavailable_reason is None


def unavailable_trace(reason: str, *, purpose: str) -> TraceSnapshot:
    return TraceSnapshot(
        error_v=None,
        secondary_v=None,
        secondary_role=None,
        frame_id=None,
        frame_processed_at=None,
        sweep_center_v=None,
        sweep_amplitude_v=None,
        geometry_read_at=None,
        purpose=purpose,
        unavailable_reason=reason,
    )


@dataclass
class _Capture:
    results: tuple[Any, ...]
    snapshot: TraceSnapshot


@dataclass
class LockAcquisition:
    device_key: str
    laser_name: str
    lock_source: str
    lock_acquisition_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    attempt_started_at: float = field(default_factory=time.time)
    params: dict[str, Any] = field(default_factory=dict)
    trace: TraceSnapshot | None = None
    # What the attempt is doing now; becomes `failure_stage` if it fails.
    stage: str = "trace_capture"
    lock_command_at: float | None = None
    lock_command_status: str = "not_sent"
    failure_stage: str | None = None
    failure_reason: str | None = None
    refinement: dict[str, Any] | None = None
    # Raw Linien configuration (see linien_config.build_linien_config).
    linien_config: dict[str, Any] | None = None
    # Diagnostic description of the stored trace (lock_characterization).
    characterization: dict[str, Any] | None = None
    # The detection result the lock was decided on, for characterization.
    decision_result: Any = field(default=None, repr=False)
    verification_status: str = "not_attempted"
    verification_reason: str | None = None
    verification_completed_at: float | None = None
    # Path-specific facts about which trace was kept (e.g. the staged run's
    # selected frame), merged into the stored trace metadata.
    trace_context: dict[str, Any] = field(default_factory=dict)
    # Frame identity of trace arrays handed out by the snapshot helpers,
    # matched by object identity when the detection is noted.
    _frame_meta: list[tuple[np.ndarray, dict[str, Any]]] = field(
        default_factory=list, repr=False
    )
    _captures: list[_Capture] = field(default_factory=list, repr=False)
    _recorded: bool = field(default=False, repr=False)

    @property
    def success(self) -> bool:
        """Backward-compatible `success`: the lock command was accepted.

        This is NOT proof of a stable lock; verification is recorded separately.
        """
        return self.lock_command_status == "accepted"

    # ------------------------------------------------------------- captures

    def note_frame(self, error_v: np.ndarray, meta: dict[str, Any]) -> None:
        self._frame_meta.append((error_v, meta))
        del self._frame_meta[:-MAX_CAPTURES]

    def frame_meta_for(self, error_v: Any) -> dict[str, Any]:
        for array, meta in reversed(self._frame_meta):
            if array is error_v:
                return meta
        return {}

    def note_capture(self, snapshot: TraceSnapshot) -> _Capture:
        """Record a trace about to be analysed; the caller attaches the
        detection results to the returned capture once the detector accepts
        the trace. A rejected trace stays recorded without results, so a
        failed attempt can still store the last frame it analysed."""
        capture = _Capture((), snapshot)
        self._captures.append(capture)
        del self._captures[:-MAX_CAPTURES]
        if self.stage == "trace_capture":
            self.stage = "detection"
        return capture

    def capture_for(self, result: Any) -> TraceSnapshot | None:
        """The trace the given detection result came from, if it was noted."""
        for capture in reversed(self._captures):
            if any(item is result for item in capture.results):
                return capture.snapshot
        return None

    def latest_capture(self) -> TraceSnapshot | None:
        return self._captures[-1].snapshot if self._captures else None

    # ------------------------------------------------------------- outcome

    def mark_lock_command(self, accepted: bool, at: float | None = None) -> None:
        self.stage = "lock_command"
        self.lock_command_at = time.time() if at is None else float(at)
        self.lock_command_status = "accepted" if accepted else "rejected"

    def fail(self, exc: BaseException | str) -> None:
        if self.failure_stage is None:
            self.failure_stage = self.stage
            self.failure_reason = str(exc) or type(exc).__name__

    def claim_record(self) -> bool:
        """True exactly once: guards against writing one attempt twice."""
        if self._recorded:
            return False
        self._recorded = True
        return True


VERIFICATION_STATUSES = ("not_attempted", "pending", "verified", "failed", "unavailable")

# How long after the lock command verification waits for the lock indicator to
# confirm the lock before declaring it failed.
VERIFY_TIMEOUT_S = 15.0
# A verification that sees no plot frame at all for this long is unavailable,
# not failed: nothing was observed either way.
VERIFY_NO_FRAME_S = 5.0


@dataclass
class VerificationTracker:
    """Decides whether an accepted lock command produced a stable lock.

    Fed once per plot frame with Linien's ``lock`` flag and the gateway lock
    indicator's state (the same evaluator auto-relock uses). Verified once the
    indicator has said "locked" continuously for ``hold_s`` on frames after the
    command; failed if the lock drops, the indicator reports "lost", or the
    timeout passes without confirmation; unavailable if no frames arrive.
    """

    commanded_at: float
    hold_s: float
    timeout_s: float = VERIFY_TIMEOUT_S
    no_frame_s: float = VERIFY_NO_FRAME_S
    locked_since: float | None = None
    last_frame_at: float | None = None
    frames: int = 0
    outcome: tuple[str, str | None] | None = None

    def observe(
        self, now: float, *, lock: bool, indicator_state: str | None
    ) -> tuple[str, str | None] | None:
        if self.outcome is not None or now < self.commanded_at:
            return self.outcome
        self.frames += 1
        self.last_frame_at = now
        if not lock:
            # Frames from before the lock took hold are expected right after
            # the command; only a sustained unlock after that is a failure.
            if now - self.commanded_at >= self.hold_s:
                self.outcome = ("failed", "lock_dropped")
            self.locked_since = None
            return self.outcome
        if indicator_state == "lost":
            self.outcome = ("failed", "lock_indicator_lost")
        elif indicator_state == "locked":
            if self.locked_since is None:
                self.locked_since = now
            if now - self.locked_since >= self.hold_s:
                self.outcome = ("verified", None)
        else:
            self.locked_since = None
        if self.outcome is None:
            self.check_deadline(now)
        return self.outcome

    def check_deadline(self, now: float) -> tuple[str, str | None] | None:
        if self.outcome is not None:
            return self.outcome
        if now - self.commanded_at < self.timeout_s:
            reference = self.last_frame_at or self.commanded_at
            if self.frames == 0 and now - reference >= self.no_frame_s:
                self.outcome = ("unavailable", "no_plot_frames")
            return self.outcome
        if self.frames == 0:
            self.outcome = ("unavailable", "no_plot_frames")
        else:
            self.outcome = ("failed", "not_confirmed_within_timeout")
        return self.outcome


_current: contextvars.ContextVar[LockAcquisition | None] = contextvars.ContextVar(
    "current_lock_acquisition", default=None
)


def current_acquisition() -> LockAcquisition | None:
    return _current.get()


@contextmanager
def acquisition_scope(acquisition: LockAcquisition) -> Iterator[LockAcquisition]:
    token = _current.set(acquisition)
    try:
        yield acquisition
    finally:
        _current.reset(token)


def attach_acquisition_id(exc: BaseException, acquisition: LockAcquisition) -> None:
    """Let the API layer report the failed attempt's ID with the error."""
    try:
        setattr(exc, "lock_acquisition_id", acquisition.lock_acquisition_id)
    except Exception:  # noqa: BLE001 - some exception types refuse attributes
        pass
