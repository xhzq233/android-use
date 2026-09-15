#!/usr/bin/env python3
"""Repeatable wall-clock benchmark for the source android-use command surface."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Case:
    name: str
    arguments: list[str]
    iterations: int
    timeout: float = 30.0


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def run_case(cli: Path, case: Case) -> dict:
    samples = []
    failures = []
    for index in range(case.iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            [str(cli), *case.arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=case.timeout,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        samples.append(elapsed_ms)
        if completed.returncode != 0:
            failures.append(
                {
                    "index": index,
                    "exitCode": completed.returncode,
                    "stderr": completed.stderr.decode("utf-8", errors="replace")[-500:],
                }
            )
    return {
        "name": case.name,
        "iterations": case.iterations,
        "p50Ms": round(statistics.median(samples), 1),
        "p95Ms": round(percentile(samples, 0.95), 1),
        "minMs": round(min(samples), 1),
        "maxMs": round(max(samples), 1),
        "samplesMs": [round(value, 1) for value in samples],
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--serial", required=True)
    parser.add_argument("--known-text", help="visible stock-tree target used by wait and scroll-to")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--heavy-iterations", type=int, default=5)
    parser.add_argument("--out")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(__file__).resolve().parent
    cli = root / "android-use"
    serial = ["-s", args.serial]
    with tempfile.TemporaryDirectory(prefix="android-use-benchmark-") as temporary:
        artifacts = Path(temporary)
        cases = [
            Case("help", ["--help"], args.iterations),
            Case("devices", ["devices", "--format", "json"], args.iterations),
            Case("doctor", ["doctor", *serial, "--format", "json"], args.iterations),
            Case("adb-get-state", ["adb", *serial, "--", "get-state"], args.iterations),
            Case("current-app", ["current-app", *serial, "--format", "json"], args.iterations),
            Case(
                "dump",
                ["dump", *serial, "-o", str(artifacts / "dump.txt")],
                args.heavy_iterations,
            ),
            Case(
                "screenshot",
                [
                    "screenshot",
                    *serial,
                    str(artifacts / "screenshot.png"),
                ],
                args.heavy_iterations,
            ),
            Case(
                "capture-1s",
                [
                    "capture",
                    *serial,
                    "--duration",
                    "1s",
                    "--out",
                    str(artifacts / "capture"),
                    "--no-logcat",
                ],
                min(3, args.heavy_iterations),
            ),
            Case("proxy-doctor", ["proxy", "doctor", *serial, "--timeout", "1"], 1),
        ]
        if args.known_text:
            cases.extend(
                [
                    Case(
                        "wait-present",
                        [
                            "wait",
                            *serial,
                            "--text",
                            args.known_text,
                            "--timeout",
                            "5s",
                            "--no-evidence",
                        ],
                        args.heavy_iterations,
                    ),
                    Case(
                        "scroll-to-present",
                        ["scroll-to", *serial, "--text", args.known_text, "--no-evidence"],
                        args.heavy_iterations,
                    ),
                ]
            )

        results = [run_case(cli, case) for case in cases]
        subprocess.run(
            [str(cli), "logcat", "stop", *serial],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        log_start = Case(
            "logcat-start",
            ["logcat", "start", *serial, "--package", "benchmark"],
            1,
        )
        results.append(run_case(cli, log_start))
        results.append(
            run_case(
                cli,
                Case("logcat-status", ["logcat", "status", *serial], args.iterations),
            )
        )
        results.append(
            run_case(cli, Case("logcat-stop", ["logcat", "stop", *serial], 1))
        )

    payload = {
        "schemaVersion": 1,
        "serial": args.serial,
        "cli": str(cli),
        "results": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).expanduser().resolve().write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if any(result["failures"] for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
