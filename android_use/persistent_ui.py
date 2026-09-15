"""Transparent lifecycle for the bundled device-side UiAutomation session."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shlex
import socket
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .android_device import AdbCommandError, run_adb, run_adb_shell


DEVICE_PORT = 27183
PROTOCOL_VERSION = 2
SERVER_CLASS = "io.github.xhzq233.androiduse.UiAutomationServerTest#testServe"
REMOTE_TEST_BASE = "/system/framework/android.test.base.jar"
REMOTE_LOG = "/data/local/tmp/android-use-ui-server.log"
REMOTE_PID = "/data/local/tmp/android-use-ui-server.pid"
REMOTE_JAR_MARKER = "/data/local/tmp/android-use-ui-server-"
MAX_HIERARCHY_BYTES = 32 * 1024 * 1024
PERSISTENT_TIMEOUT_CAP = 8.0


class PersistentUiError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerInfo:
    protocol: int
    pid: int
    build_hash: str


@dataclass(frozen=True)
class ServerProcessState:
    pid: int | None
    safe_to_start: bool


def server_jar_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "android-use-ui-server.jar"


@lru_cache(maxsize=1)
def expected_build_hash() -> str:
    path = server_jar_path()
    if not path.is_file():
        raise PersistentUiError(f"bundled UiAutomation server is missing: {path}")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PersistentUiError(f"cannot read bundled UiAutomation server: {exc}") from exc


def _remote_jar(build_hash: str) -> str:
    return f"/data/local/tmp/android-use-ui-server-{build_hash[:16]}.jar"


def _runtime_token_path(serial: str) -> Path:
    token = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"android-use-ui-{token}.token"


@lru_cache(maxsize=None)
def _session_token(serial: str) -> str:
    path = _runtime_token_path(serial)
    for _ in range(3):
        try:
            existing = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            existing = ""
        except OSError as exc:
            raise PersistentUiError(f"cannot read UiAutomation session token: {exc}") from exc
        if len(existing) == 64 and all(character in "0123456789abcdef" for character in existing):
            try:
                path.chmod(0o600)
            except OSError as exc:
                raise PersistentUiError(
                    f"cannot protect UiAutomation session token: {exc}"
                ) from exc
            return existing
        if existing:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise PersistentUiError(
                    f"cannot replace invalid UiAutomation session token: {exc}"
                ) from exc

        generated = secrets.token_hex(32)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise PersistentUiError(f"cannot create UiAutomation session token: {exc}") from exc
        try:
            os.write(descriptor, (generated + "\n").encode("ascii"))
        finally:
            os.close(descriptor)
        return generated
    raise PersistentUiError("cannot establish a stable UiAutomation session token")


def _forward(serial: str, timeout: float) -> int:
    try:
        result = run_adb(
            ["forward", "tcp:0", f"tcp:{DEVICE_PORT}"],
            serial=serial,
            timeout=max(0.2, min(timeout, 5.0)),
            check=True,
        )
    except AdbCommandError as exc:
        raise PersistentUiError(f"cannot create ADB forward: {exc}") from exc
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise PersistentUiError(
            f"adb did not return a local forward port: {result.stdout.strip()!r}"
        ) from exc


def _remove_forward(serial: str, port: int, timeout: float = 0.5) -> None:
    try:
        run_adb(
            ["forward", "--remove", f"tcp:{port}"],
            serial=serial,
            timeout=max(0.2, min(timeout, 1.0)),
            check=False,
        )
    except AdbCommandError:
        pass


def _read_exact(stream, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise PersistentUiError(
                f"UiAutomation server closed after {length - remaining}/{length} bytes"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _exchange_raw(
    serial: str,
    request: str,
    *,
    timeout: float,
    expects_payload: bool = False,
) -> tuple[list[str], bytes]:
    deadline = time.monotonic() + max(0.2, timeout)

    def remaining() -> float:
        return max(0.2, deadline - time.monotonic())

    port = _forward(serial, remaining())
    try:
        try:
            connection = socket.create_connection(
                ("127.0.0.1", port),
                timeout=min(remaining(), 3.0),
            )
        except OSError as exc:
            raise PersistentUiError(f"UiAutomation server is unavailable: {exc}") from exc
        with connection:
            connection.settimeout(remaining())
            connection.sendall((request + "\n").encode("ascii"))
            with connection.makefile("rb") as stream:
                raw_header = stream.readline(1024)
                if not raw_header.endswith(b"\n"):
                    raise PersistentUiError("UiAutomation server returned an incomplete header")
                header = raw_header.decode("utf-8", errors="replace").strip()
                fields = header.split()
                if not fields or fields[0] != "OK":
                    raise PersistentUiError(
                        header.removeprefix("ERR ").strip()
                        or "UiAutomation server returned an empty error"
                    )
                if expects_payload:
                    if len(fields) < 2:
                        raise PersistentUiError(f"invalid DUMP response: {header!r}")
                    try:
                        length = int(fields[1])
                    except ValueError as exc:
                        raise PersistentUiError(
                            f"invalid hierarchy length: {fields[1]!r}"
                        ) from exc
                    if length < 1 or length > MAX_HIERARCHY_BYTES:
                        raise PersistentUiError(
                            f"invalid hierarchy length: {length}"
                        )
                    return fields, _read_exact(stream, length)
                return fields, b""
    except (OSError, TimeoutError) as exc:
        raise PersistentUiError(f"UiAutomation request failed: {exc}") from exc
    finally:
        _remove_forward(serial, port, min(0.5, remaining()))


def _exchange(
    serial: str,
    command: str,
    *,
    timeout: float,
) -> tuple[list[str], bytes]:
    request = f"AUTH {_session_token(serial)} {command}"
    return _exchange_raw(
        serial,
        request,
        timeout=timeout,
        expects_payload=command.startswith("DUMP "),
    )


def _server_info(serial: str, timeout: float) -> ServerInfo:
    fields, _ = _exchange(serial, "PING", timeout=timeout)
    if len(fields) != 4:
        raise PersistentUiError(f"unsupported PING response: {' '.join(fields)!r}")
    try:
        return ServerInfo(
            protocol=int(fields[1]),
            pid=int(fields[2]),
            build_hash=fields[3],
        )
    except ValueError as exc:
        raise PersistentUiError(f"invalid PING response: {' '.join(fields)!r}") from exc


def _matches_expected(info: ServerInfo, build_hash: str) -> bool:
    return info.protocol == PROTOCOL_VERSION and info.build_hash == build_hash


def _server_process_state(
    serial: str,
    timeout: float = 1.0,
) -> ServerProcessState:
    deadline = time.monotonic() + max(0.2, timeout)

    def remaining() -> float:
        return max(0.2, deadline - time.monotonic())

    try:
        result = run_adb_shell(
            serial,
            ["cat", REMOTE_PID],
            timeout=remaining(),
            check=False,
        )
    except AdbCommandError:
        return ServerProcessState(None, False)
    if result.returncode != 0:
        try:
            absent = run_adb_shell(
                serial,
                ["test", "!", "-e", REMOTE_PID],
                timeout=remaining(),
                check=False,
            )
        except AdbCommandError:
            return ServerProcessState(None, False)
        return ServerProcessState(None, absent.returncode == 0)
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return ServerProcessState(None, False)
    if pid <= 1:
        return ServerProcessState(None, False)
    try:
        environment = run_adb_shell(
            serial,
            ["cat", f"/proc/{pid}/environ"],
            timeout=remaining(),
            check=False,
        )
    except AdbCommandError:
        return ServerProcessState(None, False)
    if environment.returncode != 0:
        try:
            gone = run_adb_shell(
                serial,
                ["test", "!", "-d", f"/proc/{pid}"],
                timeout=remaining(),
                check=False,
            )
        except AdbCommandError:
            return ServerProcessState(None, False)
        return ServerProcessState(None, gone.returncode == 0)
    if (
        "CLASSPATH=" not in environment.stdout
        or REMOTE_JAR_MARKER not in environment.stdout
    ):
        return ServerProcessState(None, False)
    return ServerProcessState(pid, False)


def _owned_server_pid(serial: str, timeout: float = 1.0) -> int | None:
    return _server_process_state(serial, timeout).pid


def _force_stop_owned_server(serial: str, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + max(0.2, timeout)

    def remaining() -> float:
        return max(0.2, deadline - time.monotonic())

    state = _server_process_state(serial, remaining())
    if state.pid is None:
        return state.safe_to_start
    pid = state.pid
    try:
        result = run_adb_shell(
            serial,
            ["kill", "-9", str(pid)],
            timeout=remaining(),
            check=False,
        )
    except AdbCommandError:
        return False
    if result.returncode != 0:
        return False

    while True:
        try:
            gone = run_adb_shell(
                serial,
                ["test", "!", "-d", f"/proc/{pid}"],
                timeout=remaining(),
                check=False,
            )
        except AdbCommandError:
            return False
        if gone.returncode == 0:
            try:
                run_adb_shell(
                    serial,
                    ["rm", "-f", REMOTE_PID],
                    timeout=remaining(),
                    check=False,
                )
            except AdbCommandError:
                pass
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.025)


def _stop_server(
    serial: str,
    timeout: float = 0.75,
    *,
    hard: bool = False,
) -> bool:
    deadline = time.monotonic() + max(0.2, timeout)

    def remaining() -> float:
        return max(0.2, deadline - time.monotonic())

    stopped = False
    if not hard:
        try:
            _exchange(serial, "SHUTDOWN", timeout=min(0.2, remaining()))
            stopped = True
        except Exception as error:
            if "unsupported request" in str(error) or "unauthorized" in str(error):
                try:
                    _exchange_raw(
                        serial,
                        "SHUTDOWN",
                        timeout=min(0.2, remaining()),
                    )
                    stopped = True
                except Exception:
                    pass
    if stopped:
        time.sleep(0.05)
        state = _server_process_state(serial, remaining())
        if state.safe_to_start:
            return True
    return _force_stop_owned_server(serial, remaining())


@contextlib.contextmanager
def _start_lock(serial: str):
    token = hashlib.sha256(serial.encode("utf-8")).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"android-use-ui-{token}.lock"
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _remote_exists(serial: str, path: str, timeout: float = 3.0) -> bool:
    try:
        result = run_adb_shell(
            serial,
            ["test", "-r", path],
            timeout=max(0.2, timeout),
            check=False,
        )
    except AdbCommandError as exc:
        raise PersistentUiError(f"cannot inspect device file {path}: {exc}") from exc
    return result.returncode == 0


def _remote_hash_matches(
    serial: str,
    path: str,
    expected_hash: str,
    timeout: float,
) -> bool:
    try:
        result = run_adb_shell(
            serial,
            ["sha256sum", path],
            timeout=max(0.2, timeout),
            check=False,
        )
    except AdbCommandError:
        return False
    return (
        result.returncode == 0
        and result.stdout.split(maxsplit=1)[0:1] == [expected_hash]
    )


def _protect_remote_jar(serial: str, path: str, timeout: float) -> None:
    try:
        run_adb_shell(
            serial,
            ["chmod", "0444", path],
            timeout=max(0.2, timeout),
            check=True,
        )
    except AdbCommandError as exc:
        raise PersistentUiError(f"cannot protect UiAutomation server: {exc}") from exc


def _ensure_remote_jar(serial: str, build_hash: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + max(0.2, timeout)

    def remaining() -> float:
        return max(0.2, deadline - time.monotonic())

    remote = _remote_jar(build_hash)
    if _remote_exists(serial, remote, remaining()):
        if _remote_hash_matches(serial, remote, build_hash, remaining()):
            _protect_remote_jar(serial, remote, remaining())
            return remote
        try:
            run_adb_shell(
                serial,
                ["rm", "-f", remote],
                timeout=remaining(),
                check=True,
            )
        except AdbCommandError as exc:
            raise PersistentUiError(
                f"cannot replace invalid UiAutomation server: {exc}"
            ) from exc
    try:
        run_adb(
            ["push", str(server_jar_path()), remote],
            serial=serial,
            timeout=remaining(),
            check=True,
        )
    except AdbCommandError as exc:
        raise PersistentUiError(f"cannot push UiAutomation server: {exc}") from exc
    if not _remote_hash_matches(serial, remote, build_hash, remaining()):
        raise PersistentUiError("pushed UiAutomation server failed SHA-256 verification")
    _protect_remote_jar(serial, remote, remaining())
    return remote


def _launch_server(
    serial: str,
    remote_jar: str,
    build_hash: str,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + max(0.2, timeout)

    def remaining() -> float:
        return max(0.2, deadline - time.monotonic())

    jars = []
    if _remote_exists(serial, REMOTE_TEST_BASE, remaining()):
        jars.append(REMOTE_TEST_BASE)
    tokens = [
        "uiautomator",
        "runtest",
        *jars,
        remote_jar,
        "--nohup",
        "-e",
        "androidUseBuild",
        build_hash,
        "-e",
        "androidUseToken",
        _session_token(serial),
        "-c",
        SERVER_CLASS,
    ]
    shell_command = (
        f"{shlex.join(tokens)} >{shlex.quote(REMOTE_LOG)} 2>&1 </dev/null &"
    )
    try:
        run_adb(
            ["shell", shell_command],
            serial=serial,
            timeout=remaining(),
            check=True,
        )
    except AdbCommandError as exc:
        raise PersistentUiError(f"cannot launch UiAutomation server: {exc}") from exc


def _startup_log(serial: str, timeout: float = 1.0) -> str:
    try:
        result = run_adb_shell(
            serial,
            ["tail", "-n", "20", REMOTE_LOG],
            timeout=max(0.2, timeout),
            check=False,
        )
    except AdbCommandError:
        return ""
    return result.stdout.strip() or result.stderr.strip()


def _ensure_server_locked(serial: str, deadline: float) -> ServerInfo:
    def remaining() -> float:
        available = deadline - time.monotonic()
        if available <= 0:
            raise PersistentUiError("UiAutomation server startup timed out")
        return max(0.2, available)

    build_hash = expected_build_hash()
    try:
        info = _server_info(serial, min(remaining(), 1.0))
    except PersistentUiError:
        info = None
    if info and _matches_expected(info, build_hash):
        return info
    if not _stop_server(serial, min(remaining(), 0.75)):
        raise PersistentUiError(
            "cannot confirm the previous UiAutomation server stopped"
        )
    time.sleep(0.05)

    remote = _ensure_remote_jar(serial, build_hash, remaining())
    _launch_server(serial, remote, build_hash, remaining())
    last_error = "server did not respond"
    while time.monotonic() < deadline:
        try:
            info = _server_info(serial, min(remaining(), 0.75))
        except PersistentUiError as exc:
            last_error = str(exc)
            time.sleep(0.05)
            continue
        if _matches_expected(info, build_hash):
            return info
        last_error = (
            f"unexpected server protocol/build: "
            f"{info.protocol}/{info.build_hash}"
        )
        break
    available = deadline - time.monotonic()
    log = _startup_log(serial, min(available, 0.5)) if available >= 0.2 else ""
    detail = f"; runner log: {log[-1000:]}" if log else ""
    with contextlib.suppress(Exception):
        _stop_server(serial, 0.5, hard=True)
    raise PersistentUiError(f"UiAutomation server startup failed: {last_error}{detail}")


def ensure_server(serial: str, timeout: float = 5.0) -> ServerInfo:
    deadline = time.monotonic() + max(0.5, min(timeout, 5.0))
    with _start_lock(serial):
        return _ensure_server_locked(serial, deadline)


def _dump_once(serial: str, compressed: bool, timeout: float) -> str:
    fields, payload = _exchange(
        serial,
        f"DUMP {1 if compressed else 0}",
        timeout=timeout,
    )
    if len(fields) != 4:
        raise PersistentUiError(f"unsupported DUMP response: {' '.join(fields)!r}")
    try:
        protocol = int(fields[2])
    except ValueError as exc:
        raise PersistentUiError(f"invalid DUMP response: {' '.join(fields)!r}") from exc
    if protocol != PROTOCOL_VERSION or fields[3] != expected_build_hash():
        raise PersistentUiError(
            f"stale UiAutomation server: protocol={protocol} build={fields[3]}"
        )
    try:
        xml = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PersistentUiError("UiAutomation hierarchy is not valid UTF-8") from exc
    if not xml.startswith("<?xml") or not xml.rstrip().endswith("</hierarchy>"):
        raise PersistentUiError("UiAutomation server returned an invalid hierarchy")
    return xml


def dump_xml(serial: str, compressed: bool = True, timeout: float = 10.0) -> str:
    total_budget = max(0.5, min(timeout, PERSISTENT_TIMEOUT_CAP))
    deadline = time.monotonic() + total_budget

    def remaining() -> float:
        available = deadline - time.monotonic()
        if available <= 0:
            raise PersistentUiError("persistent UiAutomation exhausted its timeout budget")
        return max(0.2, available)

    try:
        initial_budget = min(5.25, max(0.2, total_budget - 2.5))
        return _dump_once(serial, compressed, initial_budget)
    except PersistentUiError as exc:
        first_error = exc

    try:
        with _start_lock(serial):
            try:
                return _dump_once(serial, compressed, min(remaining(), 1.0))
            except PersistentUiError:
                pass
            if not _stop_server(
                serial,
                timeout=min(0.75, remaining()),
                hard=True,
            ):
                raise PersistentUiError(
                    "cannot confirm the failed UiAutomation server stopped"
                )
            startup_deadline = min(
                deadline,
                time.monotonic() + min(remaining(), 3.0),
            )
            _ensure_server_locked(serial, startup_deadline)
            return _dump_once(serial, compressed, remaining())
    except PersistentUiError as exc:
        with contextlib.suppress(Exception):
            _stop_server(
                serial,
                timeout=min(0.5, max(0.2, deadline - time.monotonic())),
                hard=True,
            )
        raise PersistentUiError(
            f"persistent UiAutomation failed after restart: {exc}; "
            f"initial failure: {first_error}"
        ) from exc


def stop_server(
    serial: str,
    timeout: float = 0.75,
    *,
    hard: bool = False,
) -> bool:
    with _start_lock(serial):
        return _stop_server(serial, timeout=timeout, hard=hard)
