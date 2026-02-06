from __future__ import annotations

import sys
from pathlib import Path

try:
    import linien_client  # noqa: F401
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[2]
    for sub in ("linien-client", "linien-common", "linien-server"):
        candidate = repo_root / sub
        if candidate.exists():
            sys.path.insert(0, str(candidate))
