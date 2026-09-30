"""The committed contract fixtures are what the gateway produces today.

``tests/data/contract/*.json`` are real responses (see ``contract_fixtures.py``)
that the orchestrator copies into its own tests. This regenerates them from the
production code and compares, so a change to a response shape or to what the
detectors/planner return fails here until the fixtures are refreshed
(``PYTHONPATH=$PWD python tests/contract_fixtures.py`` from ``linien-gateway/``).

Floats are compared to a relative 1e-6, not bit-for-bit, so a last-bit
difference in numpy between platforms does not fail the test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import contract_fixtures
from app import schemas


def _assert_same(actual: Any, expected: Any, path: str = "$") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and actual.keys() == expected.keys(), path
        for key in expected:
            _assert_same(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for i, (a, e) in enumerate(zip(actual, expected)):
            _assert_same(a, e, f"{path}[{i}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-6, abs=1e-12), path
    else:
        assert actual == expected, path


@pytest.fixture(scope="module")
def generated() -> dict[str, Any]:
    return contract_fixtures.generate()


def test_the_committed_fixtures_are_exactly_the_generated_set(generated):
    committed = {p.stem for p in contract_fixtures.CONTRACT_DIR.glob("*.json")}
    assert committed == set(generated)


def test_the_committed_fixtures_match_what_the_gateway_produces(generated):
    for name, body in generated.items():
        path = contract_fixtures.CONTRACT_DIR / f"{name}.json"
        _assert_same(json.loads(path.read_text()), body, name)
        # Text form too, so `contract_fixtures.py` regenerates the same files.
        assert contract_fixtures.render(json.loads(path.read_text())) == path.read_text(), name


def test_the_fixtures_cover_the_documented_contract(generated):
    for name in ("staged_begin", "staged_step_narrow", "staged_step_narrow_second",
                 "staged_step_done"):
        schemas.StagedAutolockHysteresis.model_validate(generated[name]["hysteresis"])
    lock = generated["staged_lock"]
    assert lock["hysteresis"]["selection_window"]["extra_tolerance_requested_v"] > 0
    refusal = generated["staged_step_refused_422"]["detail"]
    assert "hysteresis window" in refusal["message"]
    assert refusal["hysteresis_window"]["applied_window_v"] > 0
    trace = generated["auto_lock_candidates_include_coarse_and_trace"]
    assert trace["coarse_candidates"] and len(trace["trace"]["combined_error"]) == trace["frame"]["n_points"]
    # Nothing time- or run-dependent survives in the files.
    assert generated["staged_begin"]["token"] == "TOKEN"
    assert generated["staged_begin"]["frame"]["acquired_at"] == 0.0
