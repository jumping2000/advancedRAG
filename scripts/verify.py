#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""Single project verification: format, lint, type-check, tests, secrets.

Exit code 0 = all green. Standard library only; runs the same on Windows, Linux, macOS.
Usage: python scripts/verify.py            # all steps, stops at the first red
       python scripts/verify.py --fast     # only the steps marked fast
       python scripts/verify.py test lint  # only the listed steps
       python scripts/verify.py --keep-going
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

# Adapt this list to the project: (name, command, fast?)
STEPS = [
    # Backend and frontend steps (format, lint, types, test) are added by the first task of phase 1,
    # when the code exists and each command has been run by hand.
    ("secrets", [sys.executable, "scripts/secrets_check.py", "--all"], True),
]


def run_step(name: str, cmd: list[str]) -> bool:
    exe = shutil.which(cmd[0])  # on Windows it resolves .exe/.cmd through PATHEXT
    if exe is None:
        print(f"[FAIL] {name}: command not found: {cmd[0]}")
        return False
    start = time.monotonic()
    env = {**os.environ, "PYTHONUTF8": "1"}
    ok = subprocess.run([exe, *cmd[1:]], env=env).returncode == 0
    print(f"[{'OK' if ok else 'FAIL'}] {name} ({time.monotonic() - start:.1f}s)")
    return ok


def main(argv: list[str]) -> int:
    fast = "--fast" in argv
    keep_going = "--keep-going" in argv
    wanted = [a for a in argv if not a.startswith("--")]
    steps = [s for s in STEPS if (not wanted or s[0] in wanted) and (s[2] or not fast)]
    if not steps:
        print("No step selected")
        return 2
    failed = []
    for name, cmd, _ in steps:
        if not run_step(name, cmd):
            failed.append(name)
            if not keep_going:
                break
    print("VERIFY: " + ("GREEN" if not failed else "RED -> " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
