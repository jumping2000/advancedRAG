#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""Project state according to the playbook and suggested next step.

Usage: python scripts/state.py [--json] [--spec <id>] [root]
Read-only. Flow: sdd-start (init, brainstorming, spec) -> sdd-plan (plan with the tasks; spec and plan merged into
main with the design PR, which is the approval) -> sdd-task for every task (one branch and one PR each; independent
tasks with sdd-parallel) -> feature closed by the last task (area page, Status: implemented). Brownfield: sdd-map first.
A spec or plan that differs from main outside a task or fix branch is a revision in progress: sdd-start or sdd-plan.
"On main" is read from git: a task is done when its box is ticked in the plan on main (its PR was merged); a task is
ready when it is not done and all its dependencies are done. Two or more ready tasks with disjoint files -> sdd-parallel.
With several active specs the NEXT line refers to the spec given with --spec, the one whose folder name appears in the
current branch, or the only active one; otherwise it asks to choose. Without git, the working copy counts as main.
Standard library only (+ git, if present), cross-platform.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the project: it would count as a changed file
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_lint  # noqa: E402

SOURCES = {".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue", ".svelte", ".java", ".kt", ".scala", ".cs", ".fs", ".vb",
           ".go", ".rb", ".php", ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".swift", ".m", ".dart", ".ex", ".exs",
           ".lua", ".r", ".jl", ".hs", ".pl", ".sql", ".sh", ".bash", ".ps1", ".psm1", ".tf", ".hcl"}
# Infrastructure as code counts as existing code too (a Compose stack, a Helm chart, a Dockerfile).
INFRA = {"Dockerfile", "Containerfile", "compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml",
         "Chart.yaml", "kustomization.yaml", "helmfile.yaml", "CMakeLists.txt", "Makefile"}
IGNORE = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", "bin", "obj",
          "scripts", "docs", ".github", ".vscode"}
CLOSED = ("implemented", "superseded")
BUG_CLOSED = ("fixed", "verified")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def git(root: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    except OSError:
        return 1, ""
    return r.returncode, r.stdout


def main_ref(root: Path) -> str | None:
    return next((ref for ref in ("main", "master") if git(root, "rev-parse", "--verify", "--quiet", ref)[0] == 0),
                None)


def on_main(root: Path, ref: str | None, rel: str) -> str | None:
    """Content of the file on main (the working copy when there is no git or no main), None if missing there."""
    if ref is None:
        return _read(root / rel) or None
    code, text = git(root, "show", f"{ref}:{rel}")
    return text if code == 0 else None


def has_code(root: Path, threshold: int = 3) -> bool:
    n = 0
    for _, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        n += sum(1 for f in files if Path(f).suffix in SOURCES or f in INFRA)
        if n >= threshold:
            return True
    return False


def ready_tasks(tasks: list[dict]) -> list[dict]:
    done = {t["id"] for t in tasks if t["done"]}
    return [t for t in tasks if not t["done"] and all(d in done for d in t["depends"])]


def parallel_group(ready: list[dict], limit: int = 3) -> list[dict]:
    """Ready tasks with pairwise disjoint files, in plan order, at most `limit`."""
    group: list[dict] = []
    for t in ready:
        if all(not set(t["files"]) & set(g["files"]) for g in group):
            group.append(t)
        if len(group) == limit:
            break
    return group


def same(a: str | None, b: str | None) -> bool:
    return (a or "").replace("\r\n", "\n") == (b or "").replace("\r\n", "\n")


def analyze_spec(root: Path, ref: str | None, d: Path, branch: str = "") -> dict:
    rel = d.relative_to(root).as_posix()
    spec, plan = _read(d / "spec.md"), _read(d / "plan.md")
    spec_main, plan_main = on_main(root, ref, f"{rel}/spec.md"), on_main(root, ref, f"{rel}/plan.md")
    tasks = spec_lint.parse_tasks(plan_main or "")
    sid = spec_lint.spec_id(spec)
    revision = None  # spec or plan changed outside a task branch, once the design is on main
    if ref and spec_main and plan_main and not branch.startswith(("task/", "fix/")):
        revision = "spec" if not same(spec, spec_main) else ("plan" if not same(plan, plan_main) else None)
    return {
        "id": d.name, "title_id": f"SPEC-{sid}" if sid else None, "status": spec_lint.status(spec) or "n/a",
        "plan": bool(plan), "design_on_main": bool(spec_main and plan_main), "revision": revision,
        "tasks": [{"id": t["id"], "done": t["done"], "depends": t["depends"], "files": t["files"]} for t in tasks],
        "tasks_done": sum(t["done"] for t in tasks), "tasks_total": len(tasks),
    }


def next_for_spec(s: dict) -> dict:
    i = s["id"]
    if s["status"] in ("draft", "n/a"):
        return {"skill": "sdd-start", "spec": i, "reason": "spec to complete and confirm (Status: approved)"}
    if not s["plan"]:
        return {"skill": "sdd-plan", "spec": i, "reason": "plan with the vertical tasks to write and confirm"}
    if not s["design_on_main"]:
        return {"skill": "sdd-plan", "spec": i,
                "reason": "spec and plan not on main yet: open the design PR and ask whether to merge it"}
    if s.get("revision") == "spec":
        return {"skill": "sdd-start", "spec": i,
                "reason": "spec revision in progress: confirm it, revise the plan if the ACs changed, merge the revision PR"}
    if s.get("revision") == "plan":
        return {"skill": "sdd-plan", "spec": i, "reason": "plan revision in progress: confirm it and merge the revision PR"}
    if not s["tasks"]:
        return {"skill": "sdd-plan", "spec": i, "reason": "the plan on main has no task (- [ ] T-nn ...)"}
    ready = ready_tasks(s["tasks"])
    if not ready and s["tasks_done"] == s["tasks_total"]:
        return {"skill": "sdd-task", "spec": i,
                "reason": "all tasks merged: close the feature (area page with the five diagrams, Status: implemented)"}
    if not ready:
        pending = [t["id"] for t in s["tasks"] if not t["done"]]
        return {"skill": "sdd-task", "spec": i,
                "reason": f"no task ready: finish and merge the open PRs first ({', '.join(pending)} wait for them)"}
    group = parallel_group(ready)
    if len(group) > 1:
        return {"skill": "sdd-parallel", "spec": i, "tasks": [t["id"] for t in group],
                "reason": f"{', '.join(t['id'] for t in group)} are ready and touch different files: they can run in "
                          "parallel (one worktree and one PR each)"}
    return {"skill": "sdd-task", "spec": i, "tasks": [ready[0]["id"]], "reason": f"next task: {ready[0]['id']}"}


def choose_spec(active: list[dict], choice: str | None, branch: str) -> dict | None:
    if choice:
        k = spec_lint.id_key(choice[5:] if choice.startswith("SPEC-") else choice)
        for s in active:
            title = s["title_id"][5:] if s["title_id"] else ""
            if choice == s["id"] or (title and spec_lint.id_key(title) == k):
                return s
        return None
    in_branch = [s for s in active if s["id"].lower() in branch.lower()] if branch else []
    if in_branch:
        return max(in_branch, key=lambda s: len(s["id"]))
    return active[0] if len(active) == 1 else None


def analyze(root: Path, choice: str | None = None) -> dict:
    infra = {"AGENTS.md": (root / "AGENTS.md").is_file(), "scripts/verify.py": (root / "scripts" / "verify.py").is_file()}
    ref = main_ref(root)
    base = root / "docs" / "specs"
    branch = git(root, "branch", "--show-current")[1].strip()
    specs = [analyze_spec(root, ref, d, branch) for d in sorted(p for p in base.glob("*") if p.is_dir())] if base.is_dir() else []
    for s in specs:
        if s["status"] not in CLOSED:
            s["next"] = next_for_spec(s)
    bugs = []
    for p in sorted((root / "docs" / "bugs").glob("*.md")) if (root / "docs" / "bugs").is_dir() else []:
        state = spec_lint.status(_read(p))
        if state not in BUG_CLOSED:
            bugs.append({"file": p.name, "status": state or "n/a"})
    code = has_code(root)
    has_map = (root / "docs" / "system" / "map.md").is_file()
    return {"infra": infra, "main": ref, "existing_code": code, "map": has_map, "branch": branch, "specs": specs,
            "open_bugs": bugs, "next": next_step(infra, code, has_map, specs, choice, branch)}


def next_step(infra: dict, code: bool, has_map: bool, specs: list[dict], choice: str | None, branch: str) -> dict:
    if not all(infra.values()):
        return {"skill": "sdd-start", "reason": "project not initialized (AGENTS.md and/or scripts/verify.py missing)"}
    active = [s for s in specs if s["status"] not in CLOSED]
    if code and not has_map and not specs:
        return {"skill": "sdd-map", "reason": "existing code without docs/system/map.md (brownfield)"}
    if not active:
        return {"skill": "sdd-start", "reason": "no active spec: start from the next idea"}
    s = choose_spec(active, choice, branch)
    if s is None:
        listing = ", ".join(f"{a['id']} ({a['next']['skill']})" for a in active)
        return {"skill": "choose", "reason": (f"no active spec matches '{choice}'" if choice else
                                              f"{len(active)} active specs and none in the branch name: {listing}. "
                                              "Switch to the spec's branch or use --spec <id>")}
    return s["next"]


def show(st: dict) -> None:
    print("INFRASTRUCTURE: " + ", ".join(f"{k} {'ok' if v else 'missing'}" for k, v in st["infra"].items()))
    print(f"EXISTING CODE: {'yes' if st['existing_code'] else 'no'}; MAP: {'yes' if st['map'] else 'no'}; "
          f"MAIN: {st['main'] or 'none (working copy)'}" + (f"; BRANCH: {st['branch']}" if st["branch"] else ""))
    print("SPECS" + ("" if st["specs"] else ": none"))
    for s in st["specs"]:
        then = f" -> {s['next']['skill']}" if "next" in s else ""
        print(f"  {s['id']}: status={s['status']} plan={'yes' if s['plan'] else 'no'} "
              f"on main={'yes' if s['design_on_main'] else 'no'} tasks merged={s['tasks_done']}/{s['tasks_total']}{then}")
    if st["open_bugs"]:
        print("OPEN BUGS: " + ", ".join(f"{b['file']} ({b['status']})" for b in st["open_bugs"]) + "  -> sdd-bugfix")
    p = st["next"]
    print(f"NEXT: {p['skill']}{' on ' + p['spec'] if 'spec' in p else ''} - {p['reason']}")


def main(argv: list[str]) -> int:
    choice = argv[argv.index("--spec") + 1] if "--spec" in argv and argv.index("--spec") + 1 < len(argv) else None
    args = [a for a in argv if not a.startswith("--") and a != choice]
    st = analyze(Path(args[0]) if args else Path("."), choice)
    print(json.dumps(st, ensure_ascii=False, indent=2)) if "--json" in argv else show(st)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
