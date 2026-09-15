#!/usr/bin/env bash
# Install a source checkout and a command symlink; leave shell configuration alone.
set -euo pipefail

repo_url="${ANDROID_USE_REPO_URL:-https://github.com/xhzq233/android-use.git}"
ref="${ANDROID_USE_REF:-main}"
install_root="${ANDROID_USE_INSTALL_ROOT:-$HOME/.local/share/android-use}"
bin_dir="${ANDROID_USE_BIN_DIR:-$HOME/.local/bin}"
install_skill=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-skill) install_skill=0 ;;
        --help|-h)
            echo "Usage: install.sh [--no-skill]"
            echo "Install CLI to ~/.local/share/android-use with an entry in ~/.local/bin."
            echo "Default: link the Skill at ~/.agents/skills/android-use."
            echo "--no-skill: leave skills directories untouched; link the checkout manually later."
            echo "Re-run to update. Requires Git and Python 3.9+. Does not install ADB."
            echo "Overrides: ANDROID_USE_INSTALL_ROOT, ANDROID_USE_BIN_DIR, ANDROID_USE_REPO_URL, ANDROID_USE_REF."
            exit 0
            ;;
        *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
    shift
done
command -v git >/dev/null || { echo "Install Git first." >&2; exit 1; }
command -v python3 >/dev/null || { echo "Install Python 3.9+ first." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9+ is required")'
mkdir -p "$(dirname "$install_root")" "$bin_dir"
install_root="$(cd "$(dirname "$install_root")" && pwd)/$(basename "$install_root")"
bin_dir="$(cd "$bin_dir" && pwd)"
entry="$bin_dir/android-use"

if [[ -e "$entry" || -L "$entry" ]]; then
    if [[ ! -L "$entry" ]] || [[ "$(readlink "$entry")" != "$install_root/android-use" ]]; then
        echo "Command already exists: $entry. Move it aside or choose ANDROID_USE_BIN_DIR." >&2
        exit 1
    fi
fi

if [[ -e "$install_root" || -L "$install_root" ]]; then
    if [[ ! -d "$install_root/.git" ]]; then
        echo "Not an installed checkout: $install_root. Choose an empty install location." >&2
        exit 1
    fi
    if [[ "$(git -C "$install_root" remote get-url origin)" != "$repo_url" ]]; then
        echo "Checkout has a different origin; leave it unchanged: $install_root" >&2
        exit 1
    fi
    if [[ -n "$(git -C "$install_root" status --porcelain --untracked-files=normal)" ]]; then
        echo "Checkout has local changes; leave it unchanged: $install_root" >&2
        exit 1
    fi
    git -C "$install_root" fetch origin "$ref"
    git -C "$install_root" merge --ff-only FETCH_HEAD
else
    git clone --depth 1 --branch "$ref" -- "$repo_url" "$install_root"
fi
ln -sfn "$install_root/android-use" "$entry"
echo "Installed: $entry"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Add $bin_dir to PATH, or invoke $entry directly." ;;
esac
skill_link="$HOME/.agents/skills/android-use"
echo "Skill source: $install_root"
if [[ "$install_skill" -eq 1 ]]; then
    if [[ -e "$skill_link" || -L "$skill_link" ]]; then
        echo "Existing Skill path kept: $skill_link"
    else
        mkdir -p "$(dirname "$skill_link")"
        ln -s "$install_root" "$skill_link"
        echo "Linked Skill: $skill_link"
    fi
else
    echo "Skipped default Skill link (--no-skill); existing links are unchanged."
fi
