from __future__ import annotations

import contextlib
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.capture_command import parse_trigger, run_capture
from android_use.cli_parser import main as cli_main
from android_use.visual_capture import (
    RawScreenshot,
    image_dimensions,
    marked_screenshot,
    normalized_to_input,
    parse_duration,
    parse_raw_screenshot,
    png_dimensions,
    screenshot_to_input,
    write_png,
)


def screenshot(width: int = 8, height: int = 12) -> RawScreenshot:
    return RawScreenshot(
        width=width,
        height=height,
        rgba=b"\x10\x20\x30\xff" * (width * height),
        pixel_format=1,
        color_space=1,
    )


class VisualCaptureTest(unittest.TestCase):
    def test_duration_and_coordinate_mapping(self) -> None:
        self.assertEqual(parse_duration("500ms"), 0.5)
        self.assertEqual(parse_duration("3s"), 3.0)
        self.assertEqual(parse_duration("1m"), 60.0)
        self.assertEqual(normalized_to_input(0.5, 0.75, 1272, 2800), (636, 2100))
        self.assertEqual(normalized_to_input(1, 1, 1272, 2800), (1271, 2799))
        self.assertEqual(
            screenshot_to_input(250, 500, (500, 1000), (1000, 2000)),
            (500, 1000),
        )
        with self.assertRaises(ValueError):
            screenshot_to_input(250, 500, (500, 1000), (2000, 1000))

    def test_raw_screencap_accepts_android_v1_header(self) -> None:
        value = screenshot(3, 2)
        raw = struct.pack("<IIII", 3, 2, 1, 7) + value.rgba
        parsed = parse_raw_screenshot(raw)
        self.assertEqual((parsed.width, parsed.height), (3, 2))
        self.assertEqual(parsed.color_space, 7)
        self.assertEqual(parsed.rgba, value.rgba)

    def test_raw_screencap_rejects_truncated_payload(self) -> None:
        raw = struct.pack("<IIII", 3, 2, 1, 7) + b"\x00" * 4
        with self.assertRaises(ValueError):
            parse_raw_screenshot(raw)

    def test_dependency_free_png_and_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "shot.png"
            write_png(screenshot(), output)
            self.assertEqual(image_dimensions(output), (8, 12))
            marked = marked_screenshot(screenshot(100, 200), (50, 100))
            marked_path = Path(temporary) / "marked.png"
            write_png(marked, marked_path)
            self.assertEqual(image_dimensions(marked_path), (100, 200))
            self.assertNotEqual(output.read_bytes(), marked_path.read_bytes())

    def test_png_dimensions_rejects_invalid_or_impossible_headers(self) -> None:
        valid = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1272, 2800)
        self.assertEqual(png_dimensions(valid), (1272, 2800))
        with self.assertRaisesRegex(ValueError, "invalid PNG screenshot"):
            png_dimensions(b"not-png")
        invalid = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 0, 2800)
        with self.assertRaisesRegex(ValueError, "invalid PNG dimensions"):
            png_dimensions(invalid)

    def test_trigger_grammar(self) -> None:
        self.assertEqual(parse_trigger("text:Done"), ("text", "Done"))
        self.assertEqual(parse_trigger("text-gone:Loading"), ("text-gone", "Loading"))
        with self.assertRaises(ValueError):
            parse_trigger("Done")

    def test_capture_manifest_preserves_frame_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)

            def fake_frame(serial, destination, index, origin, timeout):
                path = destination / "frames" / f"frame-{index}.png"
                write_png(screenshot(), path)
                return {
                    "index": index,
                    "elapsedMs": index * 100.0,
                    "completedMs": index * 100.0 + 20,
                    "latencyMs": 20.0,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "size": [8, 12],
                    "valid": True,
                    "error": None,
                }

            contact = output / "contact-sheet.png"
            write_png(screenshot(), contact)
            with (
                mock.patch("android_use.capture_command._frame_capture", side_effect=fake_frame),
                mock.patch(
                    "android_use.capture_command._write_contact_sheet",
                    return_value=(str(contact), None),
                ),
            ):
                manifest = run_capture(
                    serial="SERIAL",
                    output=output,
                    fps=10,
                    duration=0.2,
                    trigger=None,
                    before=0.5,
                    after=1.0,
                    trigger_timeout=2.0,
                    trigger_interval=0.1,
                    contains=False,
                    allow_already_gone=False,
                    collect_logcat=False,
                )
            self.assertEqual(manifest["framesValid"], 2)
            self.assertEqual(manifest["actualFps"], 10.0)
            self.assertTrue(Path(manifest["selected"]["middle"]).is_file())
            self.assertTrue(Path(manifest["manifest"]).is_file())

    def test_capture_failure_always_stops_temporary_logcat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = mock.Mock()
            handle = mock.Mock()
            metadata = {"mode": "command", "healthy": True}
            with (
                mock.patch(
                    "android_use.capture_command._start_capture_log",
                    return_value=(process, handle, metadata),
                ),
                mock.patch(
                    "android_use.capture_command._frame_capture",
                    side_effect=RuntimeError("capture transport failed"),
                ),
                mock.patch("android_use.capture_command._stop_capture_log") as stop_log,
            ):
                with self.assertRaisesRegex(RuntimeError, "capture transport failed"):
                    run_capture(
                        serial="SERIAL",
                        output=Path(temporary),
                        fps=1,
                        duration=0.01,
                        trigger=None,
                        before=0.5,
                        after=1.0,
                        trigger_timeout=2.0,
                        trigger_interval=0.1,
                        contains=False,
                        allow_already_gone=False,
                        collect_logcat=True,
                    )
            stop_log.assert_called_once_with(process, handle, metadata)

    def test_capture_burst_is_a_hidden_compatibility_alias(self) -> None:
        invoked = []

        def fake_command(args):
            invoked.append(args.command)
            return 0

        stderr = io.StringIO()
        with (
            mock.patch("android_use.cli_parser.command_capture", side_effect=fake_command),
            mock.patch("android_use.cli_parser.resolve_device", return_value=SimpleNamespace(serial="SERIAL", transport="usb")),
            mock.patch(
                "android_use.cli_parser.action_marker",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch("sys.stderr", stderr),
        ):
            status = cli_main(["capture-burst", "--duration", "100ms", "--no-logcat"])
        self.assertEqual(status, 0)
        self.assertEqual(invoked, ["capture"])
        self.assertIn("renamed to capture", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
