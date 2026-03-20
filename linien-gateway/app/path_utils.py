from __future__ import annotations

from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path | None:
    start_path = (start or Path(__file__)).resolve()
    for parent in start_path.parents:
        if (parent / "config.json").exists() and (parent / "linien-web").exists():
            return parent

    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "config.json").exists() and (parent / "linien-web").exists():
            return parent
    return None


def resolve_repo_path(relative_path: str, fallback_base: Path) -> Path:
    repo_root = find_repo_root()
    if repo_root is not None:
        return repo_root / relative_path
    return fallback_base / relative_path
