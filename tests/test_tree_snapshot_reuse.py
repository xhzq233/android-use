from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.command_handlers import (
    ScrollOperationError,
    _scroll_to,
    _wait_text_assertion,
    command_deeplink,
    command_scroll_to,
    command_tap,
)
from android_use.ui_semantics import UISnapshot


PKG = "com.example.app"


def _page() -> UISnapshot:
    xml = f"""
    <hierarchy>
      <node package="{PKG}" class="android.widget.FrameLayout"
            bounds="[0,0][1080,2400]" />
    </hierarchy>
    """
    return UISnapshot(
        xml=xml,
        root=ET.fromstring(xml),
        package=PKG,
        activity="",
        elapsed_ms=2300,
    )


def _scrollable_page() -> UISnapshot:
    xml = f"""
    <hierarchy>
      <node package="{PKG}" class="android.widget.FrameLayout"
            bounds="[0,0][1080,2400]">
        <node package="{PKG}" class="androidx.recyclerview.widget.RecyclerView"
              bounds="[0,1800][1080,2300]" scrollable="true" />
      </node>
    </hierarchy>
    """
    return UISnapshot(
        xml=xml,
        root=ET.fromstring(xml),
        package=PKG,
        activity="",
        elapsed_ms=2300,
    )


def _scroll_args(**overrides):
    values = {
        "resolved_serial": "usb-device",
        "text": "Missing",
        "contains": False,
        "match_index": 0,
        "container": None,
        "container_index": 0,
        "direction": "forth",
        "attempts": 0,
        "duration": 0.3,
        "verify_delay": 0.0,
        "stop_on_no_progress": True,
        "no_evidence": False,
        "format": "text",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TreeSnapshotReuseTest(unittest.TestCase):
    def test_scroll_error_carries_the_last_snapshot(self) -> None:
        page = _page()
        with mock.patch(
            "android_use.command_handlers.snapshot",
            return_value=page,
        ) as take_snapshot:
            with self.assertRaises(ScrollOperationError) as raised:
                _scroll_to(
                    serial="usb-device",
                    target="Missing",
                    contains=False,
                    match_index=0,
                    container=None,
                    container_index=0,
                    direction="forth",
                    attempts=0,
                    duration=0.3,
                    verify_delay=0.0,
                    stop_on_no_progress=True,
                )
        self.assertIs(raised.exception.page, page)
        take_snapshot.assert_called_once()

    def test_no_progress_stops_before_sending_another_swipe(self) -> None:
        page = _scrollable_page()
        adb_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch(
                "android_use.command_handlers.snapshot",
                return_value=page,
            ) as take_snapshot,
            mock.patch(
                "android_use.command_handlers.run_adb_shell",
                return_value=adb_result,
            ) as run_shell,
        ):
            with self.assertRaises(ScrollOperationError) as raised:
                _scroll_to(
                    serial="usb-device",
                    target="Missing",
                    contains=False,
                    match_index=0,
                    container=None,
                    container_index=0,
                    direction="forth",
                    attempts=5,
                    duration=0.3,
                    verify_delay=0.0,
                    stop_on_no_progress=True,
                )
        self.assertIs(raised.exception.page, page)
        self.assertEqual(take_snapshot.call_count, 2)
        run_shell.assert_called_once()

    def test_scroll_commands_reuse_the_failed_operation_snapshot(self) -> None:
        page = _page()
        error = ScrollOperationError("missing", page)
        evidence_path = Path("/tmp/diagnostics.json")
        with (
            mock.patch(
                "android_use.command_handlers._scroll_to",
                side_effect=error,
            ),
            mock.patch(
                "android_use.command_handlers.collect_failure_evidence",
                return_value=evidence_path,
            ) as collect,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(command_scroll_to(_scroll_args()), 1)
        self.assertIs(collect.call_args.kwargs["last_page"], page)

        tap_args = _scroll_args(
            target_or_x=None,
            y=None,
            normalized=None,
            from_screenshot=None,
            scroll_if_needed=True,
            scroll_direction="forth",
            scroll_duration=0.3,
            verify_change=False,
            mark=None,
            dump=False,
        )
        metrics = SimpleNamespace(
            input_width=1080,
            input_height=2400,
            physical_width=1080,
            physical_height=2400,
            rotation=0,
        )
        with (
            mock.patch(
                "android_use.command_handlers.display_metrics",
                return_value=metrics,
            ),
            mock.patch(
                "android_use.command_handlers._scroll_to",
                side_effect=error,
            ),
            mock.patch(
                "android_use.command_handlers.collect_failure_evidence",
                return_value=evidence_path,
            ) as collect,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(command_tap(tap_args), 1)
        self.assertIs(collect.call_args.kwargs["last_page"], page)

    def test_deeplink_text_failure_reuses_the_assertion_snapshot(self) -> None:
        page = _page()
        with (
            mock.patch(
                "android_use.command_handlers.time.monotonic",
                side_effect=[0.0, 0.1, 2.0],
            ),
            mock.patch(
                "android_use.command_handlers.snapshot",
                return_value=page,
            ) as take_snapshot,
        ):
            ok, detail, last_page = _wait_text_assertion(
                "usb-device",
                "Missing",
                1.0,
            )
        self.assertFalse(ok)
        self.assertEqual(detail["matchCount"], 0)
        self.assertIs(last_page, page)
        take_snapshot.assert_called_once()

        args = SimpleNamespace(
            resolved_serial="usb-device",
            expect_log=None,
            wait=False,
            uri="example://main/path",
            timeout=5.0,
            expect_activity=None,
            expect_text="Missing",
            assert_timeout=1.0,
            no_evidence=False,
            format="json",
        )
        adb_result = SimpleNamespace(
            returncode=0,
            stdout="Starting: Intent",
            stderr="",
        )
        evidence_path = Path("/tmp/diagnostics.json")
        with (
            mock.patch(
                "android_use.command_handlers.run_adb_shell",
                return_value=adb_result,
            ),
            mock.patch(
                "android_use.command_handlers._wait_text_assertion",
                return_value=(False, {"expected": "Missing", "matchCount": 0}, page),
            ),
            mock.patch(
                "android_use.command_handlers.collect_failure_evidence",
                return_value=evidence_path,
            ) as collect,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(command_deeplink(args), 1)
        self.assertIs(collect.call_args.kwargs["last_page"], page)


if __name__ == "__main__":
    unittest.main()
