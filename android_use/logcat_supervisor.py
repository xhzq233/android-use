"""Persistent host-side supervision for one adb logcat process per device."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .android_device import adb_executable

_DEVICE_TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+\d+\s+\d+\s+[VDIWEF]\s"
)
_FLOW_WARNING_PATTERNS = (
    "logd: reader",
    "chatty",
    "dropped",
    "unexpected EOF",
    "device offline",
    "no devices/emulators found",
)


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        return False  # os.kill(pid, 0) is not a liveness probe on Windows.
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def is_adb_logcat_process(pid: int, expected_executable: str | None = None) -> bool:
    if pid <= 0:
        return False
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0 or not completed.stdout.strip():
        return False
    try:
        arguments = shlex.split(completed.stdout.strip())
    except ValueError:
        return False
    if not arguments:
        return False
    expected_name = Path(expected_executable or adb_executable()).name
    executable_matches = Path(arguments[0]).name == expected_name or (
        expected_executable is not None
        and str(Path(expected_executable).resolve())
        in (str(Path(item).resolve()) for item in arguments[1:] if item.startswith("/"))
    )
    return executable_matches and "logcat" in arguments[1:]


def read_state(path: str | Path) -> dict[str, Any] | None:
    state_path = Path(path)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid logcat state file: {state_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid logcat state file: {state_path}: root must be an object")
    return payload


def write_state(path: str | Path, payload: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{state_path.name}.",
        suffix=".tmp",
        dir=state_path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, state_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _tail_bytes(path: Path, limit: int = 131072) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read()
    except FileNotFoundError:
        return b""


def collector_status(state_path: str | Path) -> dict[str, Any]:
    state = read_state(state_path)
    if state is None:
        return {"status": "missing", "healthy": False}
    log_path = Path(str(state.get("logFile") or ""))
    supervisor_pid = int(state.get("supervisorPid") or 0)
    adb_pid = int(state.get("adbPid") or 0)
    supervisor_alive = process_alive(supervisor_pid)
    adb_alive = process_alive(adb_pid)
    try:
        stat = log_path.stat()
        bytes_written = stat.st_size
        modified_at = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z",
            time.localtime(stat.st_mtime),
        )
    except (FileNotFoundError, OSError):
        bytes_written = 0
        modified_at = None

    tail = _tail_bytes(log_path).decode("utf-8", errors="replace")
    last_device_timestamp = None
    warnings: list[str] = []
    for line in tail.splitlines():
        match = _DEVICE_TIMESTAMP_RE.match(line)
        if match:
            last_device_timestamp = match.group("timestamp")
        lower = line.lower()
        if any(pattern.lower() in lower for pattern in _FLOW_WARNING_PATTERNS):
            warnings.append(line[-300:])
    state_status = str(state.get("status") or "unknown")
    healthy = state_status == "running" and supervisor_alive and adb_alive
    payload = {
        **state,
        "healthy": healthy,
        "supervisorAlive": supervisor_alive,
        "adbAlive": adb_alive,
        "bytes": bytes_written,
        "modifiedAt": modified_at,
        "lastDeviceTimestamp": last_device_timestamp,
        "warnings": warnings[-5:],
    }
    if state_status == "starting" and supervisor_alive:
        payload["status"] = "starting"
    elif state_status in ("starting", "running") and not healthy:
        payload["status"] = "unhealthy"
    return payload


class Supervisor:
    def __init__(self, serial: str, log_file: Path, state_file: Path, package: str | None):
        self.serial = serial
        self.log_file = log_file
        self.state_file = state_file
        self.package = package
        self.child: subprocess.Popen[bytes] | None = None
        self.stop_requested = False

    def _signal(self, signum, _frame) -> None:
        self.stop_requested = True
        if self.child and self.child.poll() is None:
            try:
                self.child.terminate()
            except ProcessLookupError:
                pass

    def _state(self, status: str, **extra: Any) -> dict[str, Any]:
        current = read_state(self.state_file) or {}
        return {
            **current,
            "schemaVersion": 1,
            "serial": self.serial,
            "package": self.package,
            "logFile": str(self.log_file),
            "supervisorPid": os.getpid(),
            "adbPid": self.child.pid if self.child else None,
            "adbExecutable": adb_executable(),
            "status": status,
            **extra,
        }

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        started_at = utc_timestamp()
        with self.log_file.open("ab", buffering=0) as output:
            command = [
                adb_executable(),
                "-s",
                self.serial,
                "logcat",
                "-v",
                "threadtime",
                "-T",
                "1",
            ]
            try:
                self.child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                write_state(
                    self.state_file,
                    self._state(
                        "failed",
                        startedAt=started_at,
                        endedAt=utc_timestamp(),
                        exitCode=127,
                        error=str(exc),
                    ),
                )
                return 127
            write_state(
                self.state_file,
                self._state(
                    "running",
                    startedAt=started_at,
                    endedAt=None,
                    exitCode=None,
                    error=None,
                ),
            )
            while True:
                try:
                    _, stderr = self.child.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if not self.stop_requested:
                        continue
                    self.child.kill()
                    _, stderr = self.child.communicate(timeout=2.0)
                    break
            returncode = int(self.child.returncode or 0)

        status = "stopped" if self.stop_requested else "exited"
        error = stderr.decode("utf-8", errors="replace").strip() or None
        write_state(
            self.state_file,
            self._state(
                status,
                startedAt=started_at,
                endedAt=utc_timestamp(),
                exitCode=returncode,
                error=error,
            ),
        )
        return returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--package")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return Supervisor(
        serial=args.serial,
        log_file=Path(args.log_file),
        state_file=Path(args.state_file),
        package=args.package,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
