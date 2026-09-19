"""ADB transport, device selection, and stock Android inspection helpers."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class AdbDeviceInfo:
    serial: str
    status: str
    detail: str

    @property
    def transport(self) -> str:
        if "usb:" in self.detail:
            return "usb"
        if self.serial.startswith("emulator-"):
            return "emulator"
        if ":" in self.serial:
            return "network"
        return "unknown"


@dataclass(frozen=True)
class DisplayMetrics:
    physical_width: int
    physical_height: int
    input_width: int
    input_height: int
    rotation: int | None


class AdbCommandError(RuntimeError):
    def __init__(
        self,
        command: list[str],
        returncode: int,
        stdout: str | bytes,
        stderr: str | bytes,
    ):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        stderr_text = _decode(stderr)
        stdout_text = _decode(stdout)
        message = stderr_text.strip() or stdout_text.strip() or f"exit code {returncode}"
        super().__init__(f"{' '.join(command)} failed: {message}")


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def adb_executable() -> str:
    return os.environ.get("ANDROID_USE_ADB") or shutil.which("adb") or "adb"


def _adb_command(args: list[str], serial: str | None) -> list[str]:
    command = [adb_executable()]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    return command


def run_adb(
    args: list[str],
    *,
    serial: str | None = None,
    timeout: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = _adb_command(args, serial)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AdbCommandError(command, 127, "", f"adb executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbCommandError(
            command,
            124,
            _decode(exc.stdout),
            _decode(exc.stderr) or f"timed out after {timeout:.1f}s",
        ) from exc
    if check and result.returncode != 0:
        raise AdbCommandError(command, result.returncode, result.stdout, result.stderr)
    return result


def run_adb_bytes(
    args: list[str],
    *,
    serial: str | None = None,
    timeout: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    command = _adb_command(args, serial)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AdbCommandError(command, 127, b"", f"adb executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbCommandError(
            command,
            124,
            exc.stdout or b"",
            exc.stderr or f"timed out after {timeout:.1f}s".encode(),
        ) from exc
    if check and result.returncode != 0:
        raise AdbCommandError(command, result.returncode, result.stdout, result.stderr)
    return result


def spawn_adb(
    args: list[str],
    *,
    serial: str | None = None,
    stdout=None,
    stderr=None,
    start_new_session: bool = False,
) -> subprocess.Popen[bytes]:
    command = _adb_command(args, serial)
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )
    except FileNotFoundError as exc:
        raise AdbCommandError(command, 127, b"", f"adb executable not found: {command[0]}") from exc


def run_adb_shell(
    serial: str,
    command: list[str],
    *,
    timeout: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    # Legacy/remote ADB transports can return 0 even when the device command fails.
    # Keep argv quoting and report the device status independently of the transport.
    marker = "\nANDROID_USE_EXIT_STATUS="
    script = shlex.join(command) + '; printf "\\nANDROID_USE_EXIT_STATUS=%s\\n" "$?"'
    result = run_adb(
        ["shell", shlex.join(["sh", "-c", script])],
        serial=serial, timeout=timeout, check=False,
    )
    if result.returncode == 0:
        output, separator, status = result.stdout.rpartition(marker)
        if separator and status.strip().isdigit():
            result.stdout = output
            result.returncode = int(status.strip())
        else:
            result.returncode = 1
            result.stderr += "\nADB shell did not return the device exit status"
    if check and result.returncode != 0:
        raise AdbCommandError(result.args, result.returncode, result.stdout, result.stderr)
    return result


def list_adb_devices() -> list[AdbDeviceInfo]:
    result = run_adb(["devices", "-l"], timeout=20.0, check=True)
    devices: list[AdbDeviceInfo] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        devices.append(
            AdbDeviceInfo(
                serial=parts[0],
                status=parts[1],
                detail=parts[2] if len(parts) > 2 else "",
            )
        )
    return devices


def adb_version_text() -> str:
    result = run_adb(["version"], timeout=20.0, check=False)
    return result.stdout.strip() or result.stderr.strip()


def connected_devices() -> list[AdbDeviceInfo]:
    return [device for device in list_adb_devices() if device.status == "device"]


def connected_serials() -> list[str]:
    return [device.serial for device in connected_devices()]


def resolve_device(serial: str | None) -> AdbDeviceInfo:
    requested = serial or os.environ.get("ANDROID_USE_SERIAL")
    devices = list_adb_devices()
    if requested:
        matches = [device for device in devices if device.serial == requested]
        if not matches:
            raise SystemExit(f"Android device is not connected: {requested}")
        device = matches[0]
        if device.status != "device":
            raise SystemExit(f"Android device is not online: {requested} status={device.status}")
        return device

    online = [device for device in devices if device.status == "device"]
    if not online:
        other = [
            f"{device.serial}({device.status},{device.transport})"
            for device in devices
        ]
        suffix = f" Found: {', '.join(other)}." if other else ""
        raise SystemExit(f"No online Android device found.{suffix}")
    if len(online) > 1:
        joined = ", ".join(device.serial for device in online)
        raise SystemExit(f"Multiple Android devices found: {joined}. Pass -s SERIAL.")
    return online[0]


def resolve_serial(serial: str | None) -> str:
    return resolve_device(serial).serial


_SIZE_RE = re.compile(r"^(Physical|Override) size:\s*(\d+)x(\d+)\s*$", re.MULTILINE)
_ROTATION_PATTERNS = (
    re.compile(r"\bmCurrentRotation=(\d+)"),
    re.compile(r"\brotation=(\d+)"),
    re.compile(r"\bmRotation=(?:ROTATION_)?(\d+)"),
)


def display_metrics(serial: str, timeout: float = 5.0) -> DisplayMetrics:
    size_result = run_adb_shell(serial, ["wm", "size"], timeout=timeout, check=True)
    sizes = {
        kind.lower(): (int(width), int(height))
        for kind, width, height in _SIZE_RE.findall(size_result.stdout)
    }
    physical = sizes.get("physical")
    if physical is None:
        raise RuntimeError(f"cannot parse display size from: {size_result.stdout.strip()!r}")
    input_size = sizes.get("override", physical)

    rotation: int | None = None
    rotation_result = run_adb_shell(
        serial,
        ["dumpsys", "input"],
        timeout=timeout,
        check=False,
    )
    for pattern in _ROTATION_PATTERNS:
        match = pattern.search(rotation_result.stdout)
        if match:
            rotation = int(match.group(1))
            break
    return DisplayMetrics(
        physical_width=physical[0],
        physical_height=physical[1],
        input_width=input_size[0],
        input_height=input_size[1],
        rotation=rotation,
    )


def _stock_dump_xml(serial: str, compressed: bool = True, timeout: float = 10.0) -> str:
    command = ["exec-out", "uiautomator", "dump"]
    if compressed:
        command.append("--compressed")
    command.append("/dev/tty")
    result = run_adb(command, serial=serial, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if result.returncode in (137, -9):
            detail = (
                detail + " " if detail else ""
            ) + "another UiAutomation session may be active"
        raise AdbCommandError(
            _adb_command(command, serial),
            result.returncode,
            result.stdout,
            detail,
        )
    start = result.stdout.find("<?xml")
    end = result.stdout.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise RuntimeError(
            "stock uiautomator did not return an XML hierarchy; "
            f"output={result.stdout[-300:].strip()!r}"
        )
    return result.stdout[start : end + len("</hierarchy>")]


def dump_xml(serial: str, compressed: bool = True, timeout: float = 10.0) -> str:
    if os.environ.get("ANDROID_USE_STOCK_UIAUTOMATOR") == "1":
        from .persistent_ui import stop_server

        if not stop_server(serial):
            raise RuntimeError(
                "cannot confirm the persistent UiAutomation server stopped; "
                "stock dump was not started"
            )
        return _stock_dump_xml(serial, compressed=compressed, timeout=timeout)

    from .persistent_ui import dump_xml as persistent_dump_xml, stop_server

    deadline = time.monotonic() + max(0.5, timeout)
    persistent_budget = max(0.5, min(6.0, timeout - 3.5))
    try:
        return persistent_dump_xml(
            serial,
            compressed=compressed,
            timeout=persistent_budget,
        )
    except Exception as persistent_error:
        remaining = deadline - time.monotonic()
        released = (
            remaining > 0.2
            and stop_server(
                serial,
                timeout=min(0.75, remaining),
                hard=True,
            )
        )
        if not released:
            raise RuntimeError(
                "persistent UiAutomation failed and its process could not be "
                f"confirmed stopped; stock dump was not started: {persistent_error}"
            ) from persistent_error
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            raise RuntimeError(
                "persistent UiAutomation failed and exhausted the dump timeout: "
                f"{persistent_error}"
            ) from persistent_error
        try:
            return _stock_dump_xml(
                serial,
                compressed=compressed,
                timeout=remaining,
            )
        except Exception as stock_error:
            raise RuntimeError(
                f"persistent UiAutomation failed: {persistent_error}; "
                f"stock uiautomator failed: {stock_error}"
            ) from stock_error


def parse_xml(xml: str) -> ET.Element:
    return ET.fromstring(xml.encode("utf-8"))


def dump_root(
    serial: str,
    compressed: bool = True,
    timeout: float = 10.0,
) -> tuple[str, ET.Element]:
    xml = dump_xml(serial, compressed=compressed, timeout=timeout)
    return xml, parse_xml(xml)
