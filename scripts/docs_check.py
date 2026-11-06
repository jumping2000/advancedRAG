#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""Documentation checks: valid relative links, Mermaid diagrams and runnable Python examples.

Usage:
  python scripts/docs_check.py [paths...] [--run-examples]

Default paths: README.md and the docs/ folder. Checks the relative links and images of Markdown files (excludes code
blocks, external URLs and pure anchors). Every ```mermaid block must start with a known diagram type and have a body
(the syntax is checked by the renderer: preview the diagram). Every page of docs/system/ (area pages and map) needs
the "## Diagrams" section: flowchart, sequence, state, class and ER diagram, each a Mermaid block or "- n/a: <reason>"
(playbook §6). With --run-examples it runs the ```python blocks whose first non-empty line is
`# docs-check: run` (60 s timeout, current folder = project root): they must exit with code 0. Other blocks are not
run: examples in other languages must be tried by hand by the agent.
Exit code: 0 ok; 1 broken links or failed examples; 2 wrong usage. Standard library only, cross-platform.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the project: it would count as a changed file
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_lint  # noqa: E402

# First keywords of the Mermaid diagram types (https://mermaid.js.org/intro/).
MERMAID_TYPES = {"flowchart", "graph", "sequenceDiagram", "stateDiagram", "stateDiagram-v2", "classDiagram",
                 "erDiagram", "journey", "gantt", "pie", "quadrantChart", "requirementDiagram", "gitGraph", "mindmap",
                 "timeline", "C4Context", "C4Container", "C4Component", "C4Dynamic", "C4Deployment", "sankey-beta",
                 "xychart-beta", "block-beta", "packet-beta", "kanban", "architecture-beta", "radar-beta"}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+-]*)\s*$")


def md_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out += sorted(path.rglob("*.md"))
        elif path.is_file():
            out.append(path)
    return out


def split_blocks(text: str):
    """Return (text lines outside the blocks, blocks[(language, code)])."""
    outside: list[tuple[int, str]] = []
    blocks: list[tuple[str, str]] = []
    in_block, marker, language, buf = False, "", "", []
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if not in_block and m:
            in_block, marker, language, buf = True, m.group(2), m.group(3), []
        elif in_block and m and not m.group(3) and m.group(2)[0] == marker[0] and len(m.group(2)) >= len(marker):
            blocks.append((language, "\n".join(buf)))  # closing fence: same character, at least as long
            in_block = False
        elif in_block:
            buf.append(line)
        else:
            outside.append((n, line))
    return outside, blocks


def broken_links(f: Path) -> list[str]:
    errors = []
    outside, _ = split_blocks(f.read_text(encoding="utf-8", errors="ignore"))
    for n, line in outside:
        line = re.sub(r"`[^`]*`", "", line)  # ignore inline code
        for m in LINK.finditer(line):
            dest = m.group(1)
            if re.match(r"^(https?:|mailto:|#|/)", dest):
                continue
            target = (f.parent / dest.split("#")[0]).resolve()
            if not target.exists():
                errors.append(f"{f.as_posix()}:{n}: broken link -> {dest}")
    return errors


def diagram_errors(f: Path) -> list[str]:
    text = f.read_text(encoding="utf-8", errors="ignore")
    errors = []
    for i, (language, code) in enumerate(split_blocks(text)[1], 1):
        if language != "mermaid":
            continue
        kind = spec_lint.mermaid_type(code)
        if kind not in MERMAID_TYPES:
            errors.append(f"{f.as_posix()}: Mermaid block #{i}: unknown diagram type '{kind or 'empty'}'")
        elif len([x for x in code.splitlines() if x.strip()]) < 2:
            errors.append(f"{f.as_posix()}: Mermaid block #{i}: empty diagram")
    if f.parent.name == "system" and f.parent.parent.name == "docs":
        errors += [f"{f.as_posix()}: {p}" for p in spec_lint.diagram_problems(text)]
    return errors


def run_examples(f: Path) -> tuple[list[str], int]:
    errors, executed = [], 0
    _, blocks = split_blocks(f.read_text(encoding="utf-8", errors="ignore"))
    for i, (language, code) in enumerate(blocks, 1):
        first = next((line.strip() for line in code.splitlines() if line.strip()), "")
        if language != "python" or first != "# docs-check: run":
            continue
        executed += 1
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as t:
            t.write(code)
            name = t.name
        try:
            r = subprocess.run([sys.executable, name], capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                errors.append(f"{f.as_posix()}: example #{i} failed (exit {r.returncode}): "
                              f"{(r.stderr or r.stdout).strip()[-200:]}")
        except subprocess.TimeoutExpired:
            errors.append(f"{f.as_posix()}: example #{i} exceeded the 60 s timeout")
        finally:
            Path(name).unlink(missing_ok=True)
    return errors, executed


def main(argv: list[str]) -> int:
    run = "--run-examples" in argv
    paths = [a for a in argv if not a.startswith("--")] or ["README.md", "docs"]
    files = md_files(paths)
    if not files:
        print("[ERROR] no Markdown file found")
        return 2
    errors, executed = [], 0
    for f in files:
        errors += broken_links(f) + diagram_errors(f)
        if run:
            e, n = run_examples(f)
            errors += e
            executed += n
    for e in errors:
        print(f"[ERROR] {e}")
    print(f"DOCS: {len(files)} files" + (f", {executed} examples run" if run else "") + " - "
          + ("OK" if not errors else f"{len(errors)} errors"))
    return 1 if errors else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
