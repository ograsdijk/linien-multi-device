"""Micro-benchmarks for proposed gateway-side optimizations.

Covers items #1, #7, #8, #12, #16 from the prioritization list:
  #1   encode_plot_frame_binary cost (per-subscriber redundancy savings)
  #7   vectorize update_histories' Python loop
  #8   float32 vs float64 series math
  #12  deepcopy(last_plot_frame) vs shallow-copy
  #16  min/max decimate 2048 -> 500 points

Each bench reports total ms + ns/op + at-12x10fps cost in ms/sec.
No browser, no asyncio, no FastAPI -- just timing the numeric work.
"""
from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

import numpy as np

# Make `app` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.stream import encode_plot_frame_binary, encode_plot_frame_json  # noqa: E402

N_POINTS = 2048
ITERS = 5000


def make_realistic_series() -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.normal(0, 0.3, N_POINTS).astype(np.float64)


def make_summary_frame() -> dict:
    return {
        "type": "plot_frame",
        "lock": False,
        "dual_channel": False,
        "series": {
            "combined_error": make_realistic_series(),
            "control_signal": make_realistic_series(),
            "error_signal_1": make_realistic_series(),
            "error_signal_2": make_realistic_series(),
            "monitor_signal": make_realistic_series(),
        },
        "signal_power": {"channel1": None, "channel2": None},
        "stats": {"error_std": 0.012, "control_std": 0.034},
        "lock_indicator": {
            "state": "unknown",
            "reasons": [],
            "metrics": {"control_stuck_s": 0.0, "control_rail_s": 0.0},
            "last_transition_at": None,
        },
        "auto_relock": {
            "enabled": False,
            "state": "idle",
            "attempts": 0,
            "max_attempts": 3,
            "cooldown_remaining_s": 0.0,
        },
        "lock_target": None,
        "x_label": "sweep voltage",
        "x_unit": "V",
    }


def bench(name: str, fn, iters: int = ITERS) -> tuple[str, float, float]:
    for _ in range(50):
        fn()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    elapsed = time.perf_counter() - t0
    return name, elapsed * 1000, (elapsed * 1e9) / iters


def fmt(row: tuple[str, float, float], at_12x10: bool = True) -> str:
    name, total_ms, ns_per_op = row
    line = f"  {name:<48} {ITERS:>6} iters  {total_ms:>9.2f} ms total  {ns_per_op:>10.0f} ns/op"
    if at_12x10:
        line += f"  {(ns_per_op * 120) / 1e6:>7.1f} ms/sec@12x10fps"
    return line


# --- #1: encode cache benefit -------------------------------------------
# Measure cost of one encode. Multiply by (N - 1) to get savings from
# caching across N subscribers viewing the same device.

print("=" * 100)
print("#1  encode_plot_frame_binary cost (per call)")
print("=" * 100)
frame = make_summary_frame()
b_bin = bench("encode_plot_frame_binary (5 series, 2048 pts)", lambda: encode_plot_frame_binary(frame))
print(fmt(b_bin))
# The cache wins when >1 subscriber per device exists. Show savings at
# 2 and 4 subscribers (e.g. overview + group both showing same device).
print()
for n_subs in (2, 3, 4):
    savings_per_frame_us = b_bin[2] * (n_subs - 1) / 1000
    savings_per_sec_ms = (b_bin[2] * (n_subs - 1) * 120) / 1e6
    print(
        f"  cache savings @ {n_subs} subs per device: "
        f"{savings_per_frame_us:.1f} us/frame, "
        f"{savings_per_sec_ms:.1f} ms/sec/device@10fps"
    )
print()
print("  context: only matters when same device has >1 active stream")
print("  (overview+group view at once). Single-subscriber case: 0 win.")
print()


# --- #7: vectorize history scaling --------------------------------------

print("=" * 100)
print("#7  history scaling (Python loop vs numpy fancy-index)")
print("=" * 100)
V = 8192


def scale_history_python(times: np.ndarray, values: list[float]) -> np.ndarray:
    out = np.full(N_POINTS, np.nan, dtype=np.float64)
    for t, v in zip(times, values):
        idx = int(round(float(t)))
        if 0 <= idx < N_POINTS:
            out[idx] = float(v) / V
    return out


def scale_history_vectorized(times: np.ndarray, values: np.ndarray) -> np.ndarray:
    out = np.full(N_POINTS, np.nan, dtype=np.float64)
    idxs = np.round(times).astype(np.int64)
    mask = (idxs >= 0) & (idxs < N_POINTS)
    out[idxs[mask]] = (values[mask].astype(np.float64)) / V
    return out


# Realistic history: 500 samples spread across N_POINTS.
HISTORY_LEN = 500
hist_times_arr = np.linspace(0, N_POINTS - 1, HISTORY_LEN).astype(np.float64)
hist_values_list = [float(np.random.normal(0, 100)) for _ in range(HISTORY_LEN)]
hist_values_arr = np.array(hist_values_list, dtype=np.float64)

b_py = bench(
    "_scale_history Python loop (500 history samples)",
    lambda: scale_history_python(hist_times_arr, hist_values_list),
)
b_vec = bench(
    "_scale_history vectorized (500 history samples)",
    lambda: scale_history_vectorized(hist_times_arr, hist_values_arr),
)
print(fmt(b_py))
print(fmt(b_vec))
speedup = b_py[2] / b_vec[2]
print()
print(f"  speedup: {speedup:.1f}x")
print("  context: only fires on FULL-detail frames (group panel, manual lock tab).")
print("  Summary-detail subscribers (overview/group thumbnails) don't pay this.")
print()


# --- #8: float32 vs float64 throughout ----------------------------------

print("=" * 100)
print("#8  float32 vs float64 series math")
print("=" * 100)
int16_series = (np.random.normal(0, 2000, N_POINTS)).astype(np.int16)

b_f64 = bench(
    "(int16 / V).astype(float64) -- 5 series",
    lambda: [(int16_series / V).astype(np.float64) for _ in range(5)],
)
b_f32 = bench(
    "(int16 / V).astype(float32) -- 5 series",
    lambda: [(int16_series / V).astype(np.float32) for _ in range(5)],
)
print(fmt(b_f64))
print(fmt(b_f32))
saved_ns = b_f64[2] - b_f32[2]
print()
print(f"  savings per frame: {saved_ns/1000:.1f} us")
print(f"  at 12x10fps: {(saved_ns * 120) / 1e6:.1f} ms/sec gateway")
print(f"  + per-frame memory bandwidth halved (10240 -> 5120 floats stored)")
print()


# --- #12: deepcopy last_plot_frame --------------------------------------

print("=" * 100)
print("#12  deepcopy(last_plot_frame) vs shallow copy")
print("=" * 100)
b_deep = bench("copy.deepcopy(frame) -- numpy series, full metadata", lambda: copy.deepcopy(frame))


def shallow_copy_frame(f: dict) -> dict:
    return dict(f)


b_shallow = bench("dict(frame) shallow", lambda: shallow_copy_frame(frame))
print(fmt(b_deep, at_12x10=False))
print(fmt(b_shallow, at_12x10=False))
print()
print(f"  speedup: {b_deep[2] / b_shallow[2]:.0f}x")
print("  context: called once per WS connect. With 12 cards reconnecting at")
print("  page refresh, deepcopy fires 12x:")
print(f"    deepcopy x 12: {(b_deep[2] * 12) / 1e6:.1f} ms cold-start spike")
print(f"    shallow x 12:  {(b_shallow[2] * 12) / 1e6:.3f} ms cold-start spike")
print()


# --- #16: server-side decimation -----------------------------------------

print("=" * 100)
print("#16  server-side min/max decimate 2048 -> 500 points")
print("=" * 100)


def decimate_minmax(arr: np.ndarray, out_len: int) -> np.ndarray:
    """Min/max preserving decimation. Each output pair is (min, max) of a bucket."""
    bucket = len(arr) // (out_len // 2)
    if bucket < 2:
        return arr.copy()
    trimmed = arr[: (out_len // 2) * bucket].reshape(-1, bucket)
    out = np.empty(out_len, dtype=arr.dtype)
    out[0::2] = trimmed.min(axis=1)
    out[1::2] = trimmed.max(axis=1)
    return out


full_series = make_realistic_series()

b_dec = bench(
    "min/max decimate 2048->500 (one series)",
    lambda: decimate_minmax(full_series, 500),
)
print(fmt(b_dec))
print()

# Size reduction for the resulting binary frame.
from app.stream import encode_plot_frame_binary  # already imported but for clarity

decimated_frame = make_summary_frame()
for k, v in decimated_frame["series"].items():
    decimated_frame["series"][k] = decimate_minmax(v, 500).astype(np.float32)

full_bytes = encode_plot_frame_binary(frame)
dec_bytes = encode_plot_frame_binary(decimated_frame)
print(f"  binary frame size 2048pts: {len(full_bytes):>6} bytes")
print(f"  binary frame size  500pts: {len(dec_bytes):>6} bytes")
print(f"  shrink factor: {len(full_bytes) / len(dec_bytes):.1f}x")
print()
print(f"  decimation cost per 5-series frame: {(b_dec[2] * 5) / 1000:.1f} us")
print(f"  at 12x10fps: gateway adds {(b_dec[2] * 5 * 120) / 1e6:.1f} ms/sec")
print(
    f"  frontend saves (M2 baseline 2.4us per binary decode): "
    f"~{(2.4 * (len(full_bytes) - len(dec_bytes)) / len(full_bytes)):.1f} us/frame"
)
print(f"  bandwidth saved: {(len(full_bytes) - len(dec_bytes)) * 120 / 1024:.0f} KB/s at 12x10fps")
