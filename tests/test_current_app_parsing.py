from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from android_use.ui_semantics import parse_current_app


class CurrentAppParsingTest(unittest.TestCase):
    def test_parses_oplus_resumed_activity(self) -> None:
        text = """
        ResumedActivity: ActivityRecord{139482750 u0 com.android.launcher/.Launcher t5}
        mFocusedApp=ActivityRecord{139482750 u0 com.android.launcher/.Launcher t5}
        """

        self.assertEqual(
            parse_current_app(text),
            {"package": "com.android.launcher", "activity": ".Launcher"},
        )

    def test_parses_standard_m_resumed_activity(self) -> None:
        text = """
        mResumedActivity: ActivityRecord{4bd5 u0 com.example.app/.EditActivity t222}
        """

        self.assertEqual(
            parse_current_app(text),
            {"package": "com.example.app", "activity": ".EditActivity"},
        )

    def test_parses_window_current_focus(self) -> None:
        text = """
        mCurrentFocus=Window{35a2 u0 com.heytap.browser/com.android.browser.BrowserActivity}
        """

        self.assertEqual(
            parse_current_app(text),
            {"package": "com.heytap.browser", "activity": "com.android.browser.BrowserActivity"},
        )


if __name__ == "__main__":
    unittest.main()
