"""ADB-first command implementations for the android-use CLI."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from pathlib import Path
from typing import Any

from .android_device import (
    AdbCommandError,
    adb_executable,
    adb_version_text,
    display_metrics,
    list_adb_devices,
    resolve_serial,
    run_adb_bytes,
    run_adb_shell,
)
from .android_ui_tree import iter_nodes
from .capture_command import parse_trigger, run_capture
from .geometry_types import format_rect
from .logcat_commands import (
    active_status,
    format_status,
    log_offset,
    read_log_since,
    start_logcat,
    state_path,
    stop_logcat,
)
from .logcat_supervisor import collector_status
from .runtime_paths import runtime_directory
from .ui_semantics import (
    UISnapshot,
    clean_tree_text,
    current_app,
    find_target_nodes,
    fingerprint,
    node_payload,
    node_rect,
    relative_scroll_direction,
    select_scroll_node,
    select_target,
    snapshot,
    swipe_points,
)
from .visual_capture import (
    RawScreenshot,
    image_dimensions,
    marked_screenshot,
    normalized_to_input,
    parse_raw_screenshot,
    png_dimensions,
    screenshot_to_input,
    write_png,
)


def _serial(args: argparse.Namespace) -> str:
    return getattr(args, "resolved_serial", None) or resolve_serial(getattr(args, "serial", None))


def _device_payload(args: argparse.Namespace) -> dict[str, str]:
    return {
        "serial": _serial(args),
        "transport": getattr(args, "resolved_transport", "unknown"),
    }


def _artifact_directory(prefix: str, requested: str | None = None) -> Path:
    if requested:
        path = Path(requested).expanduser().resolve()
    else:
        path = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=runtime_directory("artifacts")))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _json_output(args: argparse.Namespace) -> bool:
    return getattr(args, "format", "text") == "json"


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _adb_error(exc: AdbCommandError) -> int:
    print(str(exc), file=sys.stderr)
    return exc.returncode or 1


def _write_or_print(content: str, output: str | None) -> None:
    if output:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    else:
        print(content, end="")


def command_devices(args: argparse.Namespace) -> int:
    try:
        devices = list_adb_devices()
    except AdbCommandError as exc:
        return _adb_error(exc)
    payload = {
        "devices": [
            {
                "serial": device.serial,
                "status": device.status,
                "transport": device.transport,
                "detail": device.detail,
            }
            for device in devices
        ]
    }
    if _json_output(args):
        _print_json(payload)
    else:
        for device in devices:
            print(f"{device.serial}\t{device.status}\t{device.transport}")
    return 0 if any(
        device.status == "device"
        for device in devices
    ) else 1


def command_doctor(args: argparse.Namespace) -> int:
    adb_path = shutil.which(os.environ.get("ANDROID_USE_ADB", "adb"))
    try:
        version = adb_version_text()
        devices = list_adb_devices()
    except AdbCommandError as exc:
        version = ""
        devices = []
        adb_error = str(exc)
    else:
        adb_error = None

    selected = None
    selection_error = None
    try:
        selected = resolve_serial(args.serial)
    except SystemExit as exc:
        selection_error = str(exc)

    properties: dict[str, str] = {}
    if selected:
        for key in ("ro.product.model", "ro.build.version.release", "ro.build.version.sdk"):
            result = run_adb_shell(selected, ["getprop", key], timeout=3.0, check=False)
            properties[key] = result.stdout.strip()

    ui_check = None
    if args.check_ui and selected:
        started = time.perf_counter()
        try:
            page = snapshot(
                selected,
                timeout=args.timeout,
                include_activity=True,
            )
            ui_check = {
                "ok": True,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "nodes": sum(1 for _ in iter_nodes(page.root)),
                "package": page.package,
                "activity": page.activity,
            }
        except Exception as exc:
            ui_check = {
                "ok": False,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "error": str(exc),
            }

    payload = {
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "adb": {
            "ok": bool(adb_path and not adb_error),
            "path": adb_path,
            "version": version,
            "error": adb_error,
        },
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "supported": sys.version_info >= (3, 9),
            "thirdPartyRuntimePackagesRequired": False,
        },
        "tools": {
            "mitmdump": {
                "available": shutil.which("mitmdump") is not None,
                "path": shutil.which("mitmdump"),
            },
        },
        "devices": [
            {
                "serial": device.serial,
                "status": device.status,
                "transport": device.transport,
                "detail": device.detail,
            }
            for device in devices
        ],
        "selectedDevice": selected,
        "selectionError": selection_error,
        "deviceProperties": properties,
        "uiDump": ui_check,
    }
    ok = bool(
        payload["adb"]["ok"]
        and payload["python"]["supported"]
        and selected
        and (not args.check_ui or (ui_check and ui_check["ok"]))
    )
    payload["ok"] = ok
    if _json_output(args):
        _print_json(payload)
    else:
        print(
            f"doctor ok={str(ok).lower()} adb={adb_path or '-'} "
            f"device={selected or '-'} python={platform.python_version()}"
        )
        if selection_error:
            print(selection_error, file=sys.stderr)
        if ui_check:
            print(
                f"uiDump ok={str(ui_check['ok']).lower()} "
                f"elapsedMs={ui_check['elapsedMs']}"
            )
    return 0 if ok else 1


def command_adb(args: argparse.Namespace) -> int:
    serial = _serial(args)
    forwarded = list(args.adb_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if not forwarded:
        print("android-use adb requires arguments after '--'", file=sys.stderr)
        return 2
    command = [adb_executable(), "-s", serial, *forwarded]
    try:
        completed = subprocess.run(command, timeout=args.timeout)
    except FileNotFoundError:
        print(f"adb executable not found: {command[0]}", file=sys.stderr)
        return 127
    except subprocess.TimeoutExpired:
        print(f"adb command timed out after {args.timeout:.1f}s", file=sys.stderr)
        return 124
    return completed.returncode


def command_current_app(args: argparse.Namespace) -> int:
    serial = _serial(args)
    app = current_app(serial, timeout=args.timeout)
    if _json_output(args):
        _print_json(app)
    else:
        print(f"package={app['package'] or '-'} activity={app['activity'] or '-'}")
    return 0


def command_dump(args: argparse.Namespace) -> int:
    serial = _serial(args)
    patterns = []
    for value in args.keep_id_regex:
        try:
            patterns.append(re.compile(value))
        except re.error as exc:
            print(f"invalid --keep-id-regex {value!r}: {exc}", file=sys.stderr)
            return 2
    try:
        page = snapshot(serial, compressed=args.compressed, timeout=args.timeout)
    except (AdbCommandError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    content = clean_tree_text(page.root, page.package, patterns)
    if args.raw_out:
        raw_path = Path(args.raw_out).expanduser().resolve()
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(page.xml, encoding="utf-8")
    if _json_output(args):
        payload = {
            "schemaVersion": 1,
            "ok": True,
            "operation": "dump",
            "device": _device_payload(args),
            "package": page.package,
            "activity": page.activity,
            "elapsedMs": round(page.elapsed_ms, 1),
            "nodes": sum(1 for _ in iter_nodes(page.root)),
            "tree": content,
            "rawFile": str(Path(args.raw_out).resolve()) if args.raw_out else None,
        }
        if args.out:
            Path(args.out).expanduser().resolve().write_text(content, encoding="utf-8")
            payload["treeFile"] = str(Path(args.out).expanduser().resolve())
        _print_json(payload)
    else:
        _write_or_print(content, args.out)
        if args.stats:
            print(
                f"total_ms={page.elapsed_ms:.1f} backend=auto-uiautomator "
                f"package={page.package or '-'} nodes={sum(1 for _ in iter_nodes(page.root))}",
                file=sys.stderr,
            )
    return 0


def _capture_raw(serial: str, timeout: float = 10.0) -> tuple[RawScreenshot, float]:
    started = time.perf_counter()
    result = run_adb_bytes(
        ["exec-out", "screencap"],
        serial=serial,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise AdbCommandError(
            [adb_executable(), "-s", serial, "exec-out", "screencap"],
            result.returncode,
            result.stdout,
            result.stderr,
        )
    screenshot = parse_raw_screenshot(result.stdout)
    return screenshot, (time.perf_counter() - started) * 1000


def _save_screenshot(
    serial: str,
    output: Path,
    timeout: float = 10.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = run_adb_bytes(
        ["exec-out", "screencap", "-p"],
        serial=serial,
        timeout=timeout,
        check=False,
    )
    capture_ms = (time.perf_counter() - started) * 1000
    if result.returncode != 0:
        raise AdbCommandError(
            [adb_executable(), "-s", serial, "exec-out", "screencap", "-p"],
            result.returncode,
            result.stdout,
            result.stderr,
        )
    width, height = png_dimensions(result.stdout)
    write_started = time.perf_counter()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.stdout)
    write_ms = (time.perf_counter() - write_started) * 1000
    return {
        "path": str(output.resolve()),
        "width": width,
        "height": height,
        "captureMs": round(capture_ms, 1),
        "writeMs": round(write_ms, 1),
    }


def command_screenshot(args: argparse.Namespace) -> int:
    serial = _serial(args)
    if args.out:
        output = Path(args.out).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
    else:
        output = _artifact_directory("screenshot") / "screenshot.png"
    try:
        artifact = _save_screenshot(serial, output, timeout=args.timeout)
    except (AdbCommandError, ValueError, OSError) as exc:
        print(f"screenshot failed: {exc}", file=sys.stderr)
        return 1

    image_line = (
        f"screenshot={artifact['path']} size={artifact['width']}x{artifact['height']} "
        f"captureMs={artifact['captureMs']:.1f} writeMs={artifact['writeMs']:.1f}"
    )
    if not _json_output(args):
        print(image_line, flush=True)

    if _json_output(args):
        _print_json(
            {
                "schemaVersion": 1,
                "ok": True,
                "operation": "screenshot",
                "device": _device_payload(args),
                "image": artifact,
            }
        )
    return 0


def _candidate_payload(
    page: UISnapshot,
    target: str | None,
    contains: bool,
) -> dict[str, Any] | None:
    if not target:
        return None
    matches = find_target_nodes(
        page.root,
        target,
        package_name=page.package,
        contains=contains,
    )
    actionable = [
        node
        for node in iter_nodes(page.root)
        if node.attrib.get("clickable") == "true"
    ][:10]
    return {
        "query": target,
        "matches": [node_payload(node) for node in matches[:10]],
        "actionableNodes": [node_payload(node) for node in actionable],
    }


def collect_failure_evidence(
    *,
    serial: str,
    operation: str,
    reason: str,
    target: str | None = None,
    contains: bool = False,
    last_page: UISnapshot | None = None,
    budget: float = 3.0,
) -> Path:
    output = _artifact_directory(f"{operation}-failure")
    started = time.monotonic()
    page = last_page
    screenshot_artifact = None

    def collect_page() -> UISnapshot:
        return snapshot(serial, timeout=max(1.0, budget))

    def collect_image() -> dict[str, Any]:
        return _save_screenshot(serial, output / "screenshot.png", timeout=max(1.0, budget))

    with ThreadPoolExecutor(max_workers=2) as executor:
        page_future = executor.submit(collect_page) if page is None else None
        image_future = executor.submit(collect_image)
        futures = [future for future in (page_future, image_future) if future is not None]
        done, _ = wait_futures(futures, timeout=max(0.1, budget))
        if page_future is not None and page_future in done:
            try:
                page = page_future.result()
            except Exception:
                page = None
        if image_future in done:
            try:
                screenshot_artifact = image_future.result()
            except Exception:
                screenshot_artifact = None

    try:
        metrics = display_metrics(serial, timeout=max(0.2, budget - (time.monotonic() - started)))
        metrics_payload = {
            "physical": [metrics.physical_width, metrics.physical_height],
            "input": [metrics.input_width, metrics.input_height],
            "rotation": metrics.rotation,
        }
    except Exception as exc:
        metrics_payload = {"error": str(exc)}
    try:
        app = current_app(serial, timeout=1.0)
    except Exception as exc:
        app = {"error": str(exc)}

    tree_path = None
    raw_path = None
    candidates = None
    if page is not None:
        tree_path = output / "page-tree.txt"
        tree_path.write_text(clean_tree_text(page.root, page.package), encoding="utf-8")
        raw_path = output / "page.xml"
        raw_path.write_text(page.xml, encoding="utf-8")
        candidates = _candidate_payload(page, target, contains)
    log = active_status(serial)
    payload = {
        "schemaVersion": 1,
        "operation": operation,
        "reason": reason,
        "elapsedMs": round((time.monotonic() - started) * 1000, 1),
        "device": {"serial": serial, "display": metrics_payload},
        "currentApp": app,
        "page": {
            "package": page.package if page else None,
            "activity": page.activity if page else None,
            "target": candidates,
        },
        "artifacts": {
            "screenshot": screenshot_artifact["path"] if screenshot_artifact else None,
            "tree": str(tree_path) if tree_path else None,
            "rawXml": str(raw_path) if raw_path else None,
            "logcat": log.get("logFile") if log else None,
        },
        "partial": page is None or screenshot_artifact is None,
    }
    summary = output / "diagnostics.json"
    summary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


class ScrollOperationError(RuntimeError):
    def __init__(self, message: str, page: UISnapshot):
        super().__init__(message)
        self.page = page


def _scroll_to(
    *,
    serial: str,
    target: str,
    contains: bool,
    match_index: int,
    container: str | None,
    container_index: int,
    direction: str,
    attempts: int,
    duration: float,
    verify_delay: float,
    stop_on_no_progress: bool,
) -> tuple[Any, UISnapshot, int]:
    previous = None
    last_page = None
    for attempt in range(attempts + 1):
        page = snapshot(serial)
        last_page = page
        try:
            matches = find_target_nodes(
                page.root,
                target,
                package_name=page.package,
                contains=contains,
            )
            if matches:
                return (
                    select_target(matches, target, match_index=match_index),
                    page,
                    attempt,
                )
            if attempt >= attempts:
                break
            current = fingerprint(page)
            if stop_on_no_progress and previous is not None and current == previous:
                break
            previous = current
            scroll_node = select_scroll_node(
                page.root,
                page.package,
                container,
                container_index,
            )
            resolved_direction = relative_scroll_direction(direction, scroll_node)
            x1, y1, x2, y2 = swipe_points(resolved_direction, node_rect(scroll_node))
            result = run_adb_shell(
                serial,
                [
                    "input",
                    "swipe",
                    str(x1),
                    str(y1),
                    str(x2),
                    str(y2),
                    str(max(1, round(duration * 1000))),
                ],
                timeout=5.0,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "adb swipe failed")
            if verify_delay > 0:
                time.sleep(verify_delay)
        except Exception as exc:
            raise ScrollOperationError(str(exc), page) from exc
    assert last_page is not None
    raise ScrollOperationError(
        f"target not found after {attempts} scroll attempt(s): {target!r}",
        last_page,
    )


def command_scroll_to(args: argparse.Namespace) -> int:
    serial = _serial(args)
    try:
        node, page, attempts = _scroll_to(
            serial=serial,
            target=args.text,
            contains=args.contains,
            match_index=args.match_index,
            container=args.container,
            container_index=args.container_index,
            direction=args.direction,
            attempts=args.attempts,
            duration=args.duration,
            verify_delay=args.verify_delay,
            stop_on_no_progress=args.stop_on_no_progress,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        if not args.no_evidence:
            last_page = exc.page if isinstance(exc, ScrollOperationError) else None
            evidence = collect_failure_evidence(
                serial=serial,
                operation="scroll-to",
                reason=str(exc),
                target=args.text,
                contains=args.contains,
                last_page=last_page,
            )
            print(f"evidence={evidence}", file=sys.stderr)
        return 1
    payload = {
        "target": args.text,
        "attempts": attempts,
        "node": node_payload(node),
        "pageElapsedMs": round(page.elapsed_ms, 1),
    }
    if _json_output(args):
        _print_json(payload)
    else:
        print(
            f"found target={args.text!r} bounds={format_rect(node_rect(node))} "
            f"attempts={attempts}"
        )
    return 0


def command_tap(args: argparse.Namespace) -> int:
    serial = _serial(args)
    target = args.text
    raw_coordinates = args.y is not None
    if not target and args.target_or_x is not None and not raw_coordinates:
        target = args.target_or_x
    modes = sum(
        bool(value)
        for value in (
            target,
            raw_coordinates,
            args.normalized is not None,
            args.from_screenshot is not None,
        )
    )
    if modes != 1 or (args.text and args.target_or_x is not None):
        print(
            "tap requires exactly one target, x y pair, --normalized X Y, "
            "or --from-screenshot IMAGE X Y",
            file=sys.stderr,
        )
        return 2

    before_page = None
    source: dict[str, Any]
    try:
        metrics = display_metrics(serial)
        input_size = (metrics.input_width, metrics.input_height)
        if target:
            if args.scroll_if_needed:
                node, before_page, attempts = _scroll_to(
                    serial=serial,
                    target=target,
                    contains=args.contains,
                    match_index=args.match_index,
                    container=args.container,
                    container_index=args.container_index,
                    direction=args.scroll_direction,
                    attempts=args.attempts,
                    duration=args.scroll_duration,
                    verify_delay=args.verify_delay,
                    stop_on_no_progress=True,
                )
            else:
                before_page = snapshot(
                    serial,
                    include_activity=args.verify_change,
                )
                matches = find_target_nodes(
                    before_page.root,
                    target,
                    package_name=before_page.package,
                    contains=args.contains,
                )
                node = select_target(
                    matches,
                    target,
                    match_index=args.match_index,
                )
                attempts = 0
            x, y = node_rect(node).center
            source = {
                "type": "target",
                "query": target,
                "scrollAttempts": attempts,
                "selected": node_payload(node),
            }
        elif raw_coordinates:
            x, y = int(args.target_or_x), int(args.y)
            source = {"type": "input", "point": [x, y]}
        elif args.normalized is not None:
            x, y = normalized_to_input(
                args.normalized[0],
                args.normalized[1],
                *input_size,
            )
            source = {
                "type": "normalized",
                "point": list(args.normalized),
            }
        else:
            image_path, raw_x, raw_y = args.from_screenshot
            source_size = image_dimensions(image_path)
            source_point = (float(raw_x), float(raw_y))
            x, y = screenshot_to_input(
                *source_point,
                source_size,
                input_size,
            )
            source = {
                "type": "screenshot",
                "image": str(Path(image_path).expanduser().resolve()),
                "point": list(source_point),
                "screenshotSize": list(source_size),
            }
        if not 0 <= x < input_size[0] or not 0 <= y < input_size[1]:
            raise ValueError(
                f"tap point ({x},{y}) is outside input size {input_size[0]}x{input_size[1]}"
            )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        if before_page is None and isinstance(exc, ScrollOperationError):
            before_page = exc.page
        if target and not args.no_evidence:
            evidence = collect_failure_evidence(
                serial=serial,
                operation="tap",
                reason=str(exc),
                target=target,
                contains=args.contains,
                last_page=before_page,
            )
            print(f"evidence={evidence}", file=sys.stderr)
        return 1

    if args.verify_change and before_page is None:
        try:
            before_page = snapshot(serial, include_activity=True)
        except Exception as exc:
            print(f"cannot capture pre-tap UI: {exc}", file=sys.stderr)
            return 1
    result = run_adb_shell(
        serial,
        ["input", "tap", str(x), str(y)],
        timeout=5.0,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "adb tap failed", file=sys.stderr)
        return result.returncode or 1

    mark_path = None
    if args.mark:
        try:
            screenshot, _ = _capture_raw(serial)
            mark_x, mark_y = screenshot_to_input(
                x,
                y,
                input_size,
                (screenshot.width, screenshot.height),
            )
            write_png(
                marked_screenshot(screenshot, (mark_x, mark_y)),
                args.mark,
            )
            mark_path = str(Path(args.mark).expanduser().resolve())
        except Exception as exc:
            print(f"warning: cannot write marked screenshot: {exc}", file=sys.stderr)

    changed = None
    after_page = None
    if args.verify_change or args.dump:
        if args.verify_delay > 0:
            time.sleep(args.verify_delay)
        try:
            after_page = snapshot(
                serial,
                include_activity=args.verify_change,
            )
        except Exception as exc:
            print(f"cannot capture post-tap UI: {exc}", file=sys.stderr)
            return 1
        if args.verify_change and before_page is not None:
            changed = (
                before_page.activity != after_page.activity
                or fingerprint(before_page) != fingerprint(after_page)
            )
            if not changed:
                reason = "activity and UI tree did not change after tap"
                print(reason, file=sys.stderr)
                if not args.no_evidence:
                    evidence = collect_failure_evidence(
                        serial=serial,
                        operation="tap",
                        reason=reason,
                        target=target,
                        contains=args.contains,
                        last_page=after_page,
                    )
                    print(f"evidence={evidence}", file=sys.stderr)
                return 1

    payload = {
        "input": [x, y],
        "inputSize": list(input_size),
        "physicalSize": [metrics.physical_width, metrics.physical_height],
        "rotation": metrics.rotation,
        "source": source,
        "markedScreenshot": mark_path,
        "changed": changed,
    }
    if _json_output(args):
        _print_json(payload)
    else:
        print(
            f"tapped input=({x},{y}) size={input_size[0]}x{input_size[1]} "
            f"source={source['type']}"
        )
        if mark_path:
            print(f"markedScreenshot={mark_path}")
        if changed is not None:
            print(f"changed={str(changed).lower()}")
        if args.dump and after_page is not None:
            print(clean_tree_text(after_page.root, after_page.package), end="")
    return 0


def _node_state_matches(node, state: str | None) -> bool:
    if state is None:
        return True
    if state == "enabled":
        return node.attrib.get("enabled") != "false"
    if state == "disabled":
        return node.attrib.get("enabled") == "false"
    if state == "selected":
        return node.attrib.get("selected") == "true"
    if state == "checked":
        return node.attrib.get("checked") == "true"
    if state == "unchecked":
        return node.attrib.get("checkable") == "true" and node.attrib.get("checked") != "true"
    return False


def _screen_change_ratio(
    before: RawScreenshot,
    after: RawScreenshot,
    *,
    channel_threshold: int = 16,
) -> float:
    if (before.width, before.height) != (after.width, after.height):
        return 1.0
    top = round(before.height * 0.08)
    bottom = round(before.height * 0.95)
    x_step = max(4, before.width // 120)
    y_step = max(4, before.height // 180)
    changed = 0
    sampled = 0
    for y in range(top, bottom, y_step):
        row = y * before.width * 4
        for x in range(0, before.width, x_step):
            offset = row + x * 4
            sampled += 1
            if max(
                abs(before.rgba[offset + channel] - after.rgba[offset + channel])
                for channel in range(3)
            ) > channel_threshold:
                changed += 1
    return changed / sampled if sampled else 1.0


def command_wait(args: argparse.Namespace) -> int:
    serial = _serial(args)
    started = time.monotonic()
    deadline = started + args.timeout
    last_page = None
    last_state: dict[str, Any] = {}
    observed = False
    stable_since = None
    previous_screen = None
    log_path_value = None
    log_position = 0
    log_pattern = None
    if args.log:
        try:
            log_path_value, log_position, _ = log_offset(serial)
            log_pattern = re.compile(args.log)
        except (RuntimeError, re.error) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    activity_pattern = None
    if args.activity:
        try:
            activity_pattern = re.compile(args.activity)
        except re.error as exc:
            print(f"invalid --activity regex: {exc}", file=sys.stderr)
            return 2

    while True:
        now = time.monotonic()
        success = False
        description = ""
        try:
            if args.text or args.text_gone:
                last_page = snapshot(serial)
                target = args.text or args.text_gone
                matches = find_target_nodes(
                    last_page.root,
                    target,
                    package_name=last_page.package,
                    contains=args.contains,
                )
                observed = observed or bool(matches)
                last_state = {
                    "matchCount": len(matches),
                    "pageElapsedMs": round(last_page.elapsed_ms, 1),
                    "candidates": [node_payload(node) for node in matches[:5]],
                }
                if args.text and args.match_index < len(matches):
                    selected = matches[args.match_index]
                    success = _node_state_matches(selected, args.state)
                    if success:
                        description = (
                            f"target={args.text!r} bounds={format_rect(node_rect(selected))}"
                        )
                elif args.text_gone:
                    success = not matches and (observed or not args.after_seen)
                    if success:
                        description = f"target gone: {args.text_gone!r}"
            elif activity_pattern:
                app = current_app(serial)
                value = f"{app['package']}/{app['activity']}"
                last_state = {"currentApp": app}
                success = bool(activity_pattern.search(value))
                description = f"activity={value}"
            elif log_pattern and log_path_value:
                text, log_position = read_log_since(log_path_value, log_position)
                match = log_pattern.search(text)
                last_state = {
                    "logFile": str(log_path_value),
                    "bytesObserved": len(text.encode("utf-8")),
                    "collector": active_status(serial),
                }
                success = match is not None
                if match:
                    description = f"log matched: {match.group(0)!r}"
            elif args.screen_stable is not None:
                screenshot_value, capture_ms = _capture_raw(serial)
                change_ratio = (
                    _screen_change_ratio(previous_screen, screenshot_value)
                    if previous_screen is not None
                    else 1.0
                )
                if previous_screen is None or change_ratio > args.change_threshold:
                    stable_since = time.monotonic()
                previous_screen = screenshot_value
                stable_for = (
                    time.monotonic() - stable_since
                    if stable_since is not None
                    else 0.0
                )
                last_state = {
                    "captureMs": round(capture_ms, 1),
                    "stableFor": round(stable_for, 3),
                    "changeRatio": round(change_ratio, 6),
                }
                success = stable_for >= args.screen_stable
                description = f"screen stable for {stable_for:.2f}s"
        except Exception as exc:
            last_state = {"error": str(exc)}

        if success:
            elapsed = time.monotonic() - started
            payload = {
                "ok": True,
                "elapsedMs": round(elapsed * 1000, 1),
                "description": description,
                "lastState": last_state,
            }
            if _json_output(args):
                _print_json(payload)
            else:
                print(f"wait satisfied after {elapsed:.2f}s: {description}")
            return 0
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(args.interval, max(0.0, deadline - now)))

    reason = f"wait timed out after {args.timeout:.1f}s"
    payload = {
        "ok": False,
        "elapsedMs": round((time.monotonic() - started) * 1000, 1),
        "lastState": last_state,
    }
    if _json_output(args):
        if not args.no_evidence:
            evidence = collect_failure_evidence(
                serial=serial,
                operation="wait",
                reason=reason,
                target=args.text or args.text_gone,
                contains=args.contains,
                last_page=last_page,
            )
            payload["evidence"] = str(evidence)
        _print_json(payload)
    else:
        print(f"{reason}: lastState={json.dumps(last_state, ensure_ascii=False)}", file=sys.stderr)
        if not args.no_evidence:
            evidence = collect_failure_evidence(
                serial=serial,
                operation="wait",
                reason=reason,
                target=args.text or args.text_gone,
                contains=args.contains,
                last_page=last_page,
            )
            print(f"evidence={evidence}", file=sys.stderr)
    return 1


def _wait_activity_assertion(
    serial: str,
    pattern: str,
    deadline: float,
) -> tuple[bool, dict[str, Any]]:
    regex = re.compile(pattern)
    last = {}
    while time.monotonic() < deadline:
        last = current_app(serial)
        value = f"{last['package']}/{last['activity']}"
        if regex.search(value):
            return True, {"expected": pattern, "actual": value}
        time.sleep(0.2)
    return False, {"expected": pattern, "actual": f"{last.get('package', '')}/{last.get('activity', '')}"}


def _wait_log_assertion(
    serial: str,
    pattern: str,
    path: Path,
    offset: int,
    deadline: float,
) -> tuple[bool, dict[str, Any]]:
    regex = re.compile(pattern)
    last_match = None
    while time.monotonic() < deadline:
        text, offset = read_log_since(path, offset)
        match = regex.search(text)
        if match:
            last_match = match.group(0)
            return True, {
                "expected": pattern,
                "match": last_match,
                "logFile": str(path),
            }
        time.sleep(0.1)
    return False, {
        "expected": pattern,
        "match": last_match,
        "logFile": str(path),
        "collector": active_status(serial),
    }


def _wait_text_assertion(
    serial: str,
    target: str,
    deadline: float,
) -> tuple[bool, dict[str, Any], UISnapshot | None]:
    last_count = 0
    last_page = None
    while time.monotonic() < deadline:
        page = snapshot(serial, timeout=max(1.0, deadline - time.monotonic()))
        last_page = page
        matches = find_target_nodes(
            page.root,
            target,
            package_name=page.package,
        )
        last_count = len(matches)
        if matches:
            return (
                True,
                {
                    "expected": target,
                    "matchCount": last_count,
                    "candidate": node_payload(matches[0]),
                },
                page,
            )
    return False, {"expected": target, "matchCount": last_count}, last_page


def command_deeplink(args: argparse.Namespace) -> int:
    serial = _serial(args)
    log_reference = None
    if args.expect_log:
        try:
            log_reference = log_offset(serial)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    command = ["am", "start"]
    if args.wait:
        command.append("-W")
    command.extend(["-a", "android.intent.action.VIEW", "-d", args.uri])
    result = run_adb_shell(
        serial,
        command,
        timeout=args.timeout,
        check=False,
    )
    output = result.stdout or result.stderr
    transport_ok = result.returncode == 0 and "Error:" not in output
    checks = []
    verification = "unknown"
    assertion_page = None
    if transport_ok and (args.expect_activity or args.expect_log or args.expect_text):
        deadline = time.monotonic() + args.assert_timeout
        try:
            if args.expect_activity:
                ok, detail = _wait_activity_assertion(
                    serial,
                    args.expect_activity,
                    deadline,
                )
                checks.append({"type": "activity", "ok": ok, **detail})
            if args.expect_log and log_reference:
                path, offset, _ = log_reference
                ok, detail = _wait_log_assertion(
                    serial,
                    args.expect_log,
                    path,
                    offset,
                    deadline,
                )
                checks.append({"type": "log", "ok": ok, **detail})
            if args.expect_text:
                ok, detail, assertion_page = _wait_text_assertion(
                    serial,
                    args.expect_text,
                    deadline,
                )
                checks.append({"type": "text", "ok": ok, **detail})
        except Exception as exc:
            checks.append({"type": "internal", "ok": False, "error": str(exc)})
        verification = "verified" if checks and all(check["ok"] for check in checks) else "failed"
    elif not transport_ok:
        verification = "failed"

    payload = {
        "schemaVersion": 1,
        "ok": transport_ok and verification != "failed",
        "operation": "deeplink",
        "device": _device_payload(args),
        "transport": {
            "status": "succeeded" if transport_ok else "failed",
            "exitCode": result.returncode,
            "output": output,
        },
        "verification": {"status": verification, "checks": checks},
        "warnings": (
            ["Intent transport succeeded without a business assertion."]
            if transport_ok and verification == "unknown"
            else []
        ),
    }
    if verification == "failed" and not args.no_evidence:
        evidence = collect_failure_evidence(
            serial=serial,
            operation="deeplink",
            reason="explicit DeepLink assertion failed",
            target=args.expect_text,
            last_page=assertion_page,
        )
        payload["evidence"] = str(evidence)
    if _json_output(args):
        _print_json(payload)
    else:
        print(output, end="" if output.endswith("\n") else "\n")
        print(f"verification={verification}")
        for check in checks:
            print(
                f"check type={check['type']} ok={str(check['ok']).lower()} "
                f"detail={json.dumps(check, ensure_ascii=False, separators=(',', ':'))}"
            )
        if payload.get("evidence"):
            print(f"evidence={payload['evidence']}", file=sys.stderr)
    return 0 if payload["ok"] else (result.returncode or 1)


def command_logcat(args: argparse.Namespace) -> int:
    serial = _serial(args)
    try:
        if args.logcat_action == "start":
            payload = start_logcat(
                serial,
                package=args.package,
                clear=args.clear,
                timeout=args.timeout,
            )
            if _json_output(args):
                _print_json(payload)
            else:
                print(payload["logFile"])
                print(format_status(payload))
            return 0
        if args.logcat_action == "status":
            payload = collector_status(state_path(serial))
            if _json_output(args):
                _print_json(payload)
            else:
                print(format_status(payload))
                for warning in payload.get("warnings") or []:
                    print(f"warning: {warning}", file=sys.stderr)
            return 0 if payload.get("healthy") else 1
        if args.logcat_action == "stop":
            payload = stop_logcat(serial, timeout=args.timeout)
            if _json_output(args):
                _print_json(payload)
            else:
                if payload.get("logFile"):
                    print(payload["logFile"])
                print(format_status(payload))
            return 0 if not payload.get("healthy") else 1
    except (AdbCommandError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"unsupported logcat action: {args.logcat_action}", file=sys.stderr)
    return 2


def command_capture(args: argparse.Namespace) -> int:
    serial = _serial(args)
    try:
        trigger = parse_trigger(args.trigger)
        output = _artifact_directory("capture", args.out)
        manifest = run_capture(
            serial=serial,
            output=output,
            fps=args.fps,
            duration=args.duration,
            trigger=trigger,
            before=args.before,
            after=args.after,
            trigger_timeout=args.timeout,
            trigger_interval=args.trigger_interval,
            contains=args.contains,
            allow_already_gone=args.allow_already_gone,
            collect_logcat=not args.no_logcat,
        )
    except Exception as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        return 1
    ok = (
        manifest["framesValid"] > 0
        and manifest["actualFps"] >= min(6.0, args.fps * 0.85)
        and (
            manifest["trigger"] is None
            or manifest["trigger"]["triggered"]
        )
    )
    if _json_output(args):
        _print_json({"ok": ok, **manifest})
    else:
        print(
            f"capture={manifest['manifest']} frames={manifest['framesValid']} "
            f"requestedFps={manifest['requestedFps']:g} actualFps={manifest['actualFps']:.2f} "
            f"deliveryFps={manifest['deliveryFps']:.2f}"
        )
        if manifest["contactSheet"]:
            print(f"contactSheet={manifest['contactSheet']}")
        for error in manifest["errors"]:
            print(f"warning: {error}", file=sys.stderr)
    return 0 if ok else 1


def command_proxy(args: argparse.Namespace) -> int:
    from .proxy_server import (
        cert_proxy,
        doctor_proxy,
        dump_proxy,
        mock_proxy,
        start_proxy,
        status_proxy,
        stop_proxy,
    )

    if args.proxy_action == "start":
        return start_proxy(args)
    if args.proxy_action == "stop":
        return stop_proxy(args)
    if args.proxy_action == "status":
        return status_proxy(args)
    if args.proxy_action == "doctor":
        return doctor_proxy(args)
    if args.proxy_action == "dump":
        return dump_proxy(args)
    if args.proxy_action == "mock":
        return mock_proxy(args)
    if args.proxy_action == "cert":
        return cert_proxy(args)
    print(f"unsupported proxy action: {args.proxy_action}", file=sys.stderr)
    return 2


REMOVED_COMMANDS = {
    "daemon": "No daemon is required; run the retained command directly.",
    "capabilities": "Run 'android-use doctor' for host and device capabilities.",
    "packages": "Use: android-use adb -- shell pm list packages",
    "start-app": "Use: android-use adb -- shell monkey -p PACKAGE -c android.intent.category.LAUNCHER 1",
    "stop-app": "Use: android-use adb -- shell am force-stop PACKAGE",
    "press": "Use: android-use adb -- shell input keyevent KEYCODE",
    "type": "Use: android-use adb -- shell input text TEXT (ASCII/basic text only).",
    "swipe": "Use: android-use adb -- shell input swipe X1 Y1 X2 Y2 DURATION_MS",
    "media": "Use: android-use adb -- push LOCAL /sdcard/Pictures/",
    "waitFor": "Use: android-use wait --text TEXT",
}


def command_removed(args: argparse.Namespace) -> int:
    command = args.command
    print(f"android-use {command} was removed. {REMOVED_COMMANDS[command]}", file=sys.stderr)
    return 2
