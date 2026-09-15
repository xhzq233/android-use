# android-use

`android-use` is a source-distributed CLI for inspecting and automating Android devices over
ADB. It supports USB, emulator and network devices, UI hierarchy inspection, semantic taps and waits,
screenshots, short visual captures, Deep Links, logcat, and optional proxy capture.

## Install

With Git and Python 3.9+ installed, run in a Bash environment:

```bash
curl -fsSL https://raw.githubusercontent.com/xhzq233/android-use/main/scripts/install.sh | bash
```

The installer shallow-clones the repository into `~/.local/share/android-use`
and creates only a symlink at `~/.local/bin/android-use`. The repository does not
go into `bin`. Source installation keeps the Python modules, UI Server JAR and
optional helpers together; there is no separate runtime archive or pip package.
The bundled JAR is deployed automatically on the first UI dump: no JDK,
Android SDK build tools, or separately installed APK is needed to use it.

Re-run the installer to update after stopping your logcat/proxy sessions. Local
checkout edits are not overwritten. Override `ANDROID_USE_INSTALL_ROOT` and
`ANDROID_USE_BIN_DIR` to choose other locations. The installer does not install
ADB or edit your shell config; if needed, add `~/.local/bin` to `PATH` yourself
or invoke `~/.local/bin/android-use` directly. A source checkout also runs with
`./android-use`.

## Agent Skill

```bash
android-use skill install --project       # current directory/.agents/skills/android-use
android-use skill install --user          # ~/.agents/skills/android-use
```

Choose either install scope; `--project /path/to/project` selects another project.
Only `SKILL.md` and `references/` are copied, not the CLI or repository. These
commands need no ADB or device. To refresh a previously installed Skill after
updating the CLI, repeat its install command with `--force`; this overwrites
bundled documents, including any local edits to them.

## Requirements

- Python 3.9 or newer
- `adb` on `PATH`, or `ANDROID_USE_ADB` pointing to the executable
- An authorized ADB device; use `-s <serial>` when more than one is online

Core commands do not impose an OS or CPU architecture allowlist. On Windows,
invoke the entrypoint with `python /path/to/android-use/android-use`.
Network devices must already be paired/connected with ADB. The tool does not
enable wireless debugging or expose a device on the network for you.

No third-party Python packages are required for core commands. Optional capture
contact sheets use macOS frameworks and Xcode Command Line Tools, on either Mac
architecture. Persistent logcat supervision and background proxy currently use
POSIX process support; raw `adb logcat` remains available on Windows. Proxy also
requires `mitmdump`. Windows is not yet end-to-end validated.

## Use

```bash
android-use devices
android-use doctor --check-ui
android-use dump
android-use tap --text Settings
android-use screenshot /tmp/android-use.png
```

Set `ANDROID_USE_SERIAL` to choose a default device. Use `android-use --help`
or `android-use <command> --help` for the full command surface.
Screenshots save the original PNG for direct visual inspection.

## Captured data

Default runtime state and captures are private to your account under
`~/.android-use/`. Screenshot output paths can be chosen explicitly.
Logcat captures all device logs, even with `--package` (a file label, not a filter).
Proxy records raw headers and bodies. Screenshots, UI dumps, logs and network
captures can contain credentials or personal data: review before sharing and do
not commit them. No automatic upload or telemetry is performed by the tool.

Maintainer build and verification instructions are in the source repository's
`docs/DEVELOPMENT.md`.

## License

[MIT](LICENSE).
