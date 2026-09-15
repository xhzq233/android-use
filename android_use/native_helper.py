"""Lazy compilation for source-distributed macOS framework helpers."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path


class NativeHelperError(RuntimeError):
    pass


def native_source_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "native" / name


def _cache_root() -> Path:
    override = os.environ.get("ANDROID_USE_NATIVE_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Caches" / "android-use"


def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    digest.update(source.read_bytes())
    digest.update(platform.machine().encode())
    digest.update(platform.mac_ver()[0].encode())
    return digest.hexdigest()[:16]


def _cached_helper_path(source: Path) -> Path:
    return _cache_root() / f"{source.stem}-{_source_digest(source)}"


def compile_swift_helper(
    source: str | Path,
    *,
    timeout: float = 120.0,
) -> Path:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise NativeHelperError(f"native helper source not found: {source_path}")
    if platform.system() != "Darwin":
        raise NativeHelperError("this optional helper requires macOS frameworks")
    xcrun = shutil.which("xcrun")
    if not xcrun:
        raise NativeHelperError("xcrun is unavailable; install Xcode Command Line Tools")

    cached = _cached_helper_path(source_path)
    if cached.is_file() and os.access(cached, os.X_OK):
        return cached

    cached.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{source_path.stem}-",
        dir=cached.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    command = [
        xcrun,
        "swiftc",
        "-O",
        "-o",
        str(temporary),
        str(source_path),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        temporary.unlink(missing_ok=True)
        raise NativeHelperError(f"native helper compile timed out after {timeout:.1f}s") from exc
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = (completed.stderr or completed.stdout).strip() or "no compiler output"
        raise NativeHelperError(f"native helper compile failed: {detail}")
    temporary.chmod(0o755)
    os.replace(temporary, cached)
    return cached
