"""Snapshot of the Linien lock configuration at a lock acquisition.

Values are stored raw, in Linien's own units (register counts, internal
scales), exactly as the parameter reports them; the converted columns of the
acquisition record (modulation_frequency_hz, pid_p, ...) exist alongside for SQL
convenience. Plot buffers, histories, task/optimization/PSD runtime state and
anything credential-like are deliberately not part of the snapshot.
"""
from __future__ import annotations

import importlib.metadata
import time
from typing import Any, Callable

from .manual_lock_record import _json_safe
from .serializers import UNSERIALIZABLE, to_jsonable

# Bumped whenever the snapshot's shape or the meaning of a key changes.
LINIEN_CONFIG_SCHEMA_VERSION = 1

# Lock-relevant Linien parameters (names as in linien-server 2.x).
LINIEN_CONFIG_PARAMETERS: tuple[str, ...] = (
    # Output / input routing
    "mod_channel",
    "control_channel",
    "sweep_channel",
    "slow_control_channel",
    "polarity_fast_out1",
    "polarity_fast_out2",
    "polarity_analog_out0",
    # Modulation and demodulation
    "modulation_frequency",
    "modulation_amplitude",
    "demodulation_phase_a",
    "demodulation_phase_b",
    "demodulation_multiplier_a",
    "demodulation_multiplier_b",
    "invert_a",
    "invert_b",
    # Error-signal combination
    "dual_channel",
    "channel_mixing",
    "offset_a",
    "offset_b",
    "combined_offset",
    # Error-signal filters
    "filter_automatic_a",
    "filter_automatic_b",
    "filter_1_enabled_a",
    "filter_1_enabled_b",
    "filter_2_enabled_a",
    "filter_2_enabled_b",
    "filter_1_type_a",
    "filter_1_type_b",
    "filter_2_type_a",
    "filter_2_type_b",
    "filter_1_frequency_a",
    "filter_1_frequency_b",
    "filter_2_frequency_a",
    "filter_2_frequency_b",
    # Controller
    "p",
    "i",
    "d",
    "target_slope_rising",
    "pid_on_slow_enabled",
    "pid_on_slow_strength",
    "pid_only_mode",
    # Sweep
    "sweep_center",
    "sweep_amplitude",
    "sweep_speed",
    "sweep_pause",
    # Linien's own lock watching / autolock preferences
    "check_lock",
    "watch_lock",
    "watch_lock_threshold",
    "autolock_mode_preference",
    "autolock_determine_offset",
    # Lock state at the moment of the snapshot
    "lock",
)

_MISSING = object()


def _linien_client_version() -> str | None:
    try:
        return importlib.metadata.version("linien-client")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_linien_config(
    read: Callable[[str, Any], Any],
    *,
    captured_at_stage: str,
    gateway: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The snapshot dict. ``read(name, default)`` returns a parameter's value
    or ``default`` when it does not exist or cannot be read; such parameters
    are listed under ``missing`` instead of failing the snapshot."""
    parameters: dict[str, Any] = {}
    missing: list[str] = []
    unserializable: list[str] = []
    for name in LINIEN_CONFIG_PARAMETERS:
        try:
            value = read(name, _MISSING)
        except Exception:  # noqa: BLE001 - one unreadable value never fails the snapshot
            value = _MISSING
        if value is _MISSING:
            missing.append(name)
            continue
        encoded = to_jsonable(value)
        if encoded is UNSERIALIZABLE:
            unserializable.append(name)
            continue
        parameters[name] = _json_safe(encoded)
    snapshot: dict[str, Any] = {
        "schema_version": LINIEN_CONFIG_SCHEMA_VERSION,
        "captured_at": time.time(),
        "captured_at_stage": captured_at_stage,
        "linien_client_version": _linien_client_version(),
        "units": "raw Linien parameter values (internal units)",
        "parameters": parameters,
        "missing": missing,
    }
    if unserializable:
        snapshot["unserializable"] = unserializable
    if gateway:
        # The gateway's own lock settings (auto-lock detector, acceptance,
        # lock indicator): they shape the acquisition as much as Linien's.
        encoded_gateway = {}
        for key, value in gateway.items():
            encoded = to_jsonable(value)
            if encoded is not UNSERIALIZABLE:
                encoded_gateway[key] = _json_safe(encoded)
        snapshot["gateway"] = encoded_gateway
    return snapshot
