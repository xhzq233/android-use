"""Argument parsing and dispatch for the source-distributed android-use CLI."""

from __future__ import annotations

import argparse
import os
import sys

from .android_device import resolve_device
from .command_handlers import (
    REMOVED_COMMANDS,
    command_adb,
    command_capture,
    command_current_app,
    command_deeplink,
    command_devices,
    command_doctor,
    command_dump,
    command_logcat,
    command_proxy,
    command_removed,
    command_screenshot,
    command_scroll_to,
    command_tap,
    command_wait,
)
from .logcat_commands import action_marker
from .proxy_device import MITMPROXY_CA_SDCARD_PATH
from .skill_commands import command_skill_install
from .visual_capture import duration_arg


def add_serial(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-s",
        "--serial",
        help="ADB serial (USB, emulator, or network); default: $ANDROID_USE_SERIAL or the only online device",
    )


def add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["text", "json"], default="text")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="android-use",
        description="Android evidence and semantic UI helper backed by ADB",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    skill = sub.add_parser("skill", help="install the agent Skill (no device needed)")
    skill_sub = skill.add_subparsers(dest="skill_action", required=True)
    skill_install = skill_sub.add_parser("install", help="copy Skill documents into project or user skills")
    scope = skill_install.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", nargs="?", const=".", metavar="DIR", help="install under DIR/.agents/skills (default DIR: current directory)")
    scope.add_argument("--user", action="store_true", help="install under ~/.agents/skills")
    skill_install.add_argument("--force", action="store_true", help="overwrite existing bundled Skill documents")
    skill_install.set_defaults(func=command_skill_install, resolve_device=False)

    devices = sub.add_parser("devices", help="list ADB devices and transport types")
    add_format(devices)
    devices.set_defaults(func=command_devices, resolve_device=False)

    doctor = sub.add_parser("doctor", help="check Python, ADB, the selected device, and optional tools")
    add_serial(doctor)
    add_format(doctor)
    doctor.add_argument("--check-ui", action="store_true", help="also run one UI hierarchy dump")
    doctor.add_argument("--timeout", type=float, default=10.0)
    doctor.set_defaults(func=command_doctor, resolve_device=False)

    adb = sub.add_parser("adb", help="run raw ADB with android-use device selection")
    add_serial(adb)
    adb.add_argument("--timeout", type=float, default=60.0)
    adb.add_argument("adb_args", nargs=argparse.REMAINDER, help="arguments after --")
    adb.set_defaults(func=command_adb)

    current = sub.add_parser("current-app", help="print the foreground package and Activity")
    add_serial(current)
    add_format(current)
    current.add_argument("--timeout", type=float, default=10.0)
    current.set_defaults(func=command_current_app)

    dump = sub.add_parser("dump", help="print a cleaned UI hierarchy")
    add_serial(dump)
    add_format(dump)
    dump.add_argument(
        "--compressed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="request compressed hierarchy output (default: true)",
    )
    dump.add_argument("--timeout", type=float, default=10.0)
    dump.add_argument("-o", "--out")
    dump.add_argument("--raw-out")
    dump.add_argument("--keep-id-regex", action="append", default=[], metavar="REGEX")
    dump.add_argument("--stats", action="store_true")
    dump.set_defaults(func=command_dump)

    tap = sub.add_parser(
        "tap",
        help="tap a tree target, input point, normalized point, or screenshot point",
    )
    add_serial(tap)
    add_format(tap)
    tap.add_argument("target_or_x", nargs="?")
    tap.add_argument("y", nargs="?", type=int)
    tap.add_argument("--text")
    tap.add_argument("--normalized", nargs=2, type=float, metavar=("X", "Y"))
    tap.add_argument(
        "--from-screenshot",
        nargs=3,
        metavar=("IMAGE", "X", "Y"),
    )
    tap.add_argument("--contains", action="store_true")
    tap.add_argument("--match-index", type=int, default=0)
    tap.add_argument("--scroll-if-needed", action="store_true")
    tap.add_argument("--container")
    tap.add_argument("--container-index", type=int, default=0)
    tap.add_argument(
        "--scroll-direction",
        choices=["forth", "back", "up", "down", "left", "right"],
        default="forth",
    )
    tap.add_argument("--attempts", type=int, default=5)
    tap.add_argument("--scroll-duration", type=float, default=0.3)
    tap.add_argument("--mark", metavar="IMAGE")
    tap.add_argument("--verify-change", action="store_true")
    tap.add_argument("--verify-delay", type=float, default=0.5)
    tap.add_argument("--dump", action="store_true")
    tap.add_argument("--no-evidence", action="store_true")
    tap.set_defaults(func=command_tap)

    wait_parser = sub.add_parser(
        "wait",
        help="wait for UI text, Activity, active log, or a stable screen",
    )
    add_serial(wait_parser)
    add_format(wait_parser)
    condition = wait_parser.add_mutually_exclusive_group(required=True)
    condition.add_argument("--text")
    condition.add_argument("--text-gone")
    condition.add_argument("--activity", metavar="REGEX")
    condition.add_argument("--log", metavar="REGEX")
    condition.add_argument("--screen-stable", type=duration_arg, metavar="DURATION")
    wait_parser.add_argument(
        "--state",
        choices=["enabled", "disabled", "selected", "checked", "unchecked"],
    )
    wait_parser.add_argument("--after-seen", action="store_true")
    wait_parser.add_argument("--contains", action="store_true")
    wait_parser.add_argument("--match-index", type=int, default=0)
    wait_parser.add_argument("--timeout", type=duration_arg, default=8.0)
    wait_parser.add_argument("--interval", type=duration_arg, default=0.5)
    wait_parser.add_argument(
        "--change-threshold",
        type=float,
        default=0.003,
        help="maximum changed sampled-pixel ratio for --screen-stable (default: 0.003)",
    )
    wait_parser.add_argument("--no-evidence", action="store_true")
    wait_parser.set_defaults(func=command_wait)

    scroll = sub.add_parser("scroll-to", help="find a target in a UI-tree scroll container")
    add_serial(scroll)
    add_format(scroll)
    scroll.add_argument("--text", required=True)
    scroll.add_argument("--contains", action="store_true")
    scroll.add_argument("--match-index", type=int, default=0)
    scroll.add_argument("--container", help="selector, horizontal, or vertical")
    scroll.add_argument("--container-index", type=int, default=0)
    scroll.add_argument(
        "--direction",
        "--dir",
        choices=["forth", "back", "up", "down", "left", "right"],
        default="forth",
    )
    scroll.add_argument("--attempts", type=int, default=5)
    scroll.add_argument("--duration", type=float, default=0.3)
    scroll.add_argument("--verify-delay", type=float, default=0.5)
    scroll.add_argument(
        "--stop-on-no-progress",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    scroll.add_argument("--no-evidence", action="store_true")
    scroll.set_defaults(func=command_scroll_to)

    screenshot = sub.add_parser(
        "screenshot",
        help="save a native PNG screenshot",
    )
    add_serial(screenshot)
    add_format(screenshot)
    screenshot.add_argument("out", nargs="?")
    screenshot.add_argument("--timeout", type=float, default=10.0)
    screenshot.set_defaults(func=command_screenshot)

    def configure_capture(capture: argparse.ArgumentParser, alias: bool) -> None:
        add_serial(capture)
        add_format(capture)
        capture.add_argument("--fps", type=float, default=7.0)
        capture.add_argument("--duration", type=duration_arg, default=3.0)
        capture.add_argument("--out", metavar="DIR")
        capture.add_argument("--trigger", metavar="text:VALUE|text-gone:VALUE")
        capture.add_argument("--before", type=duration_arg, default=0.5)
        capture.add_argument("--after", type=duration_arg, default=3.0)
        capture.add_argument("--timeout", type=duration_arg, default=30.0)
        capture.add_argument("--trigger-interval", type=duration_arg, default=0.5)
        capture.add_argument("--contains", action="store_true")
        capture.add_argument("--allow-already-gone", action="store_true")
        capture.add_argument("--no-logcat", action="store_true")
        capture.set_defaults(func=command_capture, deprecated_alias=alias)

    configure_capture(
        sub.add_parser("capture", help="capture a 7 FPS short screenshot sequence"),
        False,
    )

    deeplink = sub.add_parser("deeplink", help="open a URI and optionally verify its result")
    add_serial(deeplink)
    add_format(deeplink)
    deeplink.add_argument("uri")
    deeplink.add_argument("--wait", action="store_true")
    deeplink.add_argument("--timeout", type=float, default=60.0)
    deeplink.add_argument("--expect-activity", metavar="REGEX")
    deeplink.add_argument("--expect-log", metavar="REGEX")
    deeplink.add_argument("--expect-text")
    deeplink.add_argument("--assert-timeout", type=duration_arg, default=10.0)
    deeplink.add_argument("--no-evidence", action="store_true")
    deeplink.set_defaults(func=command_deeplink)

    logcat = sub.add_parser("logcat", help="manage one persistent host-supervised logcat stream")
    logcat_sub = logcat.add_subparsers(dest="logcat_action", required=True)
    log_start = logcat_sub.add_parser("start")
    add_serial(log_start)
    add_format(log_start)
    log_start.add_argument("--package", help="label the file; still captures all device logs")
    log_start.add_argument("--clear", action="store_true")
    log_start.add_argument("--timeout", type=float, default=3.0)
    log_start.set_defaults(func=command_logcat)
    log_status = logcat_sub.add_parser("status")
    add_serial(log_status)
    add_format(log_status)
    log_status.set_defaults(func=command_logcat)
    log_stop = logcat_sub.add_parser("stop")
    add_serial(log_stop)
    add_format(log_stop)
    log_stop.add_argument("--timeout", type=float, default=5.0)
    log_stop.set_defaults(func=command_logcat)

    proxy = sub.add_parser("proxy", help="optional mitmdump-backed capture and mock")
    proxy_sub = proxy.add_subparsers(dest="proxy_action", required=True)
    proxy_start = proxy_sub.add_parser("start")
    add_serial(proxy_start)
    proxy_start.add_argument("--port", type=int, default=8080)
    proxy_start.add_argument("--host", default="127.0.0.1")
    proxy_start.add_argument("--mock")
    proxy_start.add_argument("--no-capture", action="store_true")
    proxy_start.add_argument("--timeout", type=float, default=10.0)
    proxy_start.add_argument("--no-device-proxy", action="store_true")
    proxy_start.add_argument("--no-reverse", action="store_true")
    proxy_start.add_argument("--skip-cert-check", action="store_true")
    proxy_stop = proxy_sub.add_parser("stop")
    add_serial(proxy_stop)
    proxy_stop.add_argument("--port", type=int, default=8080)
    proxy_stop.add_argument("--timeout", type=float, default=5.0)
    proxy_stop.add_argument("--no-restore-proxy", action="store_true")
    proxy_status = proxy_sub.add_parser("status")
    add_serial(proxy_status)
    proxy_status.add_argument("--timeout", type=float, default=1.0)
    proxy_doctor = proxy_sub.add_parser("doctor")
    add_serial(proxy_doctor)
    proxy_doctor.add_argument("--timeout", type=float, default=1.0)
    proxy_dump = proxy_sub.add_parser("dump")
    add_serial(proxy_dump)
    proxy_dump.add_argument("--format", choices=["text", "json", "har"], default="text")
    proxy_dump.add_argument("-o", "--out")
    proxy_dump.add_argument("--timeout", type=float, default=10.0)
    proxy_mock = proxy_sub.add_parser("mock")
    add_serial(proxy_mock)
    proxy_mock.add_argument("rules")
    proxy_mock.add_argument("--timeout", type=float, default=5.0)
    proxy_cert = proxy_sub.add_parser("cert")
    cert_sub = proxy_cert.add_subparsers(dest="cert_action", required=True)
    cert_status = cert_sub.add_parser("status")
    add_serial(cert_status)
    cert_instructions = cert_sub.add_parser("instructions")
    add_serial(cert_instructions)
    cert_push = cert_sub.add_parser("push")
    add_serial(cert_push)
    cert_push.add_argument("--cert")
    cert_push.add_argument("--device-path", default=MITMPROXY_CA_SDCARD_PATH)
    cert_root = cert_sub.add_parser("install-root")
    add_serial(cert_root)
    cert_root.add_argument("--cert")
    cert_root.add_argument("--device-path", default=MITMPROXY_CA_SDCARD_PATH)
    cert_root.add_argument("--reboot", action="store_true")
    proxy.set_defaults(func=command_proxy)

    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in REMOVED_COMMANDS:
        return command_removed(
            argparse.Namespace(command=raw_argv[0], removed_args=raw_argv[1:])
        )
    if raw_argv and raw_argv[0] == "capture-burst":
        print("warning: capture-burst was renamed to capture", file=sys.stderr)
        raw_argv[0] = "capture"
    args = build_parser().parse_args(raw_argv)
    if getattr(args, "resolve_device", True):
        try:
            device = resolve_device(getattr(args, "serial", None))
            args.resolved_serial = device.serial
            args.resolved_transport = device.transport
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            return 1
    serial = getattr(args, "resolved_serial", None)
    if args.command == "logcat":
        return args.func(args)
    with action_marker(serial, args.command):
        return args.func(args)
