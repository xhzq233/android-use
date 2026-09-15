from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "android-use"


class CLIContractTest(unittest.TestCase):
    def test_help_exposes_only_retained_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn("capture", completed.stdout)
        self.assertIn("current-app", completed.stdout)
        self.assertNotIn("daemon", completed.stdout)
        self.assertNotIn("start-app", completed.stdout)

    def test_core_cli_imports_without_site_packages(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(CLI), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("backed by ADB", completed.stdout)

    def test_removed_wrapper_prints_direct_replacement(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(CLI), "press", "HOME"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("input keyevent", completed.stderr)


if __name__ == "__main__":
    unittest.main()
