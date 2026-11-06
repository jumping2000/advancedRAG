#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""Deterministic inventory of an existing codebase (starting point for the map, brownfield).

Usage: python scripts/scan_repo.py [root] [--json]

Read-only. Lists: detected manifests and stack, languages (files and lines), top-level structure, test folders,
CI configurations, likely entry points, existing documentation.
It is an inventory, not understanding: modules and responsibilities must be read in the code.
Uses `git ls-files` when available, otherwise walks the folders. Standard library only, cross-platform.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", "bin", "obj",
        ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".gradle"}
LANGUAGES = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
             ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".go": "Go", ".rb": "Ruby", ".php": "PHP",
             ".rs": "Rust", ".c": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".h": "C/C++", ".hpp": "C++",
             ".swift": "Swift", ".sql": "SQL", ".sh": "Shell", ".bash": "Shell", ".bats": "Shell (bats)",
             ".ps1": "PowerShell", ".psm1": "PowerShell", ".scala": "Scala", ".dart": "Dart", ".ex": "Elixir",
             ".exs": "Elixir", ".fs": "F#", ".vue": "Vue", ".svelte": "Svelte", ".tf": "Terraform", ".hcl": "HCL"}
MANIFEST = {"pyproject.toml": "Python", "requirements.txt": "Python", "setup.py": "Python", "uv.lock": "Python (uv)",
            "package.json": "Node", "pnpm-lock.yaml": "Node (pnpm)", "yarn.lock": "Node (yarn)",
            "go.mod": "Go", "pom.xml": "Java (Maven)", "build.gradle": "Java/Kotlin (Gradle)",
            "build.gradle.kts": "Java/Kotlin (Gradle)", "Cargo.toml": "Rust", "composer.json": "PHP",
            "Gemfile": "Ruby", "Dockerfile": "Docker", "Containerfile": "Docker", "docker-compose.yml": "Docker Compose",
            "docker-compose.yaml": "Docker Compose", "compose.yaml": "Docker Compose", "compose.yml": "Docker Compose",
            "Chart.yaml": "Helm", "kustomization.yaml": "Kubernetes (kustomize)", "helmfile.yaml": "Helm (helmfile)",
            "CMakeLists.txt": "C/C++ (CMake)", "meson.build": "C/C++ (Meson)", "conanfile.txt": "C/C++ (Conan)",
            "vcpkg.json": "C/C++ (vcpkg)", "Makefile": "make"}
MANIFEST_EXTENSIONS = {".sln": ".NET", ".csproj": ".NET", ".tf": "Terraform"}
ENTRY = {"main.py", "app.py", "manage.py", "__main__.py", "index.js", "index.ts", "server.js", "server.ts",
         "main.go", "Program.cs", "Main.java", "Application.java", "main.rs", "lib.rs"}
TEST_DIRS = {"tests", "test", "__tests__", "spec", "e2e"}


def list_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "--cached", "--others",
                        "--exclude-standard"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0 and r.stdout.strip():
        return sorted(f for f in r.stdout.splitlines() if not SKIP & set(Path(f).parts))
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and not SKIP & set(p.relative_to(root).parts))


def count_lines(p: Path) -> int:
    try:
        if p.stat().st_size > 2_000_000:
            return 0
        with p.open(encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def scan(root: Path) -> dict:
    files = list_files(root)
    lang_files, lang_lines = Counter(), Counter()
    stack, entry, test_dirs, ci, docs, top = set(), [], set(), [], [], Counter()
    for f in files:
        p = Path(f)
        name, ext = p.name, p.suffix.lower()
        top[p.parts[0] if len(p.parts) > 1 else "(root)"] += 1
        if name in MANIFEST:
            stack.add(MANIFEST[name])
        if ext in MANIFEST_EXTENSIONS:
            stack.add(MANIFEST_EXTENSIONS[ext])
        if ext in LANGUAGES:
            lang_files[LANGUAGES[ext]] += 1
            lang_lines[LANGUAGES[ext]] += count_lines(root / f)
        if name in ENTRY:
            entry.append(f)
        test_dirs.update(part for part in p.parts[:-1] if part in TEST_DIRS)
        if f.startswith(".github/workflows/") or name in ("Jenkinsfile", ".gitlab-ci.yml", "azure-pipelines.yml"):
            ci.append(f)
        if ext == ".md" and (len(p.parts) == 1 or p.parts[0] == "docs"):
            docs.append(f)
    return {
        "total_files": len(files),
        "stack": sorted(stack),
        "languages": [{"name": n, "files": lang_files[n], "lines": lang_lines[n]} for n, _ in lang_lines.most_common()],
        "top_level": [{"path": k, "files": v} for k, v in top.most_common()],
        "test_folders": sorted(test_dirs),
        "ci": ci,
        "likely_entry_points": entry[:20],
        "documentation": docs[:30],
    }


def show(r: dict) -> None:
    print(f"Total files: {r['total_files']}")
    print("Detected stack: " + (", ".join(r["stack"]) or "(no known manifest)"))
    print("Languages: " + (", ".join(f"{x['name']} ({x['files']} files, {x['lines']} lines)"
                                     for x in r["languages"]) or "(none)"))
    print("Top level: " + ", ".join(f"{x['path']} ({x['files']})" for x in r["top_level"][:15]))
    print("Test folders: " + (", ".join(r["test_folders"]) or "(none)"))
    print("CI: " + (", ".join(r["ci"]) or "(none)"))
    print("Likely entry points: " + (", ".join(r["likely_entry_points"]) or "(none)"))
    print("Documentation: " + (", ".join(r["documentation"]) or "(none)"))


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    root = Path(args[0]) if args else Path(".")
    if not root.is_dir():
        print(f"[ERROR] folder not found: {root}")
        return 2
    r = scan(root)
    print(json.dumps(r, ensure_ascii=False, indent=2)) if "--json" in argv else show(r)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
