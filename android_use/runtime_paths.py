"""Private host-side runtime files, separate from source and installed skills."""

from __future__ import annotations

from pathlib import Path


def runtime_directory(name: str) -> Path:
    root = Path.home() / ".android-use"
    path = root / name
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    path.chmod(0o700)
    return path
