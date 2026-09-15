#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERIAL="${ANDROID_USE_SERIAL:-}"
PORT=18081
ARTIFACT_DIR=""
RUN_HTTPS=1

usage() {
  cat <<'EOF'
Usage: e2e_proxy.sh [-s SERIAL] [--port PORT] [--artifacts DIR] [--skip-https]

Verifies android-use proxy start/status, ADB reverse, device-originated HTTP,
runtime mock reload, JSON/HAR export, optional HTTPS, and stop cleanup.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--serial) SERIAL="${2:?missing serial}"; shift 2 ;;
    --port) PORT="${2:?missing port}"; shift 2 ;;
    --artifacts) ARTIFACT_DIR="${2:?missing artifact directory}"; shift 2 ;;
    --skip-https) RUN_HTTPS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$PORT" in
  ""|*[!0-9]*) echo "port must be an integer" >&2; exit 2 ;;
esac
if (( PORT < 1 || PORT > 65535 )); then
  echo "port must be between 1 and 65535" >&2
  exit 2
fi

resolve_bin() {
  if [[ -n "${ANDROID_USE_BIN:-}" ]]; then
    [[ -x "$ANDROID_USE_BIN" ]] || return 1
    printf '%s\n' "$ANDROID_USE_BIN"
  elif [[ -x "$REPO_ROOT/android-use" ]]; then
    printf '%s\n' "$REPO_ROOT/android-use"
  elif command -v android-use >/dev/null 2>&1; then
    command -v android-use
  else
    return 1
  fi
}

BIN="$(resolve_bin)"
if [[ -z "$SERIAL" ]]; then
  DEVICE_LIST="$(adb devices -l | awk 'NR > 1 && $2 == "device" {print $1}')"
  DEVICE_COUNT="$(printf '%s\n' "$DEVICE_LIST" | awk 'NF {count++} END {print count+0}')"
  if [[ "$DEVICE_COUNT" -ne 1 ]]; then
    echo "expected one online adb device; pass -s SERIAL" >&2
    exit 2
  fi
  SERIAL="$DEVICE_LIST"
fi
SERIAL_ARGS=(-s "$SERIAL")

if [[ -z "$ARTIFACT_DIR" ]]; then
  ARTIFACT_DIR="$REPO_ROOT/output/android-use-e2e/proxy-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$ARTIFACT_DIR"
RESULTS="$ARTIFACT_DIR/results.txt"
: > "$RESULTS"

record() {
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" | tee -a "$RESULTS"
}

wait_for_flow() {
  local needle="$1"
  local output="$2"
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    "$BIN" proxy dump "${SERIAL_ARGS[@]}" --format json -o "$output" >/dev/null
    if grep -q "$needle" "$output"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

device_http_request() {
  local path="$1"
  local expected_status="$2"
  local output="$3"
  adb -s "$SERIAL" shell \
    "(printf 'GET http://example.com/$path HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n'; sleep 2) | nc -W 5 127.0.0.1 $PORT" \
    > "$output"
  grep -q "HTTP/1.1 $expected_status" "$output"
}

ORIGINAL_PROXY="$(adb -s "$SERIAL" shell settings get global http_proxy | tr -d '[:space:]')"
PROXY_STARTED=0
cleanup() {
  local rc=$?
  if [[ "$PROXY_STARTED" -eq 1 ]]; then
    "$BIN" proxy stop "${SERIAL_ARGS[@]}" --port "$PORT" > "$ARTIFACT_DIR/proxy-stop-cleanup.txt" 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

SMOKE_ID="android-use-http-$(date +%s)"
RELOAD_ID="android-use-reload-$(date +%s)"
HTTPS_ID="android-use-https-$(date +%s)"
MOCK_FILE="$ARTIFACT_DIR/mock.json"
RELOAD_FILE="$ARTIFACT_DIR/mock-reload.json"
printf '%s\n' "[{\"url\":\"example.com/$SMOKE_ID\",\"status\":201,\"body\":\"{\\\"source\\\":\\\"device\\\"}\"}]" > "$MOCK_FILE"
printf '%s\n' "[{\"url\":\"example.com/$RELOAD_ID\",\"status\":202,\"body\":\"{\\\"source\\\":\\\"reload\\\"}\"}]" > "$RELOAD_FILE"

"$BIN" doctor --format json "${SERIAL_ARGS[@]}" > "$ARTIFACT_DIR/doctor.json"
"$BIN" proxy doctor "${SERIAL_ARGS[@]}" > "$ARTIFACT_DIR/proxy-doctor.txt"
"$BIN" proxy start "${SERIAL_ARGS[@]}" --port "$PORT" --mock "$MOCK_FILE" --skip-cert-check \
  > "$ARTIFACT_DIR/proxy-start.txt"
PROXY_STARTED=1
"$BIN" proxy status "${SERIAL_ARGS[@]}" > "$ARTIFACT_DIR/proxy-status.txt"
record PASS start "proxy running on port $PORT"

ACTIVE_PROXY="$(adb -s "$SERIAL" shell settings get global http_proxy | tr -d '[:space:]')"
[[ "$ACTIVE_PROXY" == "127.0.0.1:$PORT" ]]
adb -s "$SERIAL" reverse --list > "$ARTIFACT_DIR/adb-reverse-active.txt"
grep -q "tcp:$PORT tcp:$PORT" "$ARTIFACT_DIR/adb-reverse-active.txt"
record PASS device-route "global proxy and adb reverse configured"

device_http_request "$SMOKE_ID" 201 "$ARTIFACT_DIR/http-response.txt"
wait_for_flow "$SMOKE_ID" "$ARTIFACT_DIR/http.json"
grep -q '"status": 201' "$ARTIFACT_DIR/http.json"
record PASS http "device-side request captured through adb reverse with mock status 201"

"$BIN" proxy mock "${SERIAL_ARGS[@]}" "$RELOAD_FILE" > "$ARTIFACT_DIR/mock-reload.txt"
device_http_request "$RELOAD_ID" 202 "$ARTIFACT_DIR/reload-response.txt"
wait_for_flow "$RELOAD_ID" "$ARTIFACT_DIR/reload.json"
grep -q '"status": 202' "$ARTIFACT_DIR/reload.json"
"$BIN" proxy dump "${SERIAL_ARGS[@]}" --format har -o "$ARTIFACT_DIR/capture.har" \
  > "$ARTIFACT_DIR/har-dump.txt"
grep -q '"entries"' "$ARTIFACT_DIR/capture.har"
record PASS mock-export "runtime mock reload and JSON/HAR export verified"

if [[ "$RUN_HTTPS" -eq 1 ]]; then
  set +e
  "$BIN" proxy cert status "${SERIAL_ARGS[@]}" > "$ARTIFACT_DIR/cert-status.txt" 2>&1
  CERT_RC=$?
  "$BIN" deeplink "https://example.com/$HTTPS_ID" --wait "${SERIAL_ARGS[@]}" \
    > "$ARTIFACT_DIR/https-deeplink.txt" 2>&1
  wait_for_flow "$HTTPS_ID" "$ARTIFACT_DIR/https.json"
  set -e
  if grep -q "$HTTPS_ID" "$ARTIFACT_DIR/https.json"; then
    record PASS https "device HTTPS decrypted and captured; cert-status exit=$CERT_RC"
  else
    record WARN https "HTTPS not decrypted; inspect cert-status.txt and app pinning; cert-status exit=$CERT_RC"
  fi
fi

"$BIN" proxy stop "${SERIAL_ARGS[@]}" --port "$PORT" > "$ARTIFACT_DIR/proxy-stop.txt"
PROXY_STARTED=0
RESTORED_PROXY="$(adb -s "$SERIAL" shell settings get global http_proxy | tr -d '[:space:]')"
[[ "$RESTORED_PROXY" == "$ORIGINAL_PROXY" ]]
adb -s "$SERIAL" reverse --list > "$ARTIFACT_DIR/adb-reverse-stopped.txt"
if grep -q "tcp:$PORT" "$ARTIFACT_DIR/adb-reverse-stopped.txt"; then
  echo "adb reverse tcp:$PORT was not removed" >&2
  exit 1
fi
record PASS cleanup "proxy restored to '$ORIGINAL_PROXY' and reverse removed"
record PASS overall "artifacts=$ARTIFACT_DIR"
