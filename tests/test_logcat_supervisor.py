from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.logcat_commands import action_marker
from android_use.logcat_supervisor import (
    Supervisor,
    collector_status,
    is_adb_logcat_process,
    write_state,
)


def fake_adb(directory: Path) -> Path:
    path = directory / "adb"
    path.write_text(
        """#!/usr/bin/env python3
print("07-19 01:02:03.456  100  200 I TestTag: hello", flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class LogcatSupervisorTest(unittest.TestCase):
    def test_supervisor_records_child_exit_and_raw_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = directory / "state.json"
            log = directory / "capture.log"
            adb = fake_adb(directory)
            with mock.patch.dict(os.environ, {"ANDROID_USE_ADB": str(adb)}, clear=False):
                status = Supervisor("SERIAL", log, state, "com.example").run()
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(status, 0)
            self.assertEqual(payload["status"], "exited")
            self.assertEqual(payload["exitCode"], 0)
            self.assertIn("TestTag: hello", log.read_text(encoding="utf-8"))

    def test_status_reports_bytes_timestamp_and_flow_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = directory / "state.json"
            log = directory / "capture.log"
            log.write_text(
                "07-19 01:02:03.456  100  200 I TestTag: hello\n"
                "07-19 01:02:04.000  100  200 W logd: reader dropped lines\n",
                encoding="utf-8",
            )
            write_state(
                state,
                {
                    "status": "stopped",
                    "logFile": str(log),
                    "supervisorPid": 0,
                    "adbPid": 0,
                },
            )
            payload = collector_status(state)
        self.assertGreater(payload["bytes"], 0)
        self.assertEqual(payload["lastDeviceTimestamp"], "07-19 01:02:04.000")
        self.assertTrue(payload["warnings"])

    def test_adb_logcat_process_identity_check(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout="/opt/android/platform-tools/adb -s SERIAL logcat -v threadtime\n",
        )
        with (
            mock.patch(
                "android_use.logcat_supervisor.subprocess.run",
                return_value=completed,
            ),
            mock.patch(
                "android_use.logcat_supervisor.adb_executable",
                return_value="/opt/android/platform-tools/adb",
            ),
        ):
            self.assertTrue(is_adb_logcat_process(123))

        completed.stdout = "/usr/bin/python3 unrelated.py\n"
        with mock.patch(
            "android_use.logcat_supervisor.subprocess.run",
            return_value=completed,
        ):
            self.assertFalse(is_adb_logcat_process(123, "/opt/android/platform-tools/adb"))

    def test_action_marker_sanitizes_device_log_message(self) -> None:
        events = []
        messages = []
        with (
            mock.patch(
                "android_use.logcat_commands.active_status",
                return_value={"healthy": True},
            ),
            mock.patch(
                "android_use.logcat_commands._append_timeline",
                side_effect=lambda _serial, event: events.append(event),
            ),
            mock.patch(
                "android_use.logcat_commands._emit_android_marker",
                side_effect=lambda _serial, message: messages.append(message),
            ),
        ):
            with action_marker("SERIAL", "tap;echo injected"):
                pass
        self.assertEqual(len(events), 2)
        self.assertNotIn(";", events[0]["operation"])
        self.assertTrue(all(";" not in message for message in messages))


if __name__ == "__main__":
    unittest.main()
