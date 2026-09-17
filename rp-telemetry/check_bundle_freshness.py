#!/usr/bin/env python3
"""Check that the committed armv7 binary matches the daemon source.

The gateway decides a board is up to date by comparing the version the board
reports against ``BUNDLED_VERSION``. Nothing at runtime notices when the
committed binary is older than that constant: installing deploys the stale
daemon, the board keeps reporting the old version, and the UI shows an
"update available" banner that reinstalling never clears. That happened once
already, between 1.2.0 and 1.3.0.

Run from anywhere:

    python3 rp-telemetry/check_bundle_freshness.py

Exits non-zero and explains what to do when something is out of step. Stdlib
only and no imports from ``app``, so CI can run it in seconds without
installing the gateway's dependencies.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
C_SOURCE = REPO_ROOT / "rp-telemetry" / "src" / "rp_telemetry.c"
STAMP = REPO_ROOT / "rp-telemetry" / "build-stamp.txt"
BINARY = REPO_ROOT / "linien-gateway" / "app" / "assets" / "rp-telemetry-armv7"
GATEWAY_MODULE = REPO_ROOT / "linien-gateway" / "app" / "rp_telemetry.py"

REBUILD = (
    "Run rp-telemetry/build-arm.sh, then commit both "
    "linien-gateway/app/assets/rp-telemetry-armv7 and "
    "rp-telemetry/build-stamp.txt."
)


def _read_stamp() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in STAMP.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def problems() -> list[str]:
    """Return a list of human-readable problems; empty means all good."""
    found: list[str] = []

    for path in (C_SOURCE, STAMP, BINARY, GATEWAY_MODULE):
        if not path.exists():
            found.append(f"missing: {path.relative_to(REPO_ROOT)}")
    if found:
        return found

    stamp = _read_stamp()

    # 1. The binary was built from the source that is checked in now. This is
    #    the check the version string cannot make: editing the daemon without
    #    bumping RPT_VERSION leaves a stale binary looking correct.
    current_hash = hashlib.sha256(C_SOURCE.read_bytes()).hexdigest()
    if stamp.get("source_sha256") != current_hash:
        found.append(
            "rp_telemetry.c has changed since the bundled binary was built "
            f"(source is {current_hash[:12]}..., stamp says "
            f"{str(stamp.get('source_sha256'))[:12]}...). " + REBUILD
        )

    # 2. The three places a version lives all agree.
    c_match = re.search(
        r'#define RPT_VERSION "([^"]+)"', C_SOURCE.read_text(encoding="utf-8")
    )
    gw_match = re.search(
        r'^BUNDLED_VERSION\s*=\s*"([^"]+)"',
        GATEWAY_MODULE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    c_version = c_match.group(1) if c_match else None
    gw_version = gw_match.group(1) if gw_match else None

    if c_version is None:
        found.append("could not find RPT_VERSION in rp_telemetry.c")
    if gw_version is None:
        found.append("could not find BUNDLED_VERSION in app/rp_telemetry.py")
    if c_version and gw_version and c_version != gw_version:
        found.append(
            f"RPT_VERSION is {c_version} but BUNDLED_VERSION is {gw_version}; "
            "they must match."
        )
    if c_version and stamp.get("version") != c_version:
        found.append(
            f"build-stamp.txt says {stamp.get('version')} but RPT_VERSION is "
            f"{c_version}. " + REBUILD
        )

    # 3. The bundled file really is that version, and really is an ARM binary,
    #    so a fresh install does not answer with "Exec format error".
    binary = BINARY.read_bytes()
    if c_version and c_version.encode() not in binary:
        found.append(
            f"app/assets/rp-telemetry-armv7 does not contain {c_version}. "
            + REBUILD
        )
    if binary[:4] != b"\x7fELF":
        found.append("app/assets/rp-telemetry-armv7 is not an ELF binary.")
    elif int.from_bytes(binary[18:20], "little") != 40:  # EM_ARM
        found.append("app/assets/rp-telemetry-armv7 is not an ARM binary.")

    return found


def main() -> int:
    found = problems()
    if not found:
        stamp = _read_stamp()
        print(
            f"OK: bundled rp-telemetry {stamp.get('version')} was built from "
            f"the committed source (backend: {stamp.get('backend', '?')})."
        )
        return 0
    print("The bundled rp-telemetry binary is out of step:\n", file=sys.stderr)
    for problem in found:
        print(f"  - {problem}", file=sys.stderr)
    print("", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
