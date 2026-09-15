# Development

## Verification

```bash
python3 -m compileall -q android_use
python3 -m py_compile android-use
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n device-ui-server/build.sh scripts/install.sh scripts/e2e_deeplink.sh scripts/e2e_proxy.sh
git diff --check
./android-use doctor --check-ui
```

The primary acceptance check is a real ADB device completing
`devices → dump → tap → dump → screenshot`. Keep unit tests focused on selection, parsing,
and recovery logic; do not add tests that only compare help strings.

## UiAutomation helper

When `device-ui-server/src/io/github/xhzq233/androiduse/UiAutomationServerTest.java` changes,
rebuild the bundled JAR:

```bash
bash device-ui-server/build.sh
```

The build needs a JDK, `zip`, Android SDK platform `android-35`, and build-tools `36.0.0`.
`ANDROID_HOME` or `ANDROID_SDK_ROOT` may select the SDK; the platform and build-tools versions
can be overridden with `ANDROID_USE_ANDROID_PLATFORM` and `ANDROID_USE_BUILD_TOOLS`.

After rebuilding, validate a cold dump, warm dump, forced helper recovery, and stock fallback
on a USB device before committing the Java source and JAR together.

## Distribution

`scripts/install.sh` installs a shallow Git checkout and a command symlink.
Use `ANDROID_USE_REPO_URL`, `ANDROID_USE_REF`, `ANDROID_USE_INSTALL_ROOT` and
`ANDROID_USE_BIN_DIR` to exercise installation and updates in temporary directories.
Verify the default Skill link, `--no-skill`, preservation of existing Skill paths,
and manual project links, then real device selection, dump and screenshot.
Keep tests away from the user's installation. The Skill link points to the
checkout's root `SKILL.md` and `references/`; no separate copy or CLI subcommand
is involved.

Before publishing, scan the selected Git history as well as the current tree for
credentials, private identities and internal references. The project is distributed
under the MIT license in `LICENSE`.
