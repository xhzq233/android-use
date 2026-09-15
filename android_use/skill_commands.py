"""Install the bundled agent instructions without a device connection."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SKILL_FILES = (Path("SKILL.md"), Path("references/operations.md"))


def command_skill_install(args: argparse.Namespace) -> int:
    base = Path.home() if args.user else Path(args.project).expanduser().resolve()
    destination = base / ".agents" / "skills" / "android-use"
    try:
        if destination.is_symlink():
            raise OSError(f"Skill destination is a symlink; leave it unchanged: {destination}")
        if destination.exists() and not args.force:
            raise OSError(f"Skill already exists: {destination}; use --force to update its bundled documents")
        for relative in SKILL_FILES:
            target = destination / relative
            if target.is_symlink() or target.parent.is_symlink():
                raise OSError(f"Refusing to overwrite a symlink: {target}")
        for relative in SKILL_FILES:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE_ROOT / relative, target)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed Skill: {destination}")
    return 0
