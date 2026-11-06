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
    ("backend-format", ["uv", "run", "--directory", "backend", "ruff", "format", "--check", "."], True),
    ("backend-lint", ["uv", "run", "--directory", "backend", "ruff", "check", "."], True),
    ("backend-types", ["uv", "run", "--directory", "backend", "mypy"], False),
    ("frontend-format", ["npm", "--prefix", "frontend", "run", "-s", "format:check"], True),
    ("frontend-lint", ["npm", "--prefix", "frontend", "run", "-s", "lint"], True),
    ("frontend-types", ["npm", "--prefix", "frontend", "run", "-s", "typecheck"], False),
    ("frontend-test", ["npm", "--prefix", "frontend", "run", "-s", "test", "--", "--reporter=default",
                       "--reporter=junit", "--outputFile.junit=../reports/junit-frontend.xml"], False),
    # Postgres and Redis for the backend tests (compose.test.yaml); needs Docker
    ("test-services", ["docker", "compose", "-f", "compose.test.yaml", "up", "-d", "--wait"], False),
    ("backend-test", ["uv", "run", "--directory", "backend", "pytest", "-q",
                      "--junitxml=../reports/junit-backend.xml"], False),
    # Full docker compose stack, built from the working tree
    ("e2e", ["uv", "run", "--directory", "backend", "pytest", "-q", "-m", "e2e",
             "--junitxml=../reports/junit-e2e.xml"], False),
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
