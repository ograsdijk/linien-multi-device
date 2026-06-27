"""Raw per-frame signal statistics.

These are plain ``mean``/``std``/``range`` reductions over the plot arrays. They
do not depend on the lock indicator in any way, so they live here as a standalone
computation that the plot path runs whenever the device is locked. Both the REST
``status()`` readout and the lock indicator consume them; the indicator no longer
*owns* them (disabling the indicator must not hide the control-voltage readout).

Voltage fields are scaled by :data:`ADC_SCALE` (counts per volt). ``control_range``
stays in raw counts because the indicator's stuck threshold is expressed in counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

# Canonical ADC scale (counts per volt). Single source of truth -- plot_processing
# imports this as ``V`` rather than redefining it.
ADC_SCALE = 8192.0


def _plot_array(to_plot: Mapping[str, Any] | None, name: str) -> np.ndarray | None:
    if not isinstance(to_plot, Mapping):
        return None
    values = to_plot.get(name)
    if values is None:
        return None
    try:
        arr = np.asarray(values, dtype=float)
    except Exception:
        return None
    if arr.ndim != 1 or arr.size == 0:
        return None
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


@dataclass
class SignalStats:
    """Per-frame signal statistics (``None`` when the source signal is absent)."""

    control_mean_v: float | None = None
    control_std_v: float | None = None
    control_range_counts: float | None = None  # raw counts, not volts
    error_std_v: float | None = None
    error_mean_abs_v: float | None = None
    monitor_mean_v: float | None = None


def compute_signal_stats(to_plot: Mapping[str, Any] | None) -> SignalStats:
    """Reduce a ``to_plot`` payload to scalar signal statistics.

    Callers gate this on ``lock``: an unlocked/sweeping frame has no meaningful
    control voltage, so callers pass an empty :class:`SignalStats` instead.
    """
    stats = SignalStats()

    error = _plot_array(to_plot, "error_signal")
    if error is not None:
        stats.error_std_v = float(np.std(error) / ADC_SCALE)
        stats.error_mean_abs_v = abs(float(np.mean(error) / ADC_SCALE))

    control = _plot_array(to_plot, "control_signal")
    if control is not None:
        stats.control_mean_v = float(np.mean(control) / ADC_SCALE)
        stats.control_std_v = float(np.std(control) / ADC_SCALE)
        stats.control_range_counts = float(np.max(control) - np.min(control))

    monitor = _plot_array(to_plot, "monitor_signal")
    if monitor is not None:
        stats.monitor_mean_v = float(np.mean(monitor) / ADC_SCALE)

    return stats
