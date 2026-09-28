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
