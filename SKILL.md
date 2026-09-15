---
name: android-use
description: "Operate or troubleshoot Android devices through ADB with android-use: UI actions, screenshots, logs and proxy capture. Supports connected USB devices, emulators and network ADB targets."
---

# Android device operation

Use the installed `android-use` command. If it is not on PATH, the default entry
is `~/.local/bin/android-use`; a source checkout also runs with `./android-use`.
On Windows, invoke the checkout's entrypoint with Python.

Use `android-use devices` and select an online ADB serial. USB, emulators and
network connections are accepted; with multiple targets pass `-s SERIAL` or set
`ANDROID_USE_SERIAL`. Pair/connect network devices with ADB first.
Run `android-use doctor --check-ui` for device/helper readiness. Core commands
need Python and ADB, without a host architecture restriction.

## Observe and act

- Use `dump`, then an observed coordinate tap. If a target is
  already known, `tap --text` avoids an extra preliminary dump; scroll-and-tap
  uses `tap --text ... --scroll-if-needed`. Old bounds expire after UI changes.
- Verify actions with the resulting tree or screenshot. Use `wait` for an
  expected text, Activity or visual state instead of repeated manual dumps.
- Stock ADB `input text` is ASCII-oriented. Unicode needs an explicitly available
  input method or manual input; do not transliterate it.
- Use ADB for App lifecycle, Back/Home and file transfer. A successful Intent
  dispatch is not proof of the destination UI. Inspect current state afterward.
- If the tree is empty or contradicts the Activity, check a screenshot for
  lockscreen/system UI. Never try to bypass a device credential prompt.

## Conditional workflows

Read [operations](references/operations.md) for ADB file/Intent recipes, semantic
waits and scrolling, screenshot/capture, persistent logcat, failure evidence,
or proxy capture.
Use `android-use <command> --help` for full flags.
If a command returns `evidence=`, inspect it before collecting the same evidence
again. Inspect screenshots directly. Stop only the
logcat/proxy sessions started for the task; proxy stop restores its saved settings.
Logs, screenshots and captures may contain credentials or personal data. Review
them before sharing; `logcat --package` labels the file but still captures all logs.

App-specific packages, entry points and assertions belong to the consumer project,
not this platform Skill.
