"""`DeviceSession.auto_lock_detect(detector=...)`: strict (default), coarse, or
auto (strict falling back to coarse), all on the same cached frame."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import app.session as session_module
from tests.test_staged_autolock import _make_session, _result


def _session(monkeypatch, *, strict, coarse):
    session = _make_session()
    trace = np.zeros(2048)
    monkeypatch.setattr(
        session, "_snapshot_auto_lock_traces_with_frame", lambda: (trace, None, 7, 123.0)
    )
    monkeypatch.setattr(session, "_snapshot_sweep_params", lambda: (0.0, 1.0, True, 10e6))
    calls: list[str] = []

    def _strict(**kwargs):
        calls.append("strict")
        if isinstance(strict, Exception):
            raise strict
        return strict

    def _coarse(**kwargs):
        calls.append("coarse")
        if isinstance(coarse, Exception):
            raise coarse
        return [SimpleNamespace(result=r) for r in coarse]

    monkeypatch.setattr(session_module, "find_auto_lock_candidates", _strict)
    monkeypatch.setattr(session_module, "find_plausible_coarse_candidates", _coarse)
    return session, calls


def test_strict_is_the_default_and_never_tries_coarse(monkeypatch):
    session, calls = _session(
        monkeypatch, strict=ValueError("too wide"), coarse=[_result(5, 0.1)]
    )
    result = session.auto_lock_detect(None)
    assert calls == ["strict"]
    assert result["found"] is False
    assert result["detector"] == "strict"
    assert result["reason"] == "too wide"


def test_auto_falls_back_to_coarse_on_the_same_frame(monkeypatch):
    session, calls = _session(
        monkeypatch, strict=ValueError("too wide"), coarse=[_result(5, 0.1)]
    )
    result = session.auto_lock_detect(None, detector="auto")
    assert calls == ["strict", "coarse"]
    assert result["detector"] == "coarse"
    assert [c["target_index"] for c in result["candidates"]] == [5]
    assert result["frame"]["frame_id"] == 7
    assert result["reason"] is None


def test_auto_keeps_strict_candidates_when_strict_finds_some(monkeypatch):
    session, calls = _session(monkeypatch, strict=[_result(3, 0.0)], coarse=[_result(5, 0.1)])
    result = session.auto_lock_detect(None, detector="auto")
    assert calls == ["strict"]
    assert result["detector"] == "strict"


def test_coarse_only_skips_strict(monkeypatch):
    session, calls = _session(monkeypatch, strict=[_result(3, 0.0)], coarse=[_result(5, 0.1)])
    result = session.auto_lock_detect(None, detector="coarse")
    assert calls == ["coarse"]
    assert result["detector"] == "coarse"


def test_auto_reports_both_reasons_when_nothing_is_found(monkeypatch):
    session, _calls = _session(
        monkeypatch, strict=ValueError("too wide"), coarse=ValueError("no pair")
    )
    result = session.auto_lock_detect(None, detector="auto")
    assert result["found"] is False
    assert "too wide" in result["reason"] and "no pair" in result["reason"]


def test_unknown_detector_raises(monkeypatch):
    session, _calls = _session(monkeypatch, strict=[], coarse=[])
    with pytest.raises(ValueError):
        session.auto_lock_detect(None, detector="bogus")


# ---- include_coarse: read-only coarse candidates for order identification --------


def _coarse_session(monkeypatch, *, strict, coarse):
    session, calls = _session(monkeypatch, strict=strict, coarse=coarse)
    seen: dict = {}
    real = session_module.find_plausible_coarse_candidates

    def _coarse_kwargs(**kwargs):
        seen.update({k: kwargs[k] for k in ("min_relative_score", "max_candidates") if k in kwargs})
        return real(**kwargs)

    monkeypatch.setattr(session_module, "find_plausible_coarse_candidates", _coarse_kwargs)
    return session, calls, seen


def test_include_coarse_adds_coarse_candidates_on_the_same_frame(monkeypatch):
    session, _calls, _seen = _coarse_session(
        monkeypatch, strict=[_result(3, 0.0)], coarse=[_result(3, 0.0), _result(9, 0.4)]
    )
    result = session.auto_lock_detect(None, include_coarse=True)
    assert result["detector"] == "strict"
    assert [c["target_index"] for c in result["candidates"]] == [3]  # main result unchanged
    assert [c["target_index"] for c in result["coarse_candidates"]] == [3, 9]
    assert result["coarse_frame"]["frame_id"] == result["frame"]["frame_id"] == 7


def test_no_coarse_block_unless_asked_or_when_the_result_is_already_coarse(monkeypatch):
    session, _calls, _seen = _coarse_session(
        monkeypatch, strict=[_result(3, 0.0)], coarse=[_result(9, 0.4)]
    )
    assert "coarse_candidates" not in session.auto_lock_detect(None)
    session, _calls, _seen = _coarse_session(
        monkeypatch, strict=ValueError("wide"), coarse=[_result(9, 0.4)]
    )
    result = session.auto_lock_detect(None, detector="auto", include_coarse=True)
    assert result["detector"] == "coarse"
    assert "coarse_candidates" not in result


def test_coarse_overrides_reach_the_detector(monkeypatch):
    session, _calls, seen = _coarse_session(monkeypatch, strict=[_result(3, 0.0)], coarse=[])
    session.auto_lock_detect(
        None, include_coarse=True, coarse_min_relative_score=0.1, coarse_max_candidates=16
    )
    assert seen == {"min_relative_score": 0.1, "max_candidates": 16}


def test_a_staged_runs_own_detection_is_untouched_by_include_coarse(monkeypatch):
    session = _make_session()
    strict = [_result(1, 0.25)]

    def _strict(settings, after=None):
        return strict, 0.0, 1.0, 3.0, {"frame_id": 5, "sweep_center_v": 0.0,
                                       "sweep_amplitude_v": 1.0, "n_points": 2048}

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict)
    session.staged_autolock_begin(None, 60.0)
    run = session._staged_autolock
    assert run.latest_detector == "strict"
    trace = np.zeros(2048)
    monkeypatch.setattr(
        session, "_snapshot_auto_lock_traces_with_frame", lambda: (trace, None, 6, 1.0)
    )
    monkeypatch.setattr(session, "_snapshot_sweep_params",
                        lambda require_unlocked=False: (0.0, 1.0, True, 10e6))
    monkeypatch.setattr(
        session_module, "find_plausible_coarse_candidates",
        lambda **kw: [SimpleNamespace(result=_result(1, 0.25)), SimpleNamespace(result=_result(8, -0.4))],
    )
    result = session.auto_lock_candidates_detect({"signal_type": "pdh"}, include_coarse=True)
    assert result["detector"] == "strict"
    assert [c["target_index"] for c in result["candidates"]] == [1]
    assert [c["target_index"] for c in result["coarse_candidates"]] == [1, 8]
    assert result["coarse_frame"]["frame_id"] == 6
    # Never folded into the run: its candidates and detector stay strict-only.
    assert run.latest_detector == "strict"
    assert [c.target_index for c in run.latest_candidates] == [1]
