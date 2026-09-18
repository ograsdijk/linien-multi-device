"""The web panel hand-writes the acceptance defaults; keep them honest.

They are the fallbacks the numeric inputs use when a field is cleared, so drift
would silently commit a different value than the gateway would have chosen. The
engine/schema pair already has a parity test for the same reason; this covers
the third copy, which lives in TypeScript and no Python import can reach.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.lock_acceptance import AcceptanceSettings

PANEL = (
    Path(__file__).resolve().parents[2]
    / "linien-web"
    / "src"
    / "components"
    / "LockAcceptancePanel.tsx"
)


def _parse_web_defaults(source: str) -> dict[str, object]:
    match = re.search(
        r"const DEFAULTS: LockAcceptanceSettings = \{(.*?)\n\};", source, re.DOTALL
    )
    assert match, "DEFAULTS block not found — did the panel get restructured?"
    values: dict[str, object] = {}
    for line in match.group(1).splitlines():
        entry = re.match(r"\s*(\w+):\s*(.+?),\s*$", line)
        if entry:
            values[entry.group(1)] = json.loads(entry.group(2))
    return values


@pytest.mark.skipif(not PANEL.exists(), reason="web sources not present")
def test_web_defaults_match_the_engine():
    web = _parse_web_defaults(PANEL.read_text(encoding="utf-8"))
    engine = {
        name: field.default
        for name, field in AcceptanceSettings.__dataclass_fields__.items()
    }

    assert web.keys() == engine.keys()
    for name, expected in engine.items():
        assert web[name] == expected, f"{name}: web {web[name]!r} != engine {expected!r}"
