"""`linien-web`'s DEFAULT_AUTO_LOCK_SETTINGS mirror the gateway's defaults.

The web UI falls back to these when a field is missing or unparseable, so a
drifted value would silently reset an operator's input to a different default
than the gateway uses. Parsed straight from the TypeScript source (a flat
object literal), not from the built bundle.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

from app.auto_lock_scan import AutoLockScanSettings

_PANEL = (
    Path(__file__).resolve().parents[2]
    / "linien-web" / "src" / "components" / "LockingPanel.tsx"
)


def _web_defaults() -> dict[str, object]:
    source = _PANEL.read_text()
    block = re.search(
        r"const DEFAULT_AUTO_LOCK_SETTINGS: AutoLockScanSettings = \{(.*?)\n\};",
        source, re.S,
    )
    assert block, "DEFAULT_AUTO_LOCK_SETTINGS not found in LockingPanel.tsx"
    values: dict[str, object] = {}
    for key, raw in re.findall(r"^\s*(\w+):\s*(.+?),\s*$", block.group(1), re.M):
        if raw in ("true", "false"):
            values[key] = raw == "true"
        elif raw.startswith("'"):
            values[key] = raw.strip("'")
        else:
            values[key] = float(raw)
    return values


def test_the_web_defaults_are_the_gateway_defaults():
    web, python = _web_defaults(), asdict(AutoLockScanSettings())
    # The UI does not carry every gateway field (the server merges a partial
    # save onto the stored settings), but every one it does carry must agree.
    assert set(web) <= set(python)
    assert web == {key: python[key] for key in web}
    assert "hysteresis_max_extra_tolerance_v" in web
