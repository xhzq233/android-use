from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use import android_device, persistent_ui


VALID_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<hierarchy rotation=\"0\"></hierarchy>"
)
VALID_BUILD = "a" * 64


class PersistentUiTest(unittest.TestCase):
    def test_dump_requires_matching_protocol_and_build(self) -> None:
        fields = [
            "OK",
            str(len(VALID_XML)),
            str(persistent_ui.PROTOCOL_VERSION),
            VALID_BUILD,
        ]
        with (
            mock.patch.object(
                persistent_ui,
                "_exchange",
                return_value=(fields, VALID_XML.encode()),
            ),
            mock.patch.object(
                persistent_ui,
                "expected_build_hash",
                return_value=VALID_BUILD,
            ),
        ):
            self.assertEqual(
                persistent_ui._dump_once("USB123", True, 2.0),
                VALID_XML,
            )

        stale = [
            "OK",
            str(len(VALID_XML)),
            str(persistent_ui.PROTOCOL_VERSION),
            "b" * 64,
        ]
        with (
            mock.patch.object(
                persistent_ui,
                "_exchange",
                return_value=(stale, VALID_XML.encode()),
            ),
            mock.patch.object(
                persistent_ui,
                "expected_build_hash",
                return_value=VALID_BUILD,
            ),
        ):
            with self.assertRaisesRegex(
                persistent_ui.PersistentUiError,
                "stale UiAutomation server",
            ):
                persistent_ui._dump_once("USB123", True, 2.0)

    def test_unknown_existing_server_is_stopped_before_launch(self) -> None:
        expected = persistent_ui.ServerInfo(
            persistent_ui.PROTOCOL_VERSION,
            42,
            VALID_BUILD,
        )
        with (
            mock.patch.object(
                persistent_ui,
                "expected_build_hash",
                return_value=VALID_BUILD,
            ),
            mock.patch.object(
                persistent_ui,
                "_start_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                persistent_ui,
                "_server_info",
                side_effect=[
                    persistent_ui.PersistentUiError("old response"),
                    expected,
                ],
            ),
            mock.patch.object(persistent_ui, "_stop_server") as stop,
            mock.patch.object(
                persistent_ui,
                "_ensure_remote_jar",
                return_value="/data/local/tmp/server.jar",
            ),
            mock.patch.object(persistent_ui, "_launch_server") as launch,
        ):
            self.assertEqual(persistent_ui.ensure_server("USB123"), expected)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0], "USB123")
        launch.assert_called_once()
        self.assertEqual(
            launch.call_args.args[:3],
            ("USB123", "/data/local/tmp/server.jar", VALID_BUILD),
        )

    def test_authenticated_exchange_marks_dump_payload(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "_session_token",
                return_value="c" * 64,
            ),
            mock.patch.object(
                persistent_ui,
                "_exchange_raw",
                return_value=(["OK"], b""),
            ) as exchange,
        ):
            persistent_ui._exchange("USB123", "DUMP 1", timeout=2.0)
        exchange.assert_called_once_with(
            "USB123",
            f"AUTH {'c' * 64} DUMP 1",
            timeout=2.0,
            expects_payload=True,
        )

    def test_session_token_is_stable_and_private(self) -> None:
        persistent_ui._session_token.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(
                    persistent_ui.tempfile,
                    "gettempdir",
                    return_value=directory,
                ):
                    first = persistent_ui._session_token("USB123")
                    persistent_ui._session_token.cache_clear()
                    second = persistent_ui._session_token("USB123")
                    path = persistent_ui._runtime_token_path("USB123")
                    self.assertEqual(first, second)
                    self.assertEqual(len(first), 64)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            persistent_ui._session_token.cache_clear()

    def test_owned_pid_requires_android_use_jar_in_process_environment(self) -> None:
        pid = subprocess.CompletedProcess([], 0, "4242\n", "")
        owned = subprocess.CompletedProcess(
            [],
            0,
            "CLASSPATH=/data/local/tmp/android-use-ui-server-deadbeef.jar\0",
            "",
        )
        unrelated = subprocess.CompletedProcess(
            [],
            0,
            "CLASSPATH=/data/local/tmp/other.jar\0",
            "",
        )
        with mock.patch.object(
            persistent_ui,
            "run_adb_shell",
            side_effect=[pid, owned],
        ):
            self.assertEqual(persistent_ui._owned_server_pid("USB123"), 4242)
        with mock.patch.object(
            persistent_ui,
            "run_adb_shell",
            side_effect=[pid, unrelated],
        ):
            self.assertIsNone(persistent_ui._owned_server_pid("USB123"))

    def test_stop_force_kills_only_after_graceful_paths_fail(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "_exchange",
                side_effect=persistent_ui.PersistentUiError("hung"),
            ),
            mock.patch.object(
                persistent_ui,
                "_exchange_raw",
                side_effect=persistent_ui.PersistentUiError("hung"),
            ),
            mock.patch.object(
                persistent_ui,
                "_force_stop_owned_server",
                return_value=True,
            ) as force_stop,
        ):
            persistent_ui._stop_server("USB123")
        force_stop.assert_called_once()
        self.assertEqual(force_stop.call_args.args[0], "USB123")

    def test_hard_stop_skips_graceful_exchange(self) -> None:
        with (
            mock.patch.object(persistent_ui, "_exchange") as exchange,
            mock.patch.object(
                persistent_ui,
                "_force_stop_owned_server",
                return_value=True,
            ) as force_stop,
        ):
            self.assertTrue(
                persistent_ui._stop_server("USB123", hard=True)
            )
        exchange.assert_not_called()
        force_stop.assert_called_once()

    def test_force_stop_confirms_process_death_before_pid_cleanup(self) -> None:
        completed = lambda returncode=0, stdout="": subprocess.CompletedProcess(
            [],
            returncode,
            stdout,
            "",
        )
        with mock.patch.object(
            persistent_ui,
            "run_adb_shell",
            side_effect=[
                completed(stdout="4242\n"),
                completed(
                    stdout=(
                        "CLASSPATH=/data/local/tmp/"
                        "android-use-ui-server-deadbeef.jar\0"
                    )
                ),
                completed(),
                completed(),
                completed(),
            ],
        ) as run_shell:
            self.assertTrue(
                persistent_ui._force_stop_owned_server("USB123")
            )
        commands = [call.args[1] for call in run_shell.call_args_list]
        self.assertEqual(commands[2], ["kill", "-9", "4242"])
        self.assertEqual(
            commands[3],
            ["test", "!", "-d", "/proc/4242"],
        )
        self.assertEqual(commands[4], ["rm", "-f", persistent_ui.REMOTE_PID])

    def test_dump_reuses_server_recovered_by_another_process(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "_dump_once",
                side_effect=[
                    persistent_ui.PersistentUiError("hung"),
                    VALID_XML,
                ],
            ) as dump,
            mock.patch.object(persistent_ui, "_stop_server") as stop,
            mock.patch.object(
                persistent_ui,
                "_ensure_server_locked",
            ) as ensure,
        ):
            self.assertEqual(
                persistent_ui.dump_xml("USB123", timeout=8.0),
                VALID_XML,
            )
        self.assertEqual(dump.call_count, 2)
        stop.assert_not_called()
        ensure.assert_not_called()

    def test_dump_restarts_once_when_lock_recheck_still_fails(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "_dump_once",
                side_effect=[
                    persistent_ui.PersistentUiError("hung"),
                    persistent_ui.PersistentUiError("still hung"),
                    VALID_XML,
                ],
            ) as dump,
            mock.patch.object(
                persistent_ui,
                "_stop_server",
                return_value=True,
            ) as stop,
            mock.patch.object(
                persistent_ui,
                "_ensure_server_locked",
            ) as ensure,
        ):
            self.assertEqual(
                persistent_ui.dump_xml("USB123", timeout=8.0),
                VALID_XML,
            )
        self.assertEqual(dump.call_count, 3)
        stop.assert_called_once()
        self.assertTrue(stop.call_args.kwargs["hard"])
        ensure.assert_called_once()

    def test_launch_passes_build_and_session_token(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "_remote_exists",
                return_value=True,
            ),
            mock.patch.object(
                persistent_ui,
                "_session_token",
                return_value="d" * 64,
            ),
            mock.patch.object(persistent_ui, "run_adb") as run_adb,
        ):
            persistent_ui._launch_server(
                "USB123",
                "/data/local/tmp/android-use-ui-server.jar",
                VALID_BUILD,
            )
        shell_command = run_adb.call_args.args[0][1]
        self.assertIn(f"androidUseBuild {VALID_BUILD}", shell_command)
        self.assertIn(f"androidUseToken {'d' * 64}", shell_command)

    def test_remote_jar_is_hash_verified_and_protected(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "_remote_exists",
                return_value=True,
            ),
            mock.patch.object(
                persistent_ui,
                "_remote_hash_matches",
                return_value=True,
            ) as hash_matches,
            mock.patch.object(
                persistent_ui,
                "_protect_remote_jar",
            ) as protect,
            mock.patch.object(persistent_ui, "run_adb") as push,
        ):
            remote = persistent_ui._ensure_remote_jar(
                "USB123",
                VALID_BUILD,
            )
        self.assertEqual(remote, persistent_ui._remote_jar(VALID_BUILD))
        hash_matches.assert_called_once()
        protect.assert_called_once()
        push.assert_not_called()

    def test_stock_dump_takes_over_after_persistent_failure(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "dump_xml",
                side_effect=persistent_ui.PersistentUiError("unavailable"),
            ) as persistent,
            mock.patch.object(
                android_device,
                "_stock_dump_xml",
                return_value=VALID_XML,
            ) as stock,
            mock.patch.object(persistent_ui, "stop_server") as stop,
            mock.patch.object(
                android_device.time,
                "monotonic",
                side_effect=[100.0, 106.0, 106.1],
            ),
        ):
            self.assertEqual(android_device.dump_xml("USB123"), VALID_XML)
        stop.assert_called_once_with("USB123", timeout=0.75, hard=True)
        persistent.assert_called_once_with(
            "USB123",
            compressed=True,
            timeout=6.0,
        )
        self.assertEqual(stock.call_args.args, ("USB123",))
        self.assertTrue(stock.call_args.kwargs["compressed"])
        self.assertAlmostEqual(stock.call_args.kwargs["timeout"], 3.9)

    def test_stock_dump_is_skipped_when_server_death_is_unconfirmed(self) -> None:
        with (
            mock.patch.object(
                persistent_ui,
                "dump_xml",
                side_effect=persistent_ui.PersistentUiError("hung"),
            ),
            mock.patch.object(
                persistent_ui,
                "stop_server",
                return_value=False,
            ),
            mock.patch.object(android_device, "_stock_dump_xml") as stock,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "stock dump was not started",
            ):
                android_device.dump_xml("USB123")
        stock.assert_not_called()

    def test_explicit_stock_mode_releases_persistent_server(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"ANDROID_USE_STOCK_UIAUTOMATOR": "1"},
            ),
            mock.patch.object(persistent_ui, "stop_server") as stop,
            mock.patch.object(
                android_device,
                "_stock_dump_xml",
                return_value=VALID_XML,
            ),
        ):
            self.assertEqual(android_device.dump_xml("USB123"), VALID_XML)
        stop.assert_called_once_with("USB123")


if __name__ == "__main__":
    unittest.main()
