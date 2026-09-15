"""mitmdump addon used by the external android-use proxy process."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mitmproxy import http


def _decode_body(content: bytes | None) -> str | None:
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


class AndroidUseMitmAddon:
    def __init__(self) -> None:
        self.capture_path = Path(os.environ["ANDROID_USE_PROXY_CAPTURE_PATH"])
        self.count_path = Path(os.environ["ANDROID_USE_PROXY_COUNT_PATH"])
        self.mock_path = Path(os.environ["ANDROID_USE_PROXY_MOCK_PATH"])
        self.ready_path = Path(os.environ["ANDROID_USE_PROXY_READY_PATH"])
        self.capture_enabled = os.environ.get("ANDROID_USE_PROXY_CAPTURE_ENABLED", "1") == "1"
        self.mock_rules: list[dict] = []
        self.flow_count = 0
        self._mock_mtime_ns: int | None = None

    def running(self) -> None:
        self.count_path.write_text("0", encoding="utf-8")
        self.ready_path.write_text(str(os.getpid()), encoding="utf-8")

    def done(self) -> None:
        try:
            self.ready_path.unlink()
        except FileNotFoundError:
            pass

    def _refresh_mock_rules(self) -> None:
        try:
            mtime_ns = self.mock_path.stat().st_mtime_ns
        except FileNotFoundError:
            self.mock_rules = []
            self._mock_mtime_ns = None
            return
        if mtime_ns == self._mock_mtime_ns:
            return
        try:
            data = json.loads(self.mock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, list):
            return
        self.mock_rules = [item for item in data if isinstance(item, dict) and "url" in item]
        self._mock_mtime_ns = mtime_ns

    def request(self, flow) -> None:
        self._refresh_mock_rules()
        for rule in self.mock_rules:
            if str(rule["url"]) in flow.request.pretty_url:
                flow.response = http.Response.make(
                    int(rule.get("status", 200)),
                    str(rule.get("body", "")).encode("utf-8"),
                    {"Content-Type": "application/json"},
                )
                return

    def response(self, flow) -> None:
        if not self.capture_enabled:
            return
        request = flow.request
        response = flow.response
        record = {
            "request": {
                "method": request.method,
                "url": request.pretty_url,
                "headers": dict(request.headers),
                "body": _decode_body(request.content),
            },
            "response": {
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": _decode_body(response.content),
            },
        }
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)
        with self.capture_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.flow_count += 1
        self.count_path.write_text(str(self.flow_count), encoding="utf-8")


addons = [AndroidUseMitmAddon()]
