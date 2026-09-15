#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SDK_ROOT="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
PLATFORM="${ANDROID_USE_ANDROID_PLATFORM:-android-35}"
BUILD_TOOLS="${ANDROID_USE_BUILD_TOOLS:-36.0.0}"
ANDROID_JAR="$SDK_ROOT/platforms/$PLATFORM/android.jar"
UIAUTOMATOR_JAR="$SDK_ROOT/platforms/$PLATFORM/uiautomator.jar"
TEST_BASE_JAR="$SDK_ROOT/platforms/$PLATFORM/optional/android.test.base.jar"
D8="$SDK_ROOT/build-tools/$BUILD_TOOLS/d8"
SOURCE="$ROOT/src/io/github/xhzq233/androiduse/UiAutomationServerTest.java"
BUILD_DIR="$ROOT/build"
OUTPUT="${1:-$ROOT/../android_use/resources/android-use-ui-server.jar}"
ZIP="$(command -v zip || true)"

if [[ -z "$ZIP" ]]; then
  echo "missing build input: zip" >&2
  exit 1
fi

for required in "$ANDROID_JAR" "$UIAUTOMATOR_JAR" "$TEST_BASE_JAR" "$D8" "$SOURCE"; do
  if [[ ! -e "$required" ]]; then
    echo "missing build input: $required" >&2
    exit 1
  fi
done

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/classes" "$BUILD_DIR/dex" "$(dirname "$OUTPUT")"
OUTPUT="$(cd "$(dirname "$OUTPUT")" && pwd)/$(basename "$OUTPUT")"

javac \
  -source 8 \
  -target 8 \
  -classpath "$ANDROID_JAR:$UIAUTOMATOR_JAR:$TEST_BASE_JAR" \
  -d "$BUILD_DIR/classes" \
  "$SOURCE"

jar cf "$BUILD_DIR/classes.jar" -C "$BUILD_DIR/classes" .
"$D8" \
  --release \
  --min-api 18 \
  --lib "$ANDROID_JAR" \
  --classpath "$UIAUTOMATOR_JAR" \
  --classpath "$TEST_BASE_JAR" \
  --output "$BUILD_DIR/dex" \
  "$BUILD_DIR/classes.jar"
rm -f "$OUTPUT"
(
  cd "$BUILD_DIR/dex"
  TZ=UTC touch -t 198001010000 classes.dex
  TZ=UTC "$ZIP" -X -q "$OUTPUT" classes.dex
)

echo "$OUTPUT"
