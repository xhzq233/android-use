from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.cli_parser import build_parser
from android_use.proxy_server import (
    MockRule,
    _absolute_cli_path,
    _dump_flows,
    _load_mock_rules,
    _read_capture_count,
    _read_captured_flows,
    _write_mock_rules,
    doctor_proxy,
    find_mitmdump,
    inspect_mitmdump,
    proxy_addon_path,
    proxy_capture_path,
    proxy_count_path,
    proxy_flows_path,
    proxy_mock_path,
    proxy_ready_path,
    proxy_socket_path,
)


FLOW = {
    "request": {
        "method": "GET",
        "url": "http://example.com/android-use-smoke",
        "headers": {"Host": "example.com"},
        "body": "",
    },
    "response": {
        "status": 200,
        "headers": {"Content-Type": "text/plain"},
        "body": "ok",
    },
}


class ProxyLogicTest(unittest.TestCase):
    def test_proxy_doctor_is_a_host_side_command(self) -> None:
        args = build_parser().parse_args(["proxy", "doctor", "-s", "device-1"])
        self.assertEqual(args.proxy_action, "doctor")
        self.assertEqual(args.serial, "device-1")

    def test_dump_supports_text_json_and_har(self) -> None:
        text = _dump_flows([FLOW], "text", None)["content"]
        json_dump = json.loads(_dump_flows([FLOW], "json", None)["content"])
        har = json.loads(_dump_flows([FLOW], "har", None)["content"])

        self.assertEqual(text, "GET    200  http://example.com/android-use-smoke\n")
        self.assertEqual(json_dump, [FLOW])
        self.assertEqual(har["log"]["entries"][0]["response"]["status"], 200)

    def test_proxy_cli_paths_are_resolved_by_the_foreground_command(self) -> None:
        self.assertEqual(_absolute_cli_path("capture.har"), str((Path.cwd() / "capture.har").resolve()))

    def test_mock_rules_are_validated_and_written_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            runtime = Path(directory) / "runtime.json"
            source.write_text(
                '[{"url":"example.com/smoke","status":201,"body":"{\\"ok\\":true}"}]',
                encoding="utf-8",
            )

            rules = _load_mock_rules(str(source))
            _write_mock_rules(runtime, rules)

            self.assertEqual(rules, [MockRule("example.com/smoke", 201, '{"ok":true}')])
            self.assertEqual(json.loads(runtime.read_text(encoding="utf-8"))[0]["status"], 201)
            self.assertFalse(runtime.with_suffix(".json.tmp").exists())

    def test_invalid_mock_rules_fail_before_proxy_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text('{"url":"not-an-array"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON array"):
                _load_mock_rules(str(path))
            path.write_text('[{"url":"x","status":"201"}]', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "status"):
                _load_mock_rules(str(path))

    def test_capture_reader_ignores_only_a_partial_last_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flows.ndjson"
            path.write_text(json.dumps(FLOW) + "\n{", encoding="utf-8")
            self.assertEqual(_read_captured_flows(path), [FLOW])

            path.write_text("{broken}\n" + json.dumps(FLOW) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 1"):
                _read_captured_flows(path)

            count_path = Path(directory) / "flows.count"
            self.assertEqual(_read_capture_count(count_path), 0)
            count_path.write_text("2", encoding="utf-8")
            self.assertEqual(_read_capture_count(count_path), 2)

    def test_runtime_paths_are_isolated_by_serial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"ANDROID_USE_PROXY_DIR": directory}, clear=False):
                first = {
                    proxy_socket_path("192.168.1.2:5555"),
                    proxy_capture_path("192.168.1.2:5555"),
                    proxy_count_path("192.168.1.2:5555"),
                    proxy_flows_path("192.168.1.2:5555"),
                    proxy_mock_path("192.168.1.2:5555"),
                    proxy_addon_path("192.168.1.2:5555"),
                    proxy_ready_path("192.168.1.2:5555"),
                }
                second = {
                    proxy_socket_path("192.168.1.3:5555"),
                    proxy_capture_path("192.168.1.3:5555"),
                    proxy_count_path("192.168.1.3:5555"),
                    proxy_flows_path("192.168.1.3:5555"),
                    proxy_mock_path("192.168.1.3:5555"),
                    proxy_addon_path("192.168.1.3:5555"),
                    proxy_ready_path("192.168.1.3:5555"),
                }
            self.assertEqual(len(first), 7)
            self.assertTrue(first.isdisjoint(second))

    def test_mitmdump_detection_uses_path_executable(self) -> None:
        with mock.patch("android_use.proxy_server.shutil.which", return_value="/opt/bin/mitmdump"):
            self.assertEqual(find_mitmdump(), "/opt/bin/mitmdump")

    def test_mitmdump_detection_accepts_explicit_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "mitmdump"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.dict(os.environ, {"ANDROID_USE_MITMDUMP": str(executable)}):
                self.assertEqual(find_mitmdump(), str(executable.resolve()))

    def test_mitmdump_inspection_reads_binary_version(self) -> None:
        completed = subprocess.CompletedProcess(
            ["/opt/bin/mitmdump", "--version"],
            0,
            stdout="Mitmproxy: 12.2.2 binary\nPython: 3.14\n",
            stderr="",
        )
        with mock.patch("android_use.proxy_server.find_mitmdump", return_value="/opt/bin/mitmdump"):
            with mock.patch("android_use.proxy_server.subprocess.run", return_value=completed):
                info = inspect_mitmdump()
        self.assertEqual(
            info,
            {
                "ok": True,
                "path": "/opt/bin/mitmdump",
                "version": "Mitmproxy: 12.2.2 binary",
            },
        )

    def test_proxy_doctor_reports_host_and_device_state(self) -> None:
        output = StringIO()
        args = Namespace(serial="device-1", timeout=0.1)
        with mock.patch(
            "android_use.proxy_server.inspect_mitmdump",
            return_value={"ok": True, "path": "/opt/bin/mitmdump", "version": "Mitmproxy: 12.2.2 binary"},
        ):
            with mock.patch("android_use.proxy_server.get_ca_cert_path", return_value=Path("/tmp/mitm-ca.pem")):
                with mock.patch("android_use.proxy_server.resolve_serial", return_value="device-1"):
                    with mock.patch(
                        "android_use.proxy_server._proxy_ping",
                        return_value={"pid": 42, "port": 8080, "flows_count": 3},
                    ):
                        with mock.patch("android_use.proxy_server.get_device_proxy", return_value="127.0.0.1:8080"):
                            with mock.patch("android_use.proxy_server.check_ca_cert", return_value="unknown"):
                                with redirect_stdout(output):
                                    exit_code = doctor_proxy(args)
        self.assertEqual(exit_code, 0)
        text = output.getvalue()
        self.assertIn("[ok] mitmdump executable: /opt/bin/mitmdump", text)
        self.assertIn("[ok] proxy: running pid=42 port=8080 flows=3", text)
        self.assertIn("[-] device CA: unknown", text)


if __name__ == "__main__":
    unittest.main()
