#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""Deterministic checks on the playbook's spec and plan.

Usage:
  python scripts/spec_lint.py docs/specs/<id>/spec.md
  python scripts/spec_lint.py docs/specs/<id>/spec.md --plan docs/specs/<id>/plan.md

Spec: title "# SPEC-yyyy-mm-dd-slug: ..." with the ID equal to the folder name (the old "# SPEC-NNN: ..." is still
valid); "- Status:" draft | approved | implemented | superseded; sections Problem and goal, Scope, Requirements,
Acceptance criteria, Open questions; every REQ has an AC, every AC cites existing REQs; a filled-in non-scope (error)
and anti-goals (warning), a "## Current behaviour and impact" section (warning). Open questions: "- <question> — blocking: yes|no"; an approved or implemented spec may keep
only non-blocking ones. Warnings: vague words in REQ and AC lines ("fast", "appropriate", "should", "etc", ...),
assumptions still marked [ASSUMPTION] in an approved spec. The approval is the merge of the design PR: no field to sign.
Plan: a "## Flowchart" with a ```flowchart Mermaid block; "## Tasks" with one line per vertical task,
"- [ ] T-nn <goal> — covers: AC-nnn — files: `path` — depends: T-nn | none — verify: `<command>`": existing ACs,
at least one file, existing dependencies without cycles, every spec AC covered, no placeholders; more than 8 tasks is a
warning (split the feature); "## Review focus" filled in or "- none".
Area pages and map (docs_check.py): "## Diagrams" with the five diagrams of DIAGRAMS, each drawn or "- n/a: <reason>".
Exit code 0 = no errors (warnings do not block). Standard library only, cross-platform.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

STATES = ("draft", "approved", "implemented", "superseded")
SECTIONS = ("Problem and goal", "Scope", "Requirements", "Acceptance criteria", "Open questions")
# Spec ID: date + slug (2026-10-24-export-csv, equal to the folder name) or the old number (001).
ID_DATE = r"\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*"
TITLE = re.compile(rf"^#\s+SPEC-({ID_DATE}|\d+)\s*:", re.M)
REQ_DEF = re.compile(r"^\s*[-*]\s+(REQ-\d+)\b")
AC_DEF = re.compile(r"^\s*[-*]\s+(AC-\d+)\s*\[([^\]]*)\]")
PLACEHOLDER = re.compile(r"<[^<>\n]{1,80}>")
NONE_ITEM = re.compile(r"^[-*]\s*(none|n/a)\b", re.I)
TASK = re.compile(r"^\s*-\s+\[([ xX])\]\s+(T-\d+)\b(.*)$", re.M)
MAX_TASKS = 8  # beyond this, split the feature into two specs (warning)
WEAK_WORDS = re.compile(r"\b(fast|quick(?:ly)?|appropriate(?:ly)?|adequate(?:ly)?|user-friendly|easy|easily|intuitive|"
                        r"flexible|efficient(?:ly)?|robust|reasonable|as needed|if possible|as appropriate|etc|and/or|"
                        r"should|some|several|various|normally|usually|approximately|TBD)\b", re.I)
# Diagrams of the documentation (playbook §6): subsection heading -> accepted first keywords. The flowchart always applies.
DIAGRAMS = {"Flowchart": ("flowchart", "graph"), "Sequence diagram": ("sequenceDiagram",),
            "State diagram": ("stateDiagram-v2", "stateDiagram"), "Class diagram": ("classDiagram",),
            "ER diagram": ("erDiagram",)}
ALWAYS_DRAWN = ("Flowchart",)
NA_ITEM = re.compile(r"^[-*]\s*n/a\s*:\s*(\S.*)$", re.I)


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def sections(text: str) -> dict[str, list[str]]:
    """Map '## ...' heading -> lines of the section."""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def section(text: str, prefix: str) -> list[str] | None:
    return next((lines for title, lines in sections(text).items() if title.lower().startswith(prefix.lower())), None)


def field(text: str, name: str) -> str | None:
    m = re.search(rf"^\s*[-*]\s+{re.escape(name)}\s*:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def clean(value: str | None) -> str:
    """Field value without HTML comments."""
    return re.sub(r"<!--.*?-->", "", value or "").strip()


def status(text: str) -> str:
    return clean(field(text, "Status")).split("|")[0].strip()


def spec_id(text: str) -> str | None:
    """ID from the title "# SPEC-<id>: ...": date + slug or number."""
    m = TITLE.search(text)
    return m.group(1) if m else None


def id_key(value: str) -> str:
    """Comparable form of an ID: numbers without leading zeros, slug in lower case with "-"."""
    v = value.replace("_", "-").lower()
    return str(int(v)) if v.isdigit() else v


def id_problems(text: str, folder: str | None) -> list[str]:
    sid = spec_id(text)
    if sid is None:
        return ["Title without ID: expected '# SPEC-yyyy-mm-dd-slug: title' (or the old '# SPEC-NNN: title')"]
    if not sid.isdigit():
        try:
            date.fromisoformat(sid[:10])
        except ValueError:
            return [f"ID {sid}: the leading date is not valid (expected yyyy-mm-dd)"]
        if folder is not None and folder != sid:
            return [f"Title ID ({sid}) differs from the folder name ({folder}): they must match"]
    elif folder is not None and (m := re.match(r"^(\d+)-", folder)) and int(m.group(1)) != int(sid):
        return [f"Title ID (SPEC-{sid}) differs from the folder number ({folder})"]
    return []


def parse_spec(text: str):
    reqs = [m.group(1) for line in text.splitlines() if (m := REQ_DEF.match(line))]
    acs: dict[str, list[str]] = {}
    for line in text.splitlines():
        if (m := AC_DEF.match(line)):
            acs[m.group(1)] = re.findall(r"REQ-\d+", m.group(2))
    return reqs, acs


def open_questions(text: str) -> list[tuple[str, bool | None]]:
    """(line, blocking: True/False/None if not stated) of the open questions."""
    out = []
    for line in sections(text).get("Open questions", []):
        line = line.strip()
        if line.startswith(("-", "*")) and not NONE_ITEM.match(line):
            m = re.search(r"blocking:\s*(yes|no)\b", line, re.I)
            out.append((line, (m.group(1).lower() == "yes") if m else None))
    return out


def language_warnings(text: str) -> list[str]:
    """Vague words in the REQ and AC lines."""
    warn = []
    for line in text.splitlines():
        m = REQ_DEF.match(line) or AC_DEF.match(line)
        if m and not PLACEHOLDER.search(line):
            vague = sorted({w.lower() for w in WEAK_WORDS.findall(line)})
            if vague:
                warn.append(f"{m.group(1)}: vague words ({', '.join(vague)}): replace them with something verifiable")
    return warn


def lint_spec(text: str, folder: str | None = None) -> tuple[list[str], list[str]]:
    """folder: name of the spec folder, which for date IDs must match the ID."""
    err: list[str] = []
    warn: list[str] = []
    state = status(text)
    if state not in STATES:
        err.append(f"Status missing or invalid (expected one of: {', '.join(STATES)}); found '{state}'")
    err += id_problems(text, folder)
    sec = sections(text)
    for s in SECTIONS:
        if s not in sec:
            err.append(f"Missing section: '## {s}'")
    reqs, acs = parse_spec(text)
    if len(reqs) != len(set(reqs)):
        err.append("Duplicate REQ IDs")
    if not reqs:
        err.append("No requirement (REQ-nnn) found")
    for req in reqs:
        if not any(req in refs for refs in acs.values()):
            err.append(f"{req} has no acceptance criterion")
    ac_lines = [m.group(1) for line in text.splitlines() if (m := AC_DEF.match(line))]
    if len(ac_lines) != len(set(ac_lines)):
        err.append("Duplicate AC IDs")
    for ac, refs in acs.items():
        if not refs:
            err.append(f"{ac} cites no REQ")
        err += [f"{ac} cites {ref}, which does not exist" for ref in refs if ref not in reqs]
    scope = "\n".join(sec.get("Scope", []))
    m = re.search(r"Non-scope[^:\n]*:\**[ \t]*(.*)", scope, re.I)
    nonscope = m.group(1).strip() if m else ""
    if not m or not nonscope or PLACEHOLDER.fullmatch(nonscope):
        err.append("Non-scope missing or not filled in")
    m = re.search(r"Anti-goals[^:\n]*:\**[ \t]*(.*)", scope, re.I)
    if not m or not m.group(1).strip() or PLACEHOLDER.fullmatch(m.group(1).strip()):
        warn.append("Anti-goals missing or not filled in (what would make the feature a failure even if it works)")
    warn += language_warnings(text)
    if "Current behaviour and impact" not in sec:
        warn.append("Section '## Current behaviour and impact' missing (what exists today and must not change; "
                    "'- none (new area)' for new code)")
    if state in ("approved", "implemented"):
        questions = open_questions(text)
        blocking = [q for q in questions if q[1] is not False]  # a placeholder also counts as an open question
        if blocking:
            err.append(f"Spec {state} with {len(blocking)} blocking open questions (or without 'blocking: yes|no')")
        if any(q[1] is False for q in questions):
            warn.append("Non-blocking open questions remain: answer them in a revision or move them to a new spec")
        if "[ASSUMPTION]" in text:
            warn.append("Assumptions marked [ASSUMPTION] remain: validate or rephrase them")
    return err, warn


def task_field(rest: str, name: str) -> str:
    m = re.search(rf"\b{name}:\s*(.*?)\s*(?=—\s*\w+:|$)", rest)
    return m.group(1).strip() if m else ""


def parse_tasks(plan: str) -> list[dict]:
    """Tasks of the "## Tasks" section: id, done, goal, covers, files, depends, verify, line."""
    out = []
    for m in TASK.finditer("\n".join(section(plan, "Tasks") or [])):
        rest = m.group(3)
        files = task_field(rest, "files")
        out.append({
            "id": m.group(2), "done": m.group(1) != " ", "line": m.group(0).strip(),
            "goal": rest.split("—")[0].strip(),
            "covers": re.findall(r"AC-\d+", task_field(rest, "covers")),
            "files": [p for b, w in re.findall(r"`([^`]+)`|([^\s,`]+)", files) if (p := (b or w).strip())],
            "depends": re.findall(r"T-\d+", task_field(rest, "depends")),
            "verify": (re.search(r"verify:\s*`([^`]+)`", rest) or [None, ""])[1],
        })
    return out


def task(plan: str, tid: str) -> dict | None:
    return next((t for t in parse_tasks(plan) if t["id"] == tid), None)


def cycle(tasks: list[dict]) -> list[str]:
    """IDs on a dependency cycle (empty if none)."""
    deps = {t["id"]: t["depends"] for t in tasks}
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str) -> list[str]:
        if state.get(n) == 1:
            return path[path.index(n):]
        if state.get(n) == 2 or n not in deps:
            return []
        state[n] = 1
        path.append(n)
        for d in deps[n]:
            if (found := visit(d)):
                return found
        path.pop()
        state[n] = 2
        return []

    for n in deps:
        if (found := visit(n)):
            return found
    return []


def mermaid_blocks(lines: list[str]) -> list[str]:
    """Bodies of the ```mermaid blocks among the lines (a closing fence is at least as long as the opening one)."""
    out, fence, buf = [], None, []
    for line in lines:
        m = re.match(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$", line)
        if fence is None and m:
            fence, buf = (m.group(1), m.group(2)), []
        elif fence is not None and m and not m.group(2) and m.group(1)[0] == fence[0][0] \
                and len(m.group(1)) >= len(fence[0]):
            if fence[1] == "mermaid":
                out.append("\n".join(buf))
            fence = None
        elif fence is not None:
            buf.append(line)
    return out


def mermaid_type(code: str) -> str:
    """First keyword of a Mermaid diagram, skipping %% comments and the --- front matter."""
    lines = [line.strip() for line in code.splitlines()]
    if lines and lines[0] == "---":
        lines = lines[lines.index("---", 1) + 1:] if "---" in lines[1:] else []
    first = next((line for line in lines if line and not line.startswith("%%")), "")
    return first.split()[0] if first else ""


def diagram_problems(text: str) -> list[str]:
    """Problems of the "## Diagrams" section: every DIAGRAMS subsection with a Mermaid block of its type or n/a."""
    lines = next((lines for title, lines in sections(text).items() if title.lower() == "diagrams"), None)
    if lines is None:
        return [f"Section '## Diagrams' missing ({', '.join(DIAGRAMS)}: a Mermaid block each, or '- n/a: <reason>')"]
    subs: dict[str, list[str]] = {}
    current = None
    for line in lines:
        m = re.match(r"^###\s+(.*?)\s*$", line)
        if m:
            current = m.group(1)
            subs[current] = []
        elif current is not None:
            subs[current].append(line)
    out = []
    for title, keywords in DIAGRAMS.items():
        body = next((v for k, v in subs.items() if k.lower() == title.lower()), None)
        if body is None:
            out.append(f"Diagrams: subsection '### {title}' missing")
            continue
        blocks = mermaid_blocks(body)
        na = [m.group(1) for line in body if (m := NA_ITEM.match(line.strip())) and not PLACEHOLDER.search(line)]
        if blocks:
            wrong = [b for b in blocks if mermaid_type(b) not in keywords]
            if wrong:
                out.append(f"Diagrams: '{title}' needs a Mermaid block starting with {' or '.join(keywords)}, "
                           f"found '{mermaid_type(wrong[0]) or 'empty'}'")
            elif any(len([x for x in b.splitlines() if x.strip()]) < 2 for b in blocks):
                out.append(f"Diagrams: '{title}' has an empty Mermaid block")
        elif na and title in ALWAYS_DRAWN:
            out.append(f"Diagrams: '{title}' always applies: draw it")
        elif not na:
            out.append(f"Diagrams: '{title}' not filled in (a Mermaid block, or '- n/a: <reason>')")
    return out


def flowchart_problems(plan: str) -> list[str]:
    lines = section(plan, "Flowchart")
    if lines is None:
        return ["Section '## Flowchart' missing from the plan (the main flow of the feature, in Mermaid)"]
    blocks = [b for b in mermaid_blocks(lines) if mermaid_type(b) in DIAGRAMS["Flowchart"]]
    if not blocks or all(len([x for x in b.splitlines() if x.strip()]) < 2 for b in blocks):
        return ["Flowchart not filled in: a ```mermaid block starting with flowchart"]
    return []


def lint_plan(spec: str, plan: str) -> tuple[list[str], list[str]]:
    err: list[str] = []
    warn: list[str] = []
    _, acs = parse_spec(spec)
    if section(plan, "Approach") is None:
        warn.append("Section '## Approach' missing (chosen approach and a discarded alternative)")
    err += flowchart_problems(plan)
    if section(plan, "Tasks") is None:
        return err + ["Section '## Tasks' missing from the plan"], warn
    tasks = parse_tasks(plan)
    if not tasks:
        return err + ["No task (- [ ] T-nn ...) in '## Tasks'"], warn
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        err.append("Duplicate task IDs")
    for t in tasks:
        tid = t["id"]
        if (m := PLACEHOLDER.search(t["line"])):
            err.append(f"{tid}: placeholder not filled in {m.group(0)}")
            continue
        if not t["covers"]:
            err.append(f"{tid}: missing 'covers: AC-nnn'")
        err += [f"{tid}: covers {ac}, which is not in the spec" for ac in t["covers"] if ac not in acs]
        if not t["files"]:
            err.append(f"{tid}: missing 'files:' (the main files it creates or changes)")
        if not t["verify"]:
            err.append(f"{tid}: missing 'verify: `<command>`'")
        if not re.search(r"\bdepends:", t["line"]):
            err.append(f"{tid}: missing 'depends: T-nn | none'")
        err += [f"{tid}: depends on {d}, which is not a task" for d in t["depends"] if d not in ids or d == tid]
    if (loop := cycle(tasks)):
        err.append(f"Dependency cycle between tasks: {' -> '.join(loop)}")
    covered = {ac for t in tasks for ac in t["covers"]}
    err += [f"{ac} is not covered by any task" for ac in acs if ac not in covered]
    if len(tasks) > MAX_TASKS:
        warn.append(f"{len(tasks)} tasks (usually at most {MAX_TASKS}): split the feature into two specs, or merge "
                    "tasks into vertical slices")
    focus = section(plan, "Review focus")
    if focus is None:
        err.append("Section '## Review focus' missing (input classes no AC exercises, or '- none')")
    elif not [x for x in focus if x.strip().startswith(("-", "*"))] or any(
            PLACEHOLDER.search(x) for x in focus if x.strip().startswith(("-", "*"))):
        err.append("Review focus not filled in: list the uncovered input classes with their task, or write '- none'")
    return err, warn


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    spec_path = args[0]
    spec = read(spec_path)
    err, warn = lint_spec(spec, Path(spec_path).resolve().parent.name)
    if "--plan" in argv and argv.index("--plan") + 1 < len(argv):
        e, w = lint_plan(spec, read(argv[argv.index("--plan") + 1]))
        err += e
        warn += w
    for w in warn:
        print(f"[WARN] {w}")
    for e in err:
        print(f"[ERROR] {e}")
    print("LINT: " + ("OK" if not err else f"{len(err)} errors"))
    return 1 if err else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
