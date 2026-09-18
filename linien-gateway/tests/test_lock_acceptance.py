"""The acceptance geometry the auto-lock refinement walk judges a landing by."""

import pytest

from app.lock_acceptance import (
    AcceptanceSettings,
    acceptance_window_v,
    capture_tolerance_v,
    rejection_bound_v,
)


def _settings(**overrides) -> AcceptanceSettings:
    base = dict(
        settle_ms=300,
        capture_fraction=0.5,
        max_correction_span=4.0,
    )
    base.update(overrides)
    return AcceptanceSettings(**base)


def test_from_mapping_fills_gaps_with_defaults_and_ignores_extras():
    settings = AcceptanceSettings.from_mapping({"settle_ms": 50, "junk": 1})
    assert settings.settle_ms == 50
    assert settings.capture_fraction == AcceptanceSettings().capture_fraction


def test_capture_tolerance_scales_with_the_calibrated_feature_width():
    assert capture_tolerance_v(_settings(capture_fraction=0.5), 0.08) == pytest.approx(0.04)
    assert capture_tolerance_v(_settings(capture_fraction=0.25), 0.08) == pytest.approx(0.02)


def test_capture_tolerance_is_never_negative():
    assert capture_tolerance_v(_settings(capture_fraction=0.5), -0.08) == pytest.approx(0.04)


def test_rejection_bound_falls_back_to_the_feature_width_without_a_sideband():
    assert rejection_bound_v(_settings(max_correction_span=4.0), 0.08) == pytest.approx(0.32)


def test_a_known_sideband_offset_tightens_the_rejection_bound():
    # Sidebands 0.5 V out: anything past 0.2 V is nearer the sideband than the
    # carrier, which is tighter than 4 x 0.08 = 0.32 V.
    bound = rejection_bound_v(_settings(max_correction_span=4.0), 0.08, sideband_offset_v=0.5)
    assert bound == pytest.approx(0.2)


def test_a_wide_sideband_offset_does_not_loosen_the_bound():
    bound = rejection_bound_v(_settings(max_correction_span=4.0), 0.08, sideband_offset_v=5.0)
    assert bound == pytest.approx(0.32)


def test_a_tight_signal_tightens_the_window_rather_than_loosening_the_guard():
    """Closely spaced features must still leave room to correct.

    Before, the bound was clamped up to meet the window, so the two met and
    every offset that failed acceptance was immediately called a neighbouring
    crossing -- the correction loop never ran and the message was wrong.
    """
    settings = _settings(capture_fraction=0.5, max_correction_span=4.0)
    window = acceptance_window_v(settings, 0.02, sideband_offset_v=0.015)

    assert window.bound_v == pytest.approx(0.006)  # 0.4 x the sideband spacing
    assert window.tolerance_v == pytest.approx(0.003)  # tightened to half the bound
    assert window.tolerance_v < window.bound_v
    assert window.tightened is True


def test_a_roomy_signal_keeps_the_configured_window():
    settings = _settings(capture_fraction=0.5, max_correction_span=4.0)
    window = acceptance_window_v(settings, 0.02, sideband_offset_v=0.5)

    assert window.tolerance_v == pytest.approx(0.01)  # 0.5 x the feature half-width
    assert window.bound_v == pytest.approx(0.08)
    assert window.tightened is False


def test_a_generous_capture_fraction_cannot_swallow_the_correction_window():
    settings = _settings(capture_fraction=4.0, max_correction_span=1.0)
    window = acceptance_window_v(settings, 0.02)

    assert window.tolerance_v == pytest.approx(0.5 * window.bound_v)
    assert window.tolerance_v < window.bound_v


def test_a_zero_sideband_offset_is_ignored_rather_than_collapsing_the_bound():
    bound = rejection_bound_v(_settings(max_correction_span=4.0), 0.08, sideband_offset_v=0.0)
    assert bound == pytest.approx(0.32)


def test_engine_and_schema_settings_stay_in_parity():
    """The engine dataclass and the Pydantic boundary model must declare the same
    field names and defaults, so the two definitions cannot drift."""
    import dataclasses

    from app.schemas import LockAcceptanceSettings as SchemaLockAcceptanceSettings

    engine_fields = {f.name: f.default for f in dataclasses.fields(AcceptanceSettings)}
    schema_fields = {
        name: info.default
        for name, info in SchemaLockAcceptanceSettings.model_fields.items()
    }
    assert engine_fields.keys() == schema_fields.keys()
    assert engine_fields == schema_fields


def test_a_zero_correction_span_disables_the_guard_instead_of_rejecting_everything():
    """A bound of 0 would make every nonzero offset a "different feature", so a
    single innocuous-looking setting would make the device unlockable."""
    window = acceptance_window_v(_settings(max_correction_span=0.0), 0.02)

    assert window.bound_v is None
    assert window.tolerance_v == pytest.approx(0.01)


def test_an_uncalibrated_feature_width_yields_no_window_at_all():
    # half_range_sweep_v = 0 means the device was never calibrated; callers must
    # refuse rather than silently reject every landing.
    window = acceptance_window_v(_settings(), 0.0)

    assert window.tolerance_v == 0.0
    assert window.bound_v is None
