"""Short animation capture using a pipelined stock-ADB screencap backend."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .android_device import run_adb_bytes, spawn_adb
from .logcat_commands import active_status
from .native_helper import NativeHelperError, compile_swift_helper, native_source_path
from .ui_semantics import find_target_nodes, snapshot
from .visual_capture import copy_selected_frames, image_dimensions, select_contact_sheet_frames


@dataclass
class TriggerState:
    condition: str
    target: str
    deadline: float
    triggered_at: float | None = None
    observed: bool = False
    last_match_count: int = 0
    last_dump_ms: float | None = None
    error: str | None = None


def parse_trigger(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    if ":" not in value:
        raise ValueError("--trigger must use text:VALUE or text-gone:VALUE")
    condition, target = value.split(":", 1)
    if condition not in ("text", "text-gone") or not target:
        raise ValueError("--trigger must use text:VALUE or text-gone:VALUE")
    return condition, target


def _frame_capture(
    serial: str,
    output: Path,
    index: int,
    origin: float,
    timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    elapsed_ms = (started - origin) * 1000
    result = run_adb_bytes(
        ["exec-out", "screencap", "-p"],
        serial=serial,
        timeout=timeout,
        check=False,
    )
    completed = time.monotonic()
    data = result.stdout
    valid = (
        result.returncode == 0
        and data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) > 32
    )
    path = output / "frames" / f"frame-{index:04d}-{round(elapsed_ms):07d}ms.png"
    error = None
    size = None
    if valid:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            size = list(image_dimensions(path))
        except (OSError, ValueError) as exc:
            valid = False
            error = str(exc)
            path.unlink(missing_ok=True)
    else:
        error = (
            result.stderr.decode("utf-8", errors="replace").strip()
            or data[:200].decode("utf-8", errors="replace").strip()
            or f"adb exit code {result.returncode}"
        )
    return {
        "index": index,
        "elapsedMs": round(elapsed_ms, 3),
        "completedMs": round((completed - origin) * 1000, 3),
        "latencyMs": round((completed - started) * 1000, 3),
        "path": str(path.resolve()) if valid else None,
        "bytes": len(data),
        "size": size,
        "valid": valid,
        "error": error,
    }


def _watch_trigger(
    serial: str,
    state: TriggerState,
    *,
    origin: float,
    contains: bool,
    interval: float,
    allow_already_gone: bool,
    done: threading.Event,
) -> None:
    while not done.is_set() and time.monotonic() < state.deadline:
        try:
            page = snapshot(serial, timeout=min(10.0, max(1.0, state.deadline - time.monotonic())))
            matches = find_target_nodes(
                page.root,
                state.target,
                package_name=page.package,
                contains=contains,
            )
            state.last_match_count = len(matches)
            state.last_dump_ms = page.elapsed_ms
            state.observed = state.observed or bool(matches)
            if state.condition == "text" and matches:
                state.triggered_at = time.monotonic() - origin
                return
            if (
                state.condition == "text-gone"
                and not matches
                and (state.observed or allow_already_gone)
            ):
                state.triggered_at = time.monotonic() - origin
                return
        except Exception as exc:
            state.error = str(exc)
        done.wait(interval)
    if state.triggered_at is None and state.error is None:
        state.error = "trigger timed out"


def _start_capture_log(
    serial: str,
    output: Path,
) -> tuple[subprocess.Popen[bytes] | None, Any, dict[str, Any]]:
    active = active_status(serial)
    if active:
        return None, None, {
            "mode": "active",
            "path": active.get("logFile"),
            "healthy": True,
        }
    log_path = output / "capture.log"
    handle = log_path.open("ab")
    try:
        process = spawn_adb(
            ["logcat", "-v", "threadtime", "-T", "1"],
            serial=serial,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        handle.close()
        raise
    return process, handle, {
        "mode": "command",
        "path": str(log_path.resolve()),
        "healthy": True,
    }


def _stop_capture_log(
    process: subprocess.Popen[bytes] | None,
    handle,
    metadata: dict[str, Any],
) -> None:
    try:
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
            metadata["exitCode"] = process.returncode
            metadata["healthy"] = process.returncode in (0, -15)
    finally:
        if handle is not None:
            handle.close()


def _write_contact_sheet(frames: list[dict[str, Any]], output: Path) -> tuple[str | None, str | None]:
    selected = select_contact_sheet_frames(frames)
    helper_input = output / ".contact-sheet-input.json"
    helper_input.write_text(
        json.dumps(
            {
                "frames": [
                    {"path": frame["path"], "elapsedMs": frame["elapsedMs"]}
                    for frame in selected
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    contact_path = output / "contact-sheet.png"
    try:
        helper = compile_swift_helper(native_source_path("ImageHelper.swift"))
        completed = subprocess.run(
            [
                str(helper),
                "contact-sheet",
                "--input",
                str(helper_input),
                "--out",
                str(contact_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30.0,
        )
        if completed.returncode != 0 or not contact_path.is_file():
            detail = (completed.stderr or completed.stdout).strip() or "no output"
            return None, f"contact sheet helper failed: {detail}"
        return str(contact_path.resolve()), None
    except (NativeHelperError, OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    finally:
        helper_input.unlink(missing_ok=True)


def run_capture(
    *,
    serial: str,
    output: Path,
    fps: float,
    duration: float,
    trigger: tuple[str, str] | None,
    before: float,
    after: float,
    trigger_timeout: float,
    trigger_interval: float,
    contains: bool,
    allow_already_gone: bool,
    collect_logcat: bool,
) -> dict[str, Any]:
    if not 0.5 <= fps <= 10:
        raise ValueError("--fps must be within [0.5,10]")
    if duration <= 0 or before < 0 or after < 0 or trigger_timeout <= 0:
        raise ValueError("capture durations must be positive; --before may be zero")
    output.mkdir(parents=True, exist_ok=True)
    origin = time.monotonic()
    trigger_done = threading.Event()
    trigger_state: TriggerState | None = None
    trigger_thread: threading.Thread | None = None
    if trigger:
        trigger_state = TriggerState(
            condition=trigger[0],
            target=trigger[1],
            deadline=origin + trigger_timeout,
        )
        trigger_thread = threading.Thread(
            target=_watch_trigger,
            args=(serial, trigger_state),
            kwargs={
                "origin": origin,
                "contains": contains,
                "interval": trigger_interval,
                "allow_already_gone": allow_already_gone,
                "done": trigger_done,
            },
            daemon=True,
        )
        trigger_thread.start()

    log_process = None
    log_handle = None
    log_metadata: dict[str, Any] = {"mode": "disabled", "path": None, "healthy": True}
    if collect_logcat:
        try:
            log_process, log_handle, log_metadata = _start_capture_log(serial, output)
        except Exception as exc:
            log_metadata = {"mode": "failed", "path": None, "healthy": False, "error": str(exc)}

    futures: list[Future[dict[str, Any]]] = []
    interval = 1.0 / fps
    fixed_count = max(1, math.ceil(duration * fps))
    max_workers = min(8, max(4, math.ceil(fps)))
    records: list[dict[str, Any]]
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            index = 0
            while True:
                now = time.monotonic()
                elapsed = now - origin
                if trigger_state is None:
                    if index >= fixed_count:
                        break
                else:
                    if trigger_state.triggered_at is not None:
                        stop_at = trigger_state.triggered_at + after
                        if elapsed >= stop_at:
                            break
                    elif now >= trigger_state.deadline:
                        break
                due = origin + index * interval
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                futures.append(
                    executor.submit(
                        _frame_capture,
                        serial,
                        output,
                        index,
                        origin,
                        12.0,
                    )
                )
                index += 1

            records = [future.result() for future in futures]
    finally:
        trigger_done.set()
        if trigger_thread:
            trigger_thread.join(timeout=3.0)
        _stop_capture_log(log_process, log_handle, log_metadata)
    completed_at = time.monotonic()

    valid_frames = [record for record in records if record["valid"]]
    if trigger_state and trigger_state.triggered_at is not None:
        start_window = max(0.0, trigger_state.triggered_at - before) * 1000
        end_window = (trigger_state.triggered_at + after) * 1000
        kept = [
            frame
            for frame in valid_frames
            if start_window <= frame["elapsedMs"] <= end_window + interval * 1000
        ]
        kept_paths = {frame["path"] for frame in kept}
        for frame in valid_frames:
            if frame["path"] not in kept_paths:
                Path(frame["path"]).unlink(missing_ok=True)
        valid_frames = kept

    valid_frames.sort(key=lambda frame: frame["elapsedMs"])
    if len(valid_frames) > 1:
        capture_span = (valid_frames[-1]["elapsedMs"] - valid_frames[0]["elapsedMs"]) / 1000
        actual_fps = (len(valid_frames) - 1) / capture_span if capture_span > 0 else 0.0
    else:
        actual_fps = 0.0
    wall_seconds = completed_at - origin
    delivery_fps = len(valid_frames) / wall_seconds if wall_seconds > 0 else 0.0
    selected = copy_selected_frames(valid_frames, output)
    contact_sheet, contact_error = _write_contact_sheet(valid_frames, output) if valid_frames else (None, None)
    errors = [record["error"] for record in records if not record["valid"] and record["error"]]
    if contact_error:
        errors.append(contact_error)

    trigger_payload = None
    if trigger_state:
        trigger_payload = {
            "condition": trigger_state.condition,
            "target": trigger_state.target,
            "triggered": trigger_state.triggered_at is not None,
            "elapsedMs": (
                round(trigger_state.triggered_at * 1000, 3)
                if trigger_state.triggered_at is not None
                else None
            ),
            "observed": trigger_state.observed,
            "lastMatchCount": trigger_state.last_match_count,
            "lastDumpMs": trigger_state.last_dump_ms,
            "error": trigger_state.error,
        }

    manifest = {
        "schemaVersion": 1,
        "backend": "adb-pipelined-png",
        "serial": serial,
        "requestedFps": fps,
        "actualFps": round(actual_fps, 2),
        "deliveryFps": round(delivery_fps, 2),
        "durationMs": round(wall_seconds * 1000, 3),
        "requestedDurationMs": round(duration * 1000, 3) if trigger is None else None,
        "framesRequested": len(records),
        "framesValid": len(valid_frames),
        "framesInvalid": len(records) - sum(1 for record in records if record["valid"]),
        "trigger": trigger_payload,
        "frames": valid_frames,
        "selected": selected,
        "contactSheet": contact_sheet,
        "logcat": log_metadata,
        "errors": errors,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest["manifest"] = str(manifest_path.resolve())
    return manifest
