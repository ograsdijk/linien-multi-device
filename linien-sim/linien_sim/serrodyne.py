"""Pure functions describing the imperfect-serrodyne order model (spec §C3).

These are deliberately free of any I/O or simulator state so that both the
simulator (`linien_sim.model.VirtualPdhModel`) and test code (including a
future centrex test fake) can import and mirror them exactly.

Physics / sign convention (see the shared spec, §P):

- Serrodyne order ``n`` puts an optical component at ``nu_n = nu_0 + n *
  f_serrodyne`` where ``nu_0`` is the carrier (unshifted) optical frequency
  and ``f_serrodyne`` is the NLTL/serrodyne drive frequency.
- That component's own PDH feature appears on the cavity scan where
  ``nu_n == nu_cav``, i.e. where ``nu_0 == nu_cav - n * f_serrodyne``. In
  optical-detuning terms (``nu_0 - nu_cav``), the order-n feature therefore
  sits at ``-n * f_serrodyne``.
- ``sweep_frequency_sign`` (``s in {+1, -1}``) maps the simulator's internal
  sweep/detuning coordinate to true optical detuning:
  ``optical_detuning = s * internal_detuning``. Solving for the internal
  detuning of the order-n feature gives ``offset_n = -n * s * f_serrodyne``
  (using ``1/s == s`` for ``s in {+1, -1}``).
- Consequently, moving ``f_serrodyne`` by ``delta_f`` moves the order-n
  feature by ``-n * s * delta_f`` in the internal detuning (Hz) coordinate,
  and by ``-n * s * N_SB * delta_f / f_PDH`` samples once expressed in the
  sweep-sample domain (``N_SB`` = carrier-to-sideband spacing in samples,
  ``f_PDH`` = the PDH modulation frequency).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

DEFAULT_SERRODYNE_ORDERS: tuple[int, ...] = (-2, -1, 0, 1, 2)


@dataclass
class SerrodyneConfig:
    """Full control state for the optional imperfect-serrodyne order model.

    Disabled (``enabled=False``, the default) means the simulator's PDH error
    and monitor signals are computed exactly as before this feature existed
    (single order, no serrodyne sum) -- see
    `VirtualPdhModel._pdh_error`/`_monitor_signal`.

    Weight formulas (see `serrodyne_order_weights`):

    Power-dependent mode (``use_power_dependence=True``, default): let
    ``d = rf_power_dbm - p_opt_dbm`` (deviation from the known synthetic
    optimum, in dB).

    - ``w[+1] = desired_peak_weight * exp(-d**2 / (2 * weight_sigma_db**2))``
      -- the desired first order, a Gaussian in dB peaking exactly at
      ``p_opt_dbm``.
    - ``w[-1] = asymmetry_ratio * w[+1]`` -- the unwanted first order,
      suppressed relative to the desired one by a fixed ratio (imperfect
      serrodyne asymmetry). It shares the same power dependence/optimum as
      ``w[+1]``, just scaled down.
    - ``w[0] = carrier_floor_weight + carrier_growth_per_db2 * d**2`` -- the
      leaked carrier, minimal at the optimum and growing quadratically away
      from it.
    - ``w[+2] = w[-2] = second_order_floor_weight +
      second_order_growth_per_db2 * d**2`` -- second-order leakage, same
      shape as the carrier term but with its own floor/growth.
    - Any other requested order uses the second-order formula.

    Fixed mode (``use_power_dependence=False``): every requested order's
    weight comes straight from ``fixed_base_weights`` (0.0 if absent), e.g.
    the documented ``{0: 0.10, 1: 0.82, -1: 0.03, 2: 0.05, -2: 0.02}``.
    """

    enabled: bool = False
    frequency_hz: float = 0.0
    rf_power_dbm: float = 0.0
    orders: tuple[int, ...] = DEFAULT_SERRODYNE_ORDERS
    sweep_frequency_sign: int = 1

    use_power_dependence: bool = True
    p_opt_dbm: float = 0.0
    weight_sigma_db: float = 6.0
    desired_peak_weight: float = 0.82
    asymmetry_ratio: float = 0.05
    carrier_floor_weight: float = 0.02
    carrier_growth_per_db2: float = 0.002
    second_order_floor_weight: float = 0.01
    second_order_growth_per_db2: float = 0.0015
    fixed_base_weights: dict[int, float] = field(
        default_factory=lambda: {0: 0.10, 1: 0.82, -1: 0.03, 2: 0.05, -2: 0.02}
    )


def serrodyne_order_weights(
    power_dbm: float, cfg: SerrodyneConfig
) -> dict[int, float]:
    """Return {order: weight} for every order in ``cfg.orders``.

    Pure function of ``power_dbm`` and ``cfg`` -- see `SerrodyneConfig` for
    the exact formulas. Weights are never negative.
    """
    if not cfg.use_power_dependence:
        return {n: float(cfg.fixed_base_weights.get(n, 0.0)) for n in cfg.orders}

    deviation_db = float(power_dbm) - float(cfg.p_opt_dbm)
    deviation_db2 = deviation_db * deviation_db
    sigma = max(float(cfg.weight_sigma_db), 1e-6)
    gaussian = math.exp(-deviation_db2 / (2.0 * sigma * sigma))
    w_plus1 = max(0.0, float(cfg.desired_peak_weight) * gaussian)

    weights: dict[int, float] = {}
    for n in cfg.orders:
        if n == 1:
            weights[n] = w_plus1
        elif n == -1:
            weights[n] = max(0.0, float(cfg.asymmetry_ratio) * w_plus1)
        elif n == 0:
            weights[n] = max(
                0.0,
                float(cfg.carrier_floor_weight)
                + float(cfg.carrier_growth_per_db2) * deviation_db2,
            )
        else:
            weights[n] = max(
                0.0,
                float(cfg.second_order_floor_weight)
                + float(cfg.second_order_growth_per_db2) * deviation_db2,
            )
    return weights


def serrodyne_feature_offsets_hz(
    f_serrodyne_hz: float,
    orders: Sequence[int],
    *,
    sweep_frequency_sign: int = 1,
) -> dict[int, float]:
    """Return {order: offset_hz} in the simulator's INTERNAL detuning
    coordinate (the ``detuning_hz`` argument fed to the single-order PDH
    model) at which each order's feature crosses zero.

    ``offset_n = -n * s * f_serrodyne_hz`` where ``s = sweep_frequency_sign``
    (see module docstring for the derivation).
    """
    s = 1 if sweep_frequency_sign >= 0 else -1
    return {n: -float(n) * s * float(f_serrodyne_hz) for n in orders}


def serrodyne_order_weights_and_offsets(
    power_dbm: float,
    f_serrodyne_hz: float,
    cfg: SerrodyneConfig,
) -> tuple[Mapping[int, float], Mapping[int, float]]:
    """Convenience: (weights, offsets_hz) for every order in ``cfg.orders``."""
    weights = serrodyne_order_weights(power_dbm, cfg)
    offsets = serrodyne_feature_offsets_hz(
        f_serrodyne_hz, cfg.orders, sweep_frequency_sign=cfg.sweep_frequency_sign
    )
    return weights, offsets
