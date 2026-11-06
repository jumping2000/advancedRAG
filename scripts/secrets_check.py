#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""Secret scan in files: keys and tokens with a recognizable format, versioned .env files.

Usage:
  python scripts/secrets_check.py                 # files changed since the merge-base with main (or --base <ref>)
  python scripts/secrets_check.py --all           # all files tracked by git
  python scripts/secrets_check.py src config.yml  # the given files and folders

- Error (exit 1): private keys, tokens with a known prefix (AWS, GitHub, GitLab, Slack, Stripe, Google, Anthropic,
  OpenAI), versioned or changed .env files (not .env.example, .env.sample, .env.template).
- Warning: passwords, secrets, tokens or keys assigned to a literal string of at least 8 characters.
A line with "secrets-check: ignore" is not checked (false positive, justified in the review); files that cannot be
annotated (PEM fixtures, a .env.development versioned by convention) go into .secrets-ignore, one glob per line. The
value found is never printed. It is a minimal, fast net: for the full repository history use a dedicated tool (for
example gitleaks) in CI. Exit code: 0 no secrets; 1 secrets found; 2 wrong usage or git not usable.
Standard library + git only, cross-platform.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

CERTAIN = [
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"), "private key"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "GitHub token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "GitLab token"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}\b"), "Stripe live key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"), "OpenAI API key"),
]
GENERIC = re.compile(r"(?i)(?<![a-z])(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|"
                     r"client[_-]?secret)(?![a-z])[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']")
PLACEHOLDER = re.compile(r"(?i)^(<.*>|\$\{.*\}|\{\{.*\}\}|x{4,}|\*{4,}|changeme|change_me|example\w*|dummy\w*|"
                         r"placeholder|your[_-].*|test\w*|fake\w*|secret|password)$")
ENV_FILE = re.compile(r"(?i)(^|/)\.env(\.[\w.-]+)?$")
EXAMPLE = re.compile(r"(?i)\.(example|sample|template)$")  # .env.example, .env.local.example
IGNORE_MARK = "secrets-check: ignore"
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", "bin", "obj"}
MAX_BYTES = 1_000_000


def git(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")  # quotePath: non-ASCII paths as they are
    except OSError:
        return 1, ""
    return r.returncode, r.stdout.strip()


def files_from_git(all_files: bool, base: str | None) -> list[str] | None:
    """Files to check according to git; None if git or the base ref are not usable."""
    if git("rev-parse", "--is-inside-work-tree")[0] != 0:
        return None
    if all_files:
        return git("ls-files")[1].splitlines()
    for ref in ([base] if base else ["main", "master", "origin/main", "origin/master"]):
        if git("rev-parse", "--verify", "--quiet", ref)[0] == 0:
            code, mb = git("merge-base", "HEAD", ref)
            if code != 0:
                return None
            diff = git("diff", "--name-only", "--diff-filter=d", mb)[1]
            new = git("ls-files", "--others", "--exclude-standard")[1]
            return sorted({f for f in (diff + "\n" + new).splitlines() if f.strip()})
    return None


def expand(paths: list[str]) -> list[str]:
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += [f.as_posix() for f in sorted(p.rglob("*")) if f.is_file() and not SKIP & set(f.parts)]
        else:
            out.append(p.as_posix())
    return out


def check_file(path: str) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a file, without reporting the value found."""
    err: list[str] = []
    warn: list[str] = []
    if ENV_FILE.search(path.replace("\\", "/")) and not EXAMPLE.search(path):
        err.append(f"{path}: .env file in the repository (secrets stay outside the repo)")
    p = Path(path)
    if not p.is_file() or p.stat().st_size > MAX_BYTES:
        return err, warn
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        return err, warn  # binary
    for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if IGNORE_MARK in line:
            continue
        for rule, name in CERTAIN:
            if rule.search(line):
                err.append(f"{path}:{n}: {name}")
        for m in GENERIC.finditer(line):
            if not PLACEHOLDER.match(m.group(2)):
                warn.append(f"{path}:{n}: possible secret assigned to '{m.group(1)}'")
    return err, warn


def exclusions() -> list[str]:
    """Globs of .secrets-ignore (one per line, # for comments)."""
    p = Path(".secrets-ignore")
    if not p.is_file():
        return []
    return [r.strip() for r in p.read_text(encoding="utf-8").splitlines() if r.strip() and not r.startswith("#")]


def main(argv: list[str]) -> int:
    base = argv[argv.index("--base") + 1] if "--base" in argv and argv.index("--base") + 1 < len(argv) else None
    paths = [a for a in argv if not a.startswith("--") and a != base]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 2
    if paths:
        files = expand(paths)
    else:
        files = files_from_git("--all" in argv, base)
        if files is None:
            print("[ERROR] git not usable or no base ref: use --base <ref>, --all or give the paths")
            return 2
    errors: list[str] = []
    warnings: list[str] = []
    excluded = exclusions()
    files = [f for f in files if not any(fnmatch.fnmatch(f.replace("\\", "/"), g) for g in excluded)]
    for f in files:
        e, w = check_file(f)
        errors += e
        warnings += w
    for w in warnings:
        print(f"[WARN] {w}")
    for e in errors:
        print(f"[ERROR] {e}")
    print(f"SECRETS: {'OK' if not errors else f'{len(errors)} secrets'} ({len(files)} files checked)")
    return 1 if errors else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
