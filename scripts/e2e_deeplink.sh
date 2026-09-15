#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN="${ANDROID_USE_BIN:-$REPO_ROOT/android-use}"
SERIAL="${ANDROID_USE_SERIAL:-}"
PACKAGE="${ANDROID_USE_E2E_PACKAGE:-}"
VALID_URI="${ANDROID_USE_E2E_VALID_URI:-}"
INVALID_URI="${DEEPLINK_INVALID_URI:-x-android-use-unregistered://nothing}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--serial) SERIAL="${2:?missing serial}"; shift 2 ;;
    -h|--help)
      printf 'Usage: ANDROID_USE_E2E_PACKAGE=<package> ANDROID_USE_E2E_VALID_URI=<uri> %s [-s SERIAL]\n' "$0"
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PACKAGE" || -z "$VALID_URI" ]]; then
  echo "ANDROID_USE_E2E_PACKAGE and ANDROID_USE_E2E_VALID_URI are required" >&2
  exit 2
fi

SERIAL_ARGS=()
if [[ -n "$SERIAL" ]]; then
  SERIAL_ARGS=(-s "$SERIAL")
fi
ARTIFACT_DIR="${ANDROID_USE_E2E_OUT:-$REPO_ROOT/output/android-use-e2e/deeplink-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$ARTIFACT_DIR"
RUNNING=0

cleanup() {
  local status=$?
  if [[ "$RUNNING" -eq 1 ]]; then
    "$BIN" logcat stop "${SERIAL_ARGS[@]}" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

"$BIN" doctor "${SERIAL_ARGS[@]}" --format json > "$ARTIFACT_DIR/doctor.json"
"$BIN" logcat start "${SERIAL_ARGS[@]}" --package "$PACKAGE" > "$ARTIFACT_DIR/logcat-start.txt"
RUNNING=1

"$BIN" deeplink "${SERIAL_ARGS[@]}" "$VALID_URI" \
  --wait \
  --expect-activity "^${PACKAGE//./\\.}/" \
  --assert-timeout 15s \
  --format json > "$ARTIFACT_DIR/valid.json"

set +e
"$BIN" deeplink "${SERIAL_ARGS[@]}" "$INVALID_URI" --wait --format json \
  > "$ARTIFACT_DIR/invalid.json" 2> "$ARTIFACT_DIR/invalid.stderr"
INVALID_STATUS=$?
set -e
if [[ "$INVALID_STATUS" -eq 0 ]]; then
  echo "invalid DeepLink unexpectedly succeeded; artifacts=$ARTIFACT_DIR" >&2
  exit 1
fi

"$BIN" logcat status "${SERIAL_ARGS[@]}" --format json > "$ARTIFACT_DIR/logcat-status.json"
"$BIN" logcat stop "${SERIAL_ARGS[@]}" > "$ARTIFACT_DIR/logcat-stop.txt"
RUNNING=0
printf 'PASS artifacts=%s\n' "$ARTIFACT_DIR"
