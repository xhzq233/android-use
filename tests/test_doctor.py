from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.android_device import AdbDeviceInfo
from android_use.cli_parser import build_parser
from android_use.command_handlers import command_doctor


class DoctorTest(unittest.TestCase):
    def _run(self) -> tuple[int, dict]:
        args = build_parser().parse_args(["doctor", "--format", "json"])
        output = io.StringIO()
        device = AdbDeviceInfo("SERIAL", "device", "usb:1-1 model:Test")
        with (
            mock.patch("android_use.command_handlers.shutil.which", side_effect=lambda value: f"/usr/bin/{value}"),
            mock.patch("android_use.command_handlers.adb_version_text", return_value="adb 1.0.41"),
            mock.patch("android_use.command_handlers.list_adb_devices", return_value=[device]),
            mock.patch("android_use.command_handlers.resolve_serial", return_value="SERIAL"),
            mock.patch(
                "android_use.command_handlers.run_adb_shell",
                return_value=SimpleNamespace(stdout="value\n"),
            ),
            mock.patch("android_use.command_handlers.platform.system", return_value="Linux"),
            mock.patch("android_use.command_handlers.platform.machine", return_value="x86_64"),
            mock.patch("android_use.command_handlers.sys.stdout", output),
        ):
            status = command_doctor(args)
        return status, json.loads(output.getvalue())

    def test_core_doctor_accepts_linux_x86_host(self) -> None:
        status, payload = self._run()
        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"])

    def test_doctor_reports_no_third_party_runtime_requirement(self) -> None:
        status, payload = self._run()
        self.assertEqual(status, 0)
        self.assertFalse(payload["python"]["thirdPartyRuntimePackagesRequired"])
        self.assertEqual(payload["selectedDevice"], "SERIAL")


if __name__ == "__main__":
    unittest.main()
