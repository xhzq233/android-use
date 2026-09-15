from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.android_device import (
    AdbCommandError,
    list_adb_devices,
    resolve_serial,
    run_adb,
    run_adb_bytes,
)


def fake_adb(directory: Path) -> Path:
    path = directory / "adb"
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
args = sys.argv[1:]
if args == ["devices", "-l"]:
    print("List of devices attached")
    print("USB123 device usb:1-2 model:Phone")
    print("10.0.0.2:5555 device product:Phone")
    print("emulator-5554 device product:sdk")
    raise SystemExit(0)
if args == ["version"]:
    print("fake adb")
    raise SystemExit(0)
if args == ["binary"]:
    sys.stdout.buffer.write(b"\\x00\\xffpayload")
    raise SystemExit(0)
if args and args[0] == "fail":
    print("device offline", file=sys.stderr)
    raise SystemExit(17)
print(json.dumps(args))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class AndroidDeviceTest(unittest.TestCase):
    def test_selection_accepts_all_online_adb_transports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adb = fake_adb(Path(temporary))
            with mock.patch.dict(os.environ, {"ANDROID_USE_ADB": str(adb)}, clear=False):
                devices = list_adb_devices()
                self.assertEqual([device.transport for device in devices], ["usb", "network", "emulator"])
                for device in devices:
                    self.assertEqual(resolve_serial(device.serial), device.serial)
                with self.assertRaises(SystemExit):
                    resolve_serial(None)

    def test_argument_vector_is_not_reparsed_by_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adb = fake_adb(Path(temporary))
            with mock.patch.dict(os.environ, {"ANDROID_USE_ADB": str(adb)}, clear=False):
                result = run_adb(["shell", "echo", "a b", "$(unsafe)"], check=True)
        self.assertEqual(
            json.loads(result.stdout),
            ["shell", "echo", "a b", "$(unsafe)"],
        )

    def test_binary_output_and_nonzero_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adb = fake_adb(Path(temporary))
            with mock.patch.dict(os.environ, {"ANDROID_USE_ADB": str(adb)}, clear=False):
                self.assertEqual(run_adb_bytes(["binary"], check=True).stdout, b"\x00\xffpayload")
                with self.assertRaises(AdbCommandError) as context:
                    run_adb(["fail"], check=True)
        self.assertEqual(context.exception.returncode, 17)
        self.assertIn("device offline", str(context.exception))


if __name__ == "__main__":
    unittest.main()
