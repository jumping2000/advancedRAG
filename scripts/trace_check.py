#!/usr/bin/env python3
# sdd-playbook: 0.30.0
"""AC -> test traceability: every acceptance criterion of the spec must appear in at least one test.

Usage:
  python scripts/trace_check.py docs/specs/2026-10-24-export-csv/spec.md
  python scripts/trace_check.py docs/specs/2026-10-24-export-csv/spec.md --tests tests integration
  python scripts/trace_check.py docs/specs/2026-10-24-export-csv/spec.md --warn-only
  python scripts/trace_check.py docs/specs/2026-10-24-export-csv/spec.md --junit reports/junit.xml
  python scripts/trace_check.py docs/specs/2026-10-24-export-csv/spec.md --task T-02 --junit reports/junit.xml

ACs restart from AC-001 in every spec, so a citation counts for one specific spec:
  1. qualified: SPEC-2026-10-24-export-csv/AC-001 (in function names SPEC_2026_10_24_export_csv_AC_001; for old IDs
     also SPEC-002-AC-001, SPEC002_AC001) -> counts for that spec;
  2. short (AC-001, AC_001, AC001, Go's TestAC001) in a file that names a single spec (e.g.
     "# SPEC-2026-10-24-export-csv" at the top) -> counts for that one. It is the recommended way with date IDs;
  3. short in a file that names no spec -> counts only if the project has a single spec in docs/specs/; otherwise it is
     ambiguous, does not count and is reported.
Citations on lines with TODO, FIXME or XXX do not count: a reminder is not a test.
--junit FILE|DIR ...: JUnit XML report of the executed tests (a folder means all its *.xml). Every AC needs at least one
test case executed and passed, and none failed: citing the AC is no longer enough. A case counts for an AC if it cites
it in qualified form (name or classname), or in short form when it belongs to a test file attributed to the spec or the
project has a single spec. The file of a case is the one whose path best matches the file attribute or the classname
(tests.a.test_api -> tests/a/test_api.py; com.acme.ExportTest -> .../com/acme/ExportTest.java); on a tie between files
of different specs the case does not count. Skipped cases do not count.
The spec ID is read from the title "# SPEC-<id>: ..." (date + lower-case slug, or a number).
--task T-nn: only the ACs the task covers ("covers:" in plan.md next to the spec): the check of a task's PR.
Without --task every AC of the spec, and every REQ needs a tested AC: the check of the feature.
Test folders searched by default: tests, test, __tests__, spec, src/test; a project adds paths or globs with the line
"- Test files: a, b" of AGENTS.md (e.g. `e2e/**/*.robot`, `src/**/*_test.rs`).
Exit code: 0 all ACs cited (and, with --junit, verified); 1 ACs without tests; 2 wrong usage or unreadable report.
Standard library only, cross-platform.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

sys.dont_write_bytecode = True  # no __pycache__ in the project: it would count as a changed file
sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_scope  # noqa: E402
import spec_lint  # noqa: E402

EXTENSIONS = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".java", ".kt", ".scala", ".cs", ".fs",
              ".go", ".rb", ".php", ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".swift", ".dart", ".ex", ".exs",
              ".sh", ".bash", ".bats", ".ps1", ".psm1", ".feature", ".robot", ".hcl", ".yaml", ".yml"}
EXTRA_TESTS: list[str] = []  # "- Test files: a, b" of AGENTS.md (paths or globs), read by main()
FOLDERS = ("tests", "test", "__tests__", "spec", "src/test")
SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", "target", "bin", "obj"}
AC_REF = re.compile(r"(?:(?<=Test)|(?<![A-Za-z0-9]))(?:AC[-_]?|ac_)0*(\d+)(?!\d)")  # TestAC001 (Go), ac_001 (Rust)
# Spec ID: date + lower-case slug (also with "_" instead of "-", for function names) or number.
ID_REF = r"(\d{4}[-_]\d{2}[-_]\d{2}[-_][a-z0-9]+(?:[-_][a-z0-9]+)*|\d+)"
SPEC_REF = re.compile(rf"(?<![A-Za-z0-9])SPEC[-_]?{ID_REF}(?!\d)")
QUALIFIED = re.compile(rf"(?<![A-Za-z0-9])SPEC[-_]?{ID_REF}[-_/:. ]{{0,3}}AC[-_]?0*(\d+)(?!\d)", re.I)  # also snake_case
REMINDER = re.compile(r"\b(TODO|FIXME|XXX)\b")


class Result(NamedTuple):
    acs: dict[str, list[str]]
    covered: dict[str, list[str]]
    missing: list[str]
    orphans: list[int]
    req_covered: dict[str, bool]
    ambiguous: list[str]
    ignored: list[str]


def spec_key(spec_text: str) -> str | None:
    """Key of the spec ID read from the title (normalized form, see spec_lint.id_key)."""
    sid = spec_lint.spec_id(spec_text)
    return spec_lint.id_key(sid) if sid else None


def specs_in_project() -> int:
    base = Path("docs/specs")
    return sum(1 for _ in base.glob("*/spec.md")) if base.is_dir() else 0


def test_files(folders: list[Path]):
    seen: set[Path] = set()
    for base in folders:
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in EXTENSIONS and not SKIP & set(f.parts):
                seen.add(f)
                yield f
    if EXTRA_TESTS:
        for f in sorted(Path(".").rglob("*")):
            rel = f.as_posix()
            if (f.is_file() and f not in seen and not SKIP & set(f.parts)
                    and any(diff_scope.matches(rel, g) for g in EXTRA_TESTS)):
                yield f


def short_in_file(text: str, spec: str | None, single_spec: bool) -> str:
    """How short citations count in a file: "valid", "ambiguous" or "foreign"."""
    declared = {spec_lint.id_key(n) for n in SPEC_REF.findall(text)}
    if spec is not None and declared == {spec}:
        return "valid"
    if not declared:
        return "valid" if single_spec else "ambiguous"
    if spec in declared:
        return "ambiguous"  # the file names several specs: a short citation does not say which one
    return "foreign"  # the file belongs to other specs


def collect(folders: list[Path], spec: str | None, single_spec: bool):
    """(AC number -> positions for the given spec, ambiguous citations, citations ignored as reminders)."""
    found: dict[int, list[str]] = {}
    ambiguous: list[str] = []
    ignored: list[str] = []
    for f in test_files(folders):
        text = f.read_text(encoding="utf-8", errors="ignore")
        short = short_in_file(text, spec, single_spec)
        for n, line in enumerate(text.splitlines(), 1):
            pos = f"{f.as_posix()}:{n}"
            if not (QUALIFIED.search(line) or AC_REF.search(line)):
                continue
            if REMINDER.search(line):
                ignored.append(pos)
                continue
            for m in QUALIFIED.finditer(line):
                if spec_lint.id_key(m.group(1)) == spec:
                    found.setdefault(int(m.group(2)), []).append(pos)
            for m in AC_REF.finditer(QUALIFIED.sub(" ", line)):
                if short == "valid":
                    found.setdefault(int(m.group(1)), []).append(pos)
                elif short == "ambiguous":
                    ambiguous.append(pos)
    return found, ambiguous, ignored


def analyze(spec_text: str, folders: list[Path], single_spec: bool | None = None) -> Result:
    _, acs = spec_lint.parse_spec(spec_text)
    single = specs_in_project() <= 1 if single_spec is None else single_spec
    found, ambiguous, ignored = collect(folders, spec_key(spec_text), single)
    covered = {ac: found.get(int(ac[3:]), []) for ac in acs}
    missing = [ac for ac, pos in covered.items() if not pos]
    numbers = {int(a[3:]) for a in acs}
    orphans = sorted(n for n in found if n not in numbers)
    req_covered: dict[str, bool] = {}
    for ac, refs in acs.items():
        for r in refs:
            req_covered[r] = req_covered.get(r, False) or bool(covered[ac])
    return Result(acs, covered, missing, orphans, req_covered, ambiguous, ignored)


def report_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(p.rglob("*.xml")) if p.is_dir() else [p]
    return out


def junit_cases(reports: list[Path]) -> list[tuple[str, str, str, str]]:
    """(name, classname, file, outcome) of every testcase; outcome: passed, failed or skipped. ValueError if unreadable."""
    cases = []
    for f in reports:
        try:
            root = ET.parse(f).getroot()
        except (ET.ParseError, OSError) as e:
            raise ValueError(f"unreadable JUnit report: {f.as_posix()} ({e})") from e
        for c in root.iter("testcase"):
            children = {x.tag for x in c}
            outcome = "failed" if children & {"failure", "error"} else "skipped" if "skipped" in children else "passed"
            cases.append((c.get("name", ""), c.get("classname", ""), c.get("file", ""), outcome))
    return cases


def parts(path: str) -> list[str]:
    return [c for c in re.split(r"[./:\\]+", path) if c]


def match_length(test_file: str, file: str, classname: str) -> int:
    """How many path parts of the test file match the case's file attribute or classname."""
    path = parts(test_file.rsplit(".", 1)[0])
    if file:
        f = parts(file.replace("\\", "/").rsplit(".", 1)[0])
        n = next((k for k in range(min(len(f), len(path)), 0, -1) if f[-k:] == path[-k:]), 0)
        if n:
            return n
    c = parts(classname)
    # the classname starts with a tail of the path (tests.a.test_api[.Class], com.acme.ExportTest)
    return next((len(path) - i for i in range(len(path)) if c[:len(path) - i] == path[i:]), 0)


def case_in_valid_file(file: str, classname: str, valid: dict[str, bool]) -> bool:
    """The case belongs to a file valid for the spec: the best match, with no tie against files of other specs."""
    scores = {f: match_length(f, file, classname) for f in valid}
    best = max(scores.values(), default=0)
    if not best:
        return False
    return all(valid[f] for f, n in scores.items() if n == best)


def executions(spec_text: str, folders: list[Path], reports: list[Path],
               single_spec: bool | None = None) -> dict[str, dict[str, list[str]]]:
    """AC -> {"passed": [...], "failed": [...], "skipped": [...]} from the report cases attributed to this spec."""
    _, acs = spec_lint.parse_spec(spec_text)
    spec = spec_key(spec_text)
    single = specs_in_project() <= 1 if single_spec is None else single_spec
    valid = {f.as_posix(): short_in_file(f.read_text(encoding="utf-8", errors="ignore"), spec, single) == "valid"
             for f in test_files(folders)}
    out: dict[str, dict[str, list[str]]] = {ac: {"passed": [], "failed": [], "skipped": []} for ac in acs}
    for name, classname, file, outcome in junit_cases(reports):
        text = f"{classname} {name}"
        numbers = {int(m.group(2)) for m in QUALIFIED.finditer(text) if spec_lint.id_key(m.group(1)) == spec}
        if single or case_in_valid_file(file, classname, valid):
            numbers |= {int(m.group(1)) for m in AC_REF.finditer(QUALIFIED.sub(" ", text))}
        for ac in acs:
            if int(ac[3:]) in numbers:
                out[ac][outcome].append(f"{classname}::{name}" if classname else name)
    return out


def values(opts: list[str], name: str) -> list[str]:
    """Arguments following the option up to the next one."""
    out: list[str] = []
    if name in opts:
        for o in opts[opts.index(name) + 1:]:
            if o.startswith("--"):
                break
            out.append(o)
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("--"):
        print(__doc__)
        return 2
    spec_path, opts = argv[0], argv[1:]
    warn_only = "--warn-only" in opts
    dirs = values(opts, "--tests")
    junit = values(opts, "--junit")
    if "--junit" in opts and not junit:
        print("[ERROR] --junit without a report: give the file or folder of the JUnit report")
        return 2
    folders = [Path(d) for d in dirs] if dirs else [Path(c) for c in FOLDERS if Path(c).is_dir()]
    folders = [c for c in folders if c.is_dir()]
    agents = Path("AGENTS.md")
    EXTRA_TESTS[:] = diff_scope.agents_entries(agents.read_text(encoding="utf-8"), "Test files") if agents.is_file() else []
    if not folders and not EXTRA_TESTS:
        print("[ERROR] no test folder found: use --tests <folder> or '- Test files:' in AGENTS.md")
        return 2
    text = Path(spec_path).read_text(encoding="utf-8")
    tid = values(opts, "--task")[0] if values(opts, "--task") else None
    only = None
    if tid:
        plan = Path(spec_path).parent / "plan.md"
        found = spec_lint.task(plan.read_text(encoding="utf-8"), tid) if plan.is_file() else None
        if found is None:
            print(f"[ERROR] {tid} not found in {plan.as_posix()}")
            return 2
        only = set(found["covers"])
    r = analyze(text, folders)
    if only is not None:
        r = r._replace(acs={a: v for a, v in r.acs.items() if a in only}, missing=[a for a in r.missing if a in only],
                       req_covered={})
    if spec_key(text) is None:
        print("[WARN] the spec has no '# SPEC-<id>: ...' title: only short citations count, and only with one spec")
    for ac in r.acs:
        pos = r.covered[ac]
        print(f"{ac}: " + (f"{len(pos)} references (e.g. {pos[0]})" if pos else "NO TEST"))
    for req, ok in r.req_covered.items():
        if not ok:
            print(f"[ERROR] {req}: none of its ACs has tests")
    if r.orphans:
        print("[WARN] ACs attributed to this spec in the tests but missing from the spec: "
              + ", ".join(f"AC-{n:03d}" for n in r.orphans))
    if r.ambiguous:
        print(f"[WARN] {len(r.ambiguous)} AC citations without a spec in files that do not declare a single spec (e.g. "
              f"{', '.join(r.ambiguous[:3])}): they do not count. Use SPEC-<id>/AC-nnn or declare the spec in the file")
    if r.ignored:
        print(f"[WARN] {len(r.ignored)} citations on TODO/FIXME/XXX lines ignored (e.g. {', '.join(r.ignored[:3])})")
    unverified: list[str] = []
    if junit:
        try:
            executed = executions(text, folders, report_files(junit))
        except ValueError as err:
            print(f"[ERROR] {err}")
            return 2
        for ac, outcomes in executed.items():
            if only is not None and ac not in only:
                continue
            if outcomes["failed"]:
                print(f"[ERROR] {ac}: failed tests in the report: {', '.join(outcomes['failed'][:3])}")
            elif not outcomes["passed"]:
                print(f"[ERROR] {ac}: no executed and passing test in the report"
                      + (f" ({len(outcomes['skipped'])} skipped)" if outcomes["skipped"] else ""))
            else:
                continue
            unverified.append(ac)
    ko = sorted(set(r.missing) | set(unverified))
    if not ko:
        print("TRACE: OK" + (" (verified against the report of executed tests)" if junit else ""))
    else:
        print(f"TRACE: {len(r.missing)} ACs without tests" + (f": {', '.join(r.missing)}" if r.missing else "")
              + (f"; {len(unverified)} ACs not verified by the report: {', '.join(unverified)}" if junit else ""))
    return 0 if (not ko or warn_only) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # UTF-8 even with a cp1252 console (Windows)
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
