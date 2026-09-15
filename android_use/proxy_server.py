"""HTTP/HTTPS proxy server management using mitmproxy."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_constants import DEFAULT_PROXY_PORT, DEFAULT_PROXY_TIMEOUT, DEFAULT_PROXY_HOST
from .android_device import resolve_serial
from .runtime_paths import runtime_directory
from .proxy_device import (
    MITMPROXY_CA_SYSTEM_PATH,
    MITMPROXY_CA_USER_PATH,
    check_ca_cert,
    clear_device_proxy,
    get_ca_cert_path,
    get_device_proxy,
    install_ca_cert_root,
    install_ca_cert_instructions,
    push_ca_cert,
    remove_reverse,
    restore_device_proxy,
    set_device_proxy,
    setup_reverse,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProxyConfig:
    port: int = DEFAULT_PROXY_PORT
    host: str = DEFAULT_PROXY_HOST
    mock_rules_path: str | None = None
    capture: bool = True
    mitmdump_path: str = "mitmdump"
    previous_device_proxy: str | None = None
    device_proxy_configured: bool = False
    use_reverse: bool = True


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


@dataclass
class MockRule:
    url: str
    status: int = 200
    body: str = ""


# ---------------------------------------------------------------------------
# Runtime path helpers
# ---------------------------------------------------------------------------

def _proxy_safe_serial(serial: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", serial)


def _proxy_base_path(serial: str) -> Path:
    override = os.environ.get("ANDROID_USE_PROXY_DIR")
    root = Path(override).expanduser() if override else runtime_directory("proxy")
    directory = root / _proxy_safe_serial(serial)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory / "session"


def proxy_socket_path(serial: str) -> Path:
    override = os.environ.get("ANDROID_USE_PROXY_SOCKET")
    if override:
        return Path(override)
    return _proxy_base_path(serial).with_suffix(".sock")


def proxy_pid_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".pid")


def proxy_log_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".log")


def proxy_flows_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".flows.json")


def proxy_capture_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".flows.ndjson")


def proxy_count_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".flows.count")


def proxy_mock_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".mocks.json")


def proxy_addon_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".addon.py")


def proxy_ready_path(serial: str) -> Path:
    return _proxy_base_path(serial).with_suffix(".ready")


# ---------------------------------------------------------------------------
# Socket communication
# ---------------------------------------------------------------------------

def _json_socket_send(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    sock.sendall(data)


def _json_socket_recv(sock: socket.socket) -> dict[str, Any]:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("empty proxy response")
    return json.loads(raw.decode("utf-8"))


def _proxy_request(serial: str, payload: dict[str, Any], timeout: float = DEFAULT_PROXY_TIMEOUT) -> dict[str, Any]:
    sock_path = proxy_socket_path(serial)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            _json_socket_send(sock, payload)
            return _json_socket_recv(sock)
    except FileNotFoundError:
        raise RuntimeError(
            f"android-use proxy is not running for {serial}; run: android-use proxy start -s {serial}"
        )
    except ConnectionRefusedError:
        raise RuntimeError(
            f"android-use proxy socket is stale for {serial}: {sock_path}"
        )
    except TimeoutError:
        raise RuntimeError(
            f"android-use proxy request timed out for {serial} after {timeout:.1f}s"
        )


def _proxy_ping(serial: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        return _proxy_request(serial, {"kind": "ping"}, timeout=timeout)
    except Exception:
        return None


def _flows_to_text(flows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for f in flows:
        req = f["request"]
        res = f.get("response", {})
        method = req["method"]
        status = res.get("status", "-")
        url = req["url"]
        lines.append(f"{method:6s} {str(status):>3s}  {url}")
    return "\n".join(lines) + ("\n" if lines else "")


def _flows_to_har(flows: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for f in flows:
        req = f["request"]
        res = f.get("response", {})
        entry = {
            "request": {
                "method": req["method"],
                "url": req["url"],
                "headers": [{"name": k, "value": v} for k, v in req.get("headers", {}).items()],
                "postData": {"text": req.get("body", "")} if req.get("body") else {},
            },
            "response": {
                "status": res.get("status", 0),
                "headers": [{"name": k, "value": v} for k, v in res.get("headers", {}).items()],
                "content": {"text": res.get("body", "")},
            },
        }
        entries.append(entry)
    return {"log": {"version": "1.2", "entries": entries}}


# ---------------------------------------------------------------------------
# Mock rules loading
# ---------------------------------------------------------------------------

def _load_mock_rules(path: str) -> list[MockRule]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"mock rules must be a JSON array, got {type(data).__name__}")
    rules = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"each mock rule must be a JSON object, got {type(item).__name__}")
        if "url" not in item:
            raise ValueError("each mock rule must have a 'url' field")
        url = item["url"]
        status = item.get("status", 200)
        body = item.get("body", "")
        if not isinstance(url, str) or not url:
            raise ValueError("each mock rule 'url' must be a non-empty string")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise ValueError("each mock rule 'status' must be an integer from 100 to 599")
        if not isinstance(body, str):
            raise ValueError("each mock rule 'body' must be a string")
        rules.append(MockRule(
            url=url,
            status=status,
            body=body,
        ))
    return rules


def _write_mock_rules(path: Path, rules: list[MockRule]) -> None:
    payload = [
        {"url": rule.url, "status": rule.status, "body": rule.body}
        for rule in rules
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_captured_flows(path: Path) -> list[dict[str, Any]]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    complete = content.endswith("\n")
    lines = content.splitlines()
    flows: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            flow = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) and not complete:
                break
            raise ValueError(f"invalid proxy capture record at line {index}: {exc}") from exc
        if not isinstance(flow, dict) or "request" not in flow:
            raise ValueError(f"invalid proxy capture record at line {index}")
        flows.append(flow)
    return flows


def _read_capture_count(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def _remove_runtime_files(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _mitmdump_addon_path() -> Path:
    path = Path(__file__).resolve().with_name("proxy_mitm_addon.py")
    if not path.is_file():
        raise FileNotFoundError(f"android-use mitmdump addon is missing: {path}")
    return path


def find_mitmdump() -> str | None:
    override = os.environ.get("ANDROID_USE_MITMDUMP")
    if not override:
        return shutil.which("mitmdump")

    expanded = os.path.expanduser(override)
    if os.path.sep not in expanded:
        return shutil.which(expanded)
    candidate = Path(expanded).resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def inspect_mitmdump() -> dict[str, Any]:
    path = find_mitmdump()
    result: dict[str, Any] = {"ok": False, "path": path, "version": None}
    if not path:
        result["error"] = "mitmdump executable not found"
        return result
    try:
        completed = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
        return result
    output = completed.stdout.strip() or completed.stderr.strip()
    result["version"] = output.splitlines()[0] if output else None
    if completed.returncode != 0:
        result["error"] = output or f"mitmdump --version exited {completed.returncode}"
        return result
    result["ok"] = True
    return result


def _wait_for_listener(
    process: subprocess.Popen,
    host: str,
    port: int,
    ready_path: Path,
    timeout: float,
) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    deadline = time.monotonic() + timeout
    listener_ready = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if not listener_ready:
            try:
                with socket.create_connection((probe_host, port), timeout=0.2):
                    listener_ready = True
            except OSError:
                pass
        if listener_ready and ready_path.is_file():
            return True
        time.sleep(0.05)
    return False


def _stop_process(process: subprocess.Popen, timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Proxy server main loop
# ---------------------------------------------------------------------------

def run_proxy_server(serial: str, config: ProxyConfig) -> int:
    sock_path = proxy_socket_path(serial)
    pid_path = proxy_pid_path(serial)
    flows_path = proxy_flows_path(serial)
    capture_path = proxy_capture_path(serial)
    count_path = proxy_count_path(serial)
    mock_path = proxy_mock_path(serial)
    addon_path = proxy_addon_path(serial)
    ready_path = proxy_ready_path(serial)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    mock_rules: list[MockRule] = []
    if config.mock_rules_path:
        try:
            mock_rules = _load_mock_rules(config.mock_rules_path)
        except Exception as exc:
            print(f"failed to load mock rules: {exc}", file=sys.stderr)
            return 2
    try:
        addon_source_path = _mitmdump_addon_path()
        addon_path.write_bytes(addon_source_path.read_bytes())
        with contextlib.suppress(FileNotFoundError):
            ready_path.unlink()
        capture_path.write_text("", encoding="utf-8")
        count_path.write_text("0", encoding="utf-8")
        _write_mock_rules(mock_path, mock_rules)
    except Exception as exc:
        _remove_runtime_files(capture_path, count_path, mock_path, addon_path, ready_path)
        print(f"failed to prepare mitmdump: {exc}", file=sys.stderr)
        return 1

    environment = os.environ.copy()
    environment.update(
        {
            "ANDROID_USE_PROXY_CAPTURE_PATH": str(capture_path),
            "ANDROID_USE_PROXY_COUNT_PATH": str(count_path),
            "ANDROID_USE_PROXY_MOCK_PATH": str(mock_path),
            "ANDROID_USE_PROXY_READY_PATH": str(ready_path),
            "ANDROID_USE_PROXY_CAPTURE_ENABLED": "1" if config.capture else "0",
        }
    )
    try:
        process = subprocess.Popen(
            [
                config.mitmdump_path,
                "--quiet",
                "--listen-host",
                config.host,
                "--listen-port",
                str(config.port),
                "--set",
                "block_global=false",
                "--scripts",
                str(addon_path),
            ],
            env=environment,
        )
    except OSError as exc:
        _remove_runtime_files(capture_path, count_path, mock_path, addon_path, ready_path)
        print(f"failed to launch mitmdump: {exc}", file=sys.stderr)
        return 1
    if not _wait_for_listener(process, config.host, config.port, ready_path, timeout=5.0):
        exit_code = process.poll()
        _stop_process(process)
        _remove_runtime_files(capture_path, count_path, mock_path, addon_path, ready_path)
        print(
            f"mitmdump failed to listen on {config.host}:{config.port}"
            + (f" (exit={exit_code})" if exit_code is not None else ""),
            file=sys.stderr,
        )
        return 1

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    exit_code = 0
    try:
        server.bind(str(sock_path))
        sock_path.chmod(0o600)
        server.listen(8)
        server.settimeout(0.5)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")

        running = True
        while running:
            if process.poll() is not None:
                print(f"mitmdump exited unexpectedly with code {process.returncode}", file=sys.stderr)
                exit_code = 1
                break
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    request = _json_socket_recv(conn)
                    kind = request.get("kind")
                    if kind == "ping":
                        response = {
                            "ok": True,
                            "pid": os.getpid(),
                            "engine_pid": process.pid,
                            "serial": serial,
                            "host": config.host,
                            "port": config.port,
                            "flows_count": _read_capture_count(count_path),
                            "mock_rules_count": len(mock_rules),
                            "previous_device_proxy": config.previous_device_proxy,
                            "device_proxy_configured": config.device_proxy_configured,
                            "use_reverse": config.use_reverse,
                        }
                    elif kind == "stop":
                        flows = _read_captured_flows(capture_path)
                        _persist_flows(flows, flows_path)
                        response = {
                            "ok": True,
                            "stdout": f"stopped proxy serial={serial} pid={os.getpid()} flows={len(flows)}\n",
                            "stderr": "",
                            "exit_code": 0,
                            "flows_count": len(flows),
                            "flows_path": str(flows_path),
                        }
                        running = False
                    elif kind == "dump":
                        flows = _read_captured_flows(capture_path)
                        fmt = request.get("format", "text")
                        out_path = request.get("out")
                        result = _dump_flows(flows, fmt, out_path)
                        response = {"ok": True, **result}
                    elif kind == "status":
                        response = {
                            "ok": True,
                            "pid": os.getpid(),
                            "serial": serial,
                            "host": config.host,
                            "port": config.port,
                            "flows_count": _read_capture_count(count_path),
                            "mock_rules_count": len(mock_rules),
                        }
                    elif kind == "reload_mocks":
                        new_path = request.get("mock_rules_path", config.mock_rules_path)
                        if new_path:
                            try:
                                mock_rules = _load_mock_rules(new_path)
                                _write_mock_rules(mock_path, mock_rules)
                            except Exception as exc:
                                response = {"ok": False, "stdout": "", "stderr": f"failed to load mock rules: {exc}\n", "exit_code": 2}
                                _json_socket_send(conn, response)
                                continue
                        else:
                            mock_rules = []
                            _write_mock_rules(mock_path, mock_rules)
                        response = {"ok": True, "mock_rules_count": len(mock_rules)}
                    else:
                        response = {"ok": False, "stdout": "", "stderr": f"unknown proxy request: {kind}\n", "exit_code": 2}
                except Exception:
                    response = {"ok": False, "stdout": "", "stderr": traceback.format_exc(), "exit_code": 1}
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    _json_socket_send(conn, response)
    finally:
        server.close()
        _stop_process(process)
        with contextlib.suppress(Exception):
            _persist_flows(_read_captured_flows(capture_path), flows_path)
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            pid_path.unlink()
        _remove_runtime_files(capture_path, count_path, mock_path, addon_path, ready_path)
    return exit_code


def _persist_flows(flows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flows, ensure_ascii=False, indent=2), encoding="utf-8")


def _dump_flows(flows: list[dict[str, Any]], fmt: str, out_path: str | None) -> dict[str, Any]:
    if fmt == "har":
        content = json.dumps(_flows_to_har(flows), ensure_ascii=False, indent=2)
    elif fmt == "json":
        content = json.dumps(flows, ensure_ascii=False, indent=2)
    else:
        content = _flows_to_text(flows)

    if out_path:
        Path(out_path).write_text(content, encoding="utf-8")
        return {"flows_count": len(flows), "out": out_path}
    else:
        return {"flows_count": len(flows), "content": content}


# ---------------------------------------------------------------------------
# Entry points (called from command_handlers)
# ---------------------------------------------------------------------------

def _proxy_log_tail(serial: str, line_count: int = 20) -> str:
    try:
        lines = proxy_log_path(serial).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-line_count:])


def _absolute_cli_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def start_proxy(args: argparse.Namespace) -> int:
    if not hasattr(os, "fork"):
        print("proxy background processes require POSIX support; core ADB commands remain available", file=sys.stderr)
        return 2
    serial = resolve_serial(args.serial)

    existing = _proxy_ping(serial)
    if existing:
        if not args.no_device_proxy:
            try:
                _configure_device_proxy(
                    serial,
                    existing.get("host") or args.host,
                    int(existing.get("port") or args.port),
                    not args.no_reverse,
                )
            except Exception as exc:
                print(f"failed to configure device proxy: {exc}", file=sys.stderr)
                return 1
        print(f"proxy already running serial={serial} pid={existing.get('pid')} port={existing.get('port')} socket={proxy_socket_path(serial)}")
        return 0

    mitmdump_path = find_mitmdump()
    if not mitmdump_path:
        print(
            "mitmdump executable not found; install the mitmproxy binary or set ANDROID_USE_MITMDUMP",
            file=sys.stderr,
        )
        return 1
    mock_rules_path = _absolute_cli_path(args.mock) if args.mock else None
    if mock_rules_path:
        try:
            _load_mock_rules(mock_rules_path)
        except Exception as exc:
            print(f"failed to load mock rules: {exc}", file=sys.stderr)
            return 2

    if not args.skip_cert_check:
        cert_status = check_ca_cert(serial)
        if cert_status == "missing":
            print(install_ca_cert_instructions(serial), file=sys.stderr)
            print("warning: CA certificate not installed, HTTPS content will not be decrypted", file=sys.stderr)
        elif cert_status == "unknown":
            print(
                "warning: CA certificate status could not be verified via adb; "
                "the device may hide user CA storage on production builds. "
                "Check Settings > Security > Encryption & credentials > User credentials.",
                file=sys.stderr,
            )

    device_proxy_configured = not args.no_device_proxy
    previous_device_proxy = get_device_proxy(serial) if device_proxy_configured else None
    config = ProxyConfig(
        port=args.port,
        host=args.host,
        mock_rules_path=mock_rules_path,
        capture=not args.no_capture,
        mitmdump_path=mitmdump_path,
        previous_device_proxy=previous_device_proxy,
        device_proxy_configured=device_proxy_configured,
        use_reverse=device_proxy_configured and not args.no_reverse and _is_loopback_host(args.host),
    )

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            log_path = proxy_log_path(serial)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(os.devnull, "rb") as devnull, open(log_path, "w", encoding="utf-8", buffering=1) as log_file:
                os.dup2(devnull.fileno(), sys.stdin.fileno())
                os.dup2(log_file.fileno(), sys.stdout.fileno())
                os.dup2(log_file.fileno(), sys.stderr.fileno())
                os._exit(run_proxy_server(serial, config))
        except Exception:
            traceback.print_exc()
            os._exit(1)

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = _proxy_ping(serial, timeout=0.2)
        if status:
            if device_proxy_configured:
                try:
                    _configure_device_proxy(serial, config.host, config.port, not args.no_reverse)
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        _proxy_request(serial, {"kind": "stop"}, timeout=2.0)
                    with contextlib.suppress(Exception):
                        restore_device_proxy(serial, previous_device_proxy)
                    with contextlib.suppress(Exception):
                        remove_reverse(serial, config.port)
                    print(f"failed to configure device proxy: {exc}", file=sys.stderr)
                    return 1
            print(f"proxy started serial={serial} pid={status.get('pid')} port={config.port} socket={proxy_socket_path(serial)}")
            return 0
        try:
            ended_pid, wait_status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            ended_pid, wait_status = pid, 1
        if ended_pid == pid:
            detail = _proxy_log_tail(serial)
            child_exit = os.waitstatus_to_exitcode(wait_status)
            print(
                f"proxy process exited before startup exit={child_exit} log={proxy_log_path(serial)}"
                + (f"\n{detail}" if detail else ""),
                file=sys.stderr,
            )
            return 1
        time.sleep(0.1)

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    print(f"proxy start timed out serial={serial} pid={pid} log={proxy_log_path(serial)}", file=sys.stderr)
    return 1


def stop_proxy(args: argparse.Namespace) -> int:
    serial = resolve_serial(args.serial)
    status = _proxy_ping(serial, timeout=args.timeout)
    port = int((status or {}).get("port") or args.port)
    response: dict[str, Any] | None = None
    request_error: RuntimeError | None = None
    try:
        response = _proxy_request(serial, {"kind": "stop"}, timeout=args.timeout)
    except RuntimeError as exc:
        request_error = exc

    if not args.no_restore_proxy:
        try:
            if status and status.get("device_proxy_configured"):
                restore_device_proxy(serial, status.get("previous_device_proxy"))
            elif not status and get_device_proxy(serial) == f"127.0.0.1:{port}":
                clear_device_proxy(serial)
        except Exception as exc:
            print(f"warning: failed to restore device proxy: {exc}", file=sys.stderr)
        if not status or status.get("use_reverse"):
            try:
                remove_reverse(serial, port)
            except Exception as exc:
                print(f"warning: failed to remove adb reverse tcp:{port}: {exc}", file=sys.stderr)

    if request_error is not None:
        print(str(request_error), file=sys.stderr)
        return 1
    assert response is not None
    sys.stdout.write(response.get("stdout") or "")
    sys.stderr.write(response.get("stderr") or "")
    return int(response.get("exit_code", 0))


def status_proxy(args: argparse.Namespace) -> int:
    serial = resolve_serial(args.serial)
    status = _proxy_ping(serial, timeout=args.timeout)
    if status:
        print(
            f"proxy running serial={serial} pid={status.get('pid')} "
            f"host={status.get('host')} port={status.get('port')} flows={status.get('flows_count', 0)} "
            f"mocks={status.get('mock_rules_count', 0)} "
            f"socket={proxy_socket_path(serial)}"
        )
        return 0
    print(f"proxy stopped serial={serial} socket={proxy_socket_path(serial)}")
    return 1


def doctor_proxy(args: argparse.Namespace) -> int:
    mitmdump = inspect_mitmdump()
    local_ca = get_ca_cert_path()
    serial: str | None = None
    serial_error: str | None = None
    try:
        serial = resolve_serial(args.serial)
    except SystemExit as exc:
        serial_error = str(exc)

    proxy_status = _proxy_ping(serial, timeout=args.timeout) if serial else None
    device_proxy: str | None = None
    device_ca: str | None = None
    device_error: str | None = None
    if serial:
        try:
            device_proxy = get_device_proxy(serial)
            device_ca = check_ca_cert(serial)
        except Exception as exc:
            device_error = str(exc)

    print("Proxy Doctor:\n")
    if mitmdump["ok"]:
        print(f"  [ok] mitmdump executable: {mitmdump['path']}")
        if mitmdump.get("version"):
            print(f"       version: {mitmdump['version']}")
    else:
        location = mitmdump.get("path") or "not found"
        print(f"  [missing] mitmdump executable: {location}")
        if mitmdump.get("error"):
            print(f"            error: {mitmdump['error']}")

    if local_ca:
        print(f"  [ok] local CA: {local_ca}")
    else:
        print("  [missing] local CA: run mitmdump once to generate ~/.mitmproxy certificates")

    if serial and not device_error:
        print(f"  [ok] device: {serial}")
    elif serial:
        print(f"  [error] device: {serial}: {device_error}")
    else:
        print(f"  [skip] device: {serial_error or 'not selected'}")

    if proxy_status:
        print(
            f"  [ok] proxy: running pid={proxy_status.get('pid')} "
            f"port={proxy_status.get('port')} flows={proxy_status.get('flows_count', 0)}"
        )
    else:
        print("  [-] proxy: stopped")

    if serial and not device_error:
        print(f"  [-] device proxy: {device_proxy or ':0'}")
        print(f"  [-] device CA: {device_ca or 'unknown'}")
    return 0 if mitmdump["ok"] else 1


def dump_proxy(args: argparse.Namespace) -> int:
    serial = resolve_serial(args.serial)
    out_path = _absolute_cli_path(args.out) if args.out else None
    try:
        response = _proxy_request(
            serial,
            {"kind": "dump", "format": args.format, "out": out_path},
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if response.get("ok"):
        if "content" in response:
            print(response["content"], end="")
        else:
            print(f"dumped {response.get('flows_count', 0)} flows to {response.get('out')}")
        return 0
    print(response.get("stderr", "dump failed"), file=sys.stderr)
    return 1


def mock_proxy(args: argparse.Namespace) -> int:
    serial = resolve_serial(args.serial)
    rules_path = _absolute_cli_path(args.rules)
    try:
        response = _proxy_request(
            serial,
            {"kind": "reload_mocks", "mock_rules_path": rules_path},
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if response.get("ok"):
        print(f"mock rules loaded: {response.get('mock_rules_count', 0)} rules active")
        return 0
    print(response.get("stderr", "mock load failed"), file=sys.stderr)
    return 1


def cert_proxy(args: argparse.Namespace) -> int:
    serial = resolve_serial(args.serial)

    if args.cert_action == "status":
        cert_status = check_ca_cert(serial)
        local_cert = get_ca_cert_path()
        local_cert_text = str(local_cert) if local_cert else "missing"
        print(
            f"CA certificate status serial={serial} device={cert_status} local={local_cert_text} "
            f"user_path={MITMPROXY_CA_USER_PATH} system_path={MITMPROXY_CA_SYSTEM_PATH}"
        )
        if cert_status == "installed":
            return 0
        if cert_status == "missing":
            return 1
        return 2

    if args.cert_action == "instructions":
        print(install_ca_cert_instructions(serial))
        return 0

    if args.cert_action == "push":
        try:
            cert_path, device_path = push_ca_cert(serial, args.cert, args.device_path)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            _print_cert_error(exc)
            return 1
        print(f"CA certificate pushed serial={serial} local={cert_path} device={device_path}")
        return 0

    if args.cert_action == "install-root":
        try:
            cert_path, installed_path = install_ca_cert_root(serial, args.cert, args.device_path, args.reboot)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            _print_cert_error(exc)
            return 1
        print(f"CA certificate installed serial={serial} local={cert_path} device={installed_path}")
        if args.reboot:
            print("device reboot requested")
        else:
            print("reboot the device before expecting HTTPS decryption to work")
        return 0

    print(f"unsupported proxy cert action: {args.cert_action}", file=sys.stderr)
    return 2


def _print_cert_error(exc: Exception) -> None:
    if isinstance(exc, subprocess.CalledProcessError):
        output = ((exc.stdout or "") + (exc.stderr or "")).strip()
        if output:
            print(output, file=sys.stderr)
            return
    print(str(exc), file=sys.stderr)


def _configure_device_proxy(serial: str, host: str, port: int, use_reverse: bool) -> None:
    if use_reverse and _is_loopback_host(host):
        setup_reverse(serial, port, port)
        set_device_proxy(serial, "127.0.0.1", port)
    else:
        set_device_proxy(serial, host, port)
