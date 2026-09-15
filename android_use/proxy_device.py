"""Device-side proxy configuration helpers."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Literal

from .android_device import adb_executable


MITMPROXY_CA_HASH = "c8750f0d"
MITMPROXY_CA_USER_PATH = f"/data/misc/user/0/cacerts-added/{MITMPROXY_CA_HASH}.0"
MITMPROXY_CA_USER_DIR = "/data/misc/user/0/cacerts-added"
MITMPROXY_CA_SYSTEM_PATH = f"/system/etc/security/cacerts/{MITMPROXY_CA_HASH}.0"
MITMPROXY_CA_SDCARD_PATH = f"/sdcard/{MITMPROXY_CA_HASH}.0"

CertCheckResult = Literal["installed", "missing", "unknown"]


def set_device_proxy(serial: str, host: str, port: int) -> None:
    proxy_str = f"{host}:{port}"
    _adb_shell(serial, ["settings", "put", "global", "http_proxy", proxy_str])


def clear_device_proxy(serial: str) -> None:
    _adb_shell(serial, ["settings", "put", "global", "http_proxy", ":0"])


def restore_device_proxy(serial: str, proxy: str | None) -> None:
    value = proxy.strip() if proxy else ":0"
    if value.lower() in {"", "null", "none"}:
        value = ":0"
    _adb_shell(serial, ["settings", "put", "global", "http_proxy", value])


def get_device_proxy(serial: str) -> str:
    result = _adb_shell(serial, ["settings", "get", "global", "http_proxy"])
    return result.strip() if result else ""


def setup_reverse(serial: str, device_port: int, host_port: int) -> None:
    _adb(serial, ["reverse", f"tcp:{device_port}", f"tcp:{host_port}"])


def remove_reverse(serial: str, device_port: int) -> None:
    try:
        _adb(serial, ["reverse", "--remove", f"tcp:{device_port}"])
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if "not found" not in output.lower() and "closed" not in output.lower():
            raise


def check_ca_cert(serial: str) -> CertCheckResult:
    for path in (MITMPROXY_CA_USER_PATH, MITMPROXY_CA_SYSTEM_PATH):
        try:
            _adb_shell(serial, ["test", "-f", path])
            return "installed"
        except subprocess.CalledProcessError:
            continue
    try:
        _adb_shell(serial, ["ls", "-ld", MITMPROXY_CA_USER_DIR])
    except subprocess.CalledProcessError as exc:
        if "Permission denied" in ((exc.stdout or "") + (exc.stderr or "")):
            return "unknown"
    return "missing"


def get_ca_cert_path() -> Path | None:
    ca_dir = Path.home() / ".mitmproxy"
    for name in ("mitmproxy-ca-cert.pem", "mitmproxy-ca-cert.cer"):
        cert_path = ca_dir / name
        if cert_path.exists():
            return cert_path
    return None


def require_ca_cert_path(path: str | None = None) -> Path:
    cert_path = Path(path).expanduser() if path else get_ca_cert_path()
    if cert_path is None:
        raise FileNotFoundError(
            "mitmproxy CA certificate not found. Run 'mitmdump' once, "
            "or pass --cert /path/to/mitmproxy-ca-cert.pem."
        )
    if not cert_path.exists():
        raise FileNotFoundError(f"CA certificate not found: {cert_path}")
    return cert_path


def push_ca_cert(serial: str, path: str | None = None, device_path: str = MITMPROXY_CA_SDCARD_PATH) -> tuple[Path, str]:
    cert_path = require_ca_cert_path(path)
    _adb(serial, ["push", str(cert_path), device_path])
    return cert_path, device_path


def install_ca_cert_root(
    serial: str,
    path: str | None = None,
    device_path: str = MITMPROXY_CA_SDCARD_PATH,
    reboot: bool = False,
) -> tuple[Path, str]:
    cert_path, pushed_path = push_ca_cert(serial, path, device_path)
    install_cmd = (
        f"mkdir -p {MITMPROXY_CA_USER_DIR} && "
        f"cp {shlex.quote(pushed_path)} {MITMPROXY_CA_USER_PATH} && "
        f"chmod 644 {MITMPROXY_CA_USER_PATH}"
    )
    _adb_shell(serial, ["su", "-c", install_cmd])
    if reboot:
        _adb(serial, ["reboot"], timeout=5.0)
    return cert_path, MITMPROXY_CA_USER_PATH


def install_ca_cert_instructions(serial: str) -> str:
    cert_path = get_ca_cert_path()
    cert_path_str = str(cert_path) if cert_path else "~/.mitmproxy/mitmproxy-ca-cert.cer"
    return (
        f"CA certificate not found on device.\n"
        f"To install:\n"
        f"  1. Ensure mitmproxy CA cert exists: {cert_path_str}\n"
        f"     (run 'mitmdump' once to generate it if missing)\n"
        f"  2. Push cert to device:\n"
        f"     adb -s {serial} push {cert_path_str} {MITMPROXY_CA_SDCARD_PATH}\n"
        f"  3. Install cert (requires root or user approval):\n"
        f"     adb -s {serial} shell\n"
        f"     su\n"
        f"     cp {MITMPROXY_CA_SDCARD_PATH} {MITMPROXY_CA_USER_PATH}\n"
        f"     chmod 644 {MITMPROXY_CA_USER_PATH}\n"
        f"     reboot\n"
        f"  Or use the device Settings > Security > Install from storage (Android < 7)\n"
        f"  Or Settings > Security > Encryption & credentials > Install a certificate (Android 7+)\n"
    )


def _adb_shell(serial: str, cmd: list[str], timeout: float = 10.0) -> str:
    return _adb(serial, ["shell", shlex.join(cmd)], timeout=timeout)


def _adb(serial: str, cmd: list[str], timeout: float = 10.0) -> str:
    result = subprocess.run(
        [adb_executable(), "-s", serial, *cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout
