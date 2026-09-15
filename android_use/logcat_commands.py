"""User-facing logcat lifecycle, health, and action marker helpers."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .android_device import run_adb, run_adb_shell
from .runtime_paths import runtime_directory
from .logcat_supervisor import (
    collector_status,
    is_adb_logcat_process,
    process_alive,
    read_state,
    utc_timestamp,
    write_state,
)


def _safe_component(value: str | None, fallback: str) -> str:
    raw = (value or "").strip() or fallback
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return safe or fallback


def state_dir() -> Path:
    override = os.environ.get("ANDROID_USE_LOGCAT_STATE_DIR")
    return Path(override).expanduser() if override else runtime_directory("logcat")


def state_path(serial: str) -> Path:
    return state_dir() / f"{_safe_component(serial, 'device')}.json"


def timeline_path(serial: str) -> Path:
    return state_dir() / f"{_safe_component(serial, 'device')}.timeline.jsonl"


def output_path(package: str | None) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    override = os.environ.get("ANDROID_USE_LOGCAT_OUTPUT_DIR")
    root = Path(override).expanduser() if override else runtime_directory("logs")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"au-{_safe_component(package, 'all')}-{timestamp}.log"


def active_status(serial: str) -> dict[str, Any] | None:
    if os.name == "nt":
        return None
    payload = collector_status(state_path(serial))
    return payload if payload.get("healthy") else None


def _append_timeline(serial: str, event: dict[str, Any]) -> None:
    path = timeline_path(serial)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _emit_android_marker(serial: str, message: str) -> None:
    run_adb_shell(
        serial,
        ["log", "-t", "android-use", message],
        timeout=2.0,
        check=False,
    )


@contextmanager
def action_marker(serial: str | None, operation: str) -> Iterator[None]:
    if not serial:
        yield
        return
    status = active_status(serial)
    if status is None:
        yield
        return
    marker_id = f"{int(time.time() * 1000):x}-{os.getpid():x}"
    safe_operation = _safe_component(operation, "command")
    started = {
        "timestamp": utc_timestamp(),
        "id": marker_id,
        "operation": safe_operation,
        "phase": "start",
    }
    _append_timeline(serial, started)
    _emit_android_marker(serial, f"id={marker_id} operation={safe_operation} phase=start")
    result = "ok"
    try:
        yield
    except BaseException:
        result = "error"
        raise
    finally:
        ended = {
            "timestamp": utc_timestamp(),
            "id": marker_id,
            "operation": safe_operation,
            "phase": "end",
            "result": result,
        }
        _append_timeline(serial, ended)
        _emit_android_marker(
            serial,
            f"id={marker_id} operation={safe_operation} phase=end result={result}",
        )


def start_logcat(
    serial: str,
    *,
    package: str | None,
    clear: bool,
    timeout: float = 3.0,
) -> dict[str, Any]:
    if os.name == "nt":
        raise RuntimeError("persistent logcat supervision requires POSIX; use raw 'adb logcat' on Windows")
    path = state_path(serial)
    existing = collector_status(path)
    if existing.get("healthy"):
        return {**existing, "reused": True}

    run_adb(["get-state"], serial=serial, timeout=5.0, check=True)
    if clear:
        run_adb(["logcat", "-c"], serial=serial, timeout=10.0, check=True)

    log_file = output_path(package)
    timeline_path(serial).unlink(missing_ok=True)
    initial = {
        "schemaVersion": 1,
        "serial": serial,
        "package": package,
        "logFile": str(log_file),
        "stateFile": str(path),
        "timelineFile": str(timeline_path(serial)),
        "status": "starting",
        "startedAt": utc_timestamp(),
        "endedAt": None,
        "supervisorPid": None,
        "adbPid": None,
        "clear": clear,
    }
    write_state(path, initial)

    package_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{package_root}{os.pathsep}{current_pythonpath}"
        if current_pythonpath
        else package_root
    )
    command = [
        sys.executable,
        "-m",
        "android_use.logcat_supervisor",
        "--serial",
        serial,
        "--log-file",
        str(log_file),
        "--state-file",
        str(path),
    ]
    if package:
        command.extend(["--package", package])
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    current = read_state(path) or initial
    if current.get("status") == "starting":
        current["supervisorPid"] = process.pid
        write_state(path, current)

    deadline = time.monotonic() + timeout
    last = collector_status(path)
    while time.monotonic() < deadline:
        last = collector_status(path)
        if last.get("healthy"):
            return {**last, "reused": False}
        if last.get("status") in ("failed", "exited", "unhealthy"):
            break
        time.sleep(0.05)
    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    raise RuntimeError(
        "logcat collector did not become healthy: "
        f"status={last.get('status')} error={last.get('error') or '-'}"
    )


def stop_logcat(serial: str, timeout: float = 5.0) -> dict[str, Any]:
    if os.name == "nt":
        raise RuntimeError("persistent logcat supervision requires POSIX; stop the raw ADB process you started")
    path = state_path(serial)
    state = read_state(path)
    if state is None:
        return {"status": "missing", "healthy": False, "serial": serial}
    supervisor_pid = int(state.get("supervisorPid") or 0)
    adb_pid = int(state.get("adbPid") or 0)
    if state.get("status") not in ("starting", "running", "unhealthy"):
        return collector_status(path)
    forced_cleanup = False
    if process_alive(supervisor_pid):
        try:
            os.killpg(supervisor_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(supervisor_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process_alive(supervisor_pid):
            time.sleep(0.05)
    if process_alive(supervisor_pid):
        forced_cleanup = True
        try:
            os.killpg(supervisor_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(supervisor_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and process_alive(supervisor_pid):
            time.sleep(0.05)

    expected_adb = str(state.get("adbExecutable") or "")
    if process_alive(adb_pid) and is_adb_logcat_process(adb_pid, expected_adb or None):
        forced_cleanup = True
        try:
            os.kill(adb_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and process_alive(adb_pid):
            time.sleep(0.05)
        if process_alive(adb_pid):
            try:
                os.kill(adb_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    if not process_alive(supervisor_pid) and not process_alive(adb_pid):
        current = read_state(path) or state
        if current.get("status") in ("starting", "running", "unhealthy"):
            current.update(
                {
                    "status": "stopped",
                    "endedAt": utc_timestamp(),
                    "forcedCleanup": forced_cleanup,
                }
            )
            write_state(path, current)

    result = collector_status(path)
    if process_alive(supervisor_pid):
        result["status"] = "unhealthy"
        result["healthy"] = False
        result["error"] = f"supervisor did not stop within {timeout:.1f}s"
    elif process_alive(adb_pid):
        result["status"] = "unhealthy"
        result["healthy"] = False
        result["error"] = "adb logcat child is still running"
    return result


def log_offset(serial: str) -> tuple[Path, int, dict[str, Any]]:
    status = active_status(serial)
    if status is None:
        raise RuntimeError("no healthy active logcat collector; run 'android-use logcat start' first")
    path = Path(str(status["logFile"]))
    try:
        offset = path.stat().st_size
    except FileNotFoundError:
        offset = 0
    return path, offset, status


def read_log_since(path: Path, offset: int) -> tuple[str, int]:
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
        next_offset = handle.tell()
    return data.decode("utf-8", errors="replace"), next_offset


def format_status(payload: dict[str, Any]) -> str:
    if payload.get("status") == "missing":
        return "logcat status=missing"
    return (
        f"logcat status={payload.get('status')} healthy={str(bool(payload.get('healthy'))).lower()} "
        f"file={payload.get('logFile') or '-'} bytes={payload.get('bytes', 0)} "
        f"supervisor={payload.get('supervisorPid') or '-'} adb={payload.get('adbPid') or '-'} "
        f"lastDeviceTimestamp={payload.get('lastDeviceTimestamp') or '-'}"
    )
