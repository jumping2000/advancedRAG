# SPEC-2026-11-20-fase-5-documentazione
"""The report of the final check of phases 1-4 and the documents of phase 5: AC-013 (REQ-012), AC-014 (REQ-013) and the
record of AC-015 (REQ-101).

`docs/audit/2026-11-20-controllo-finale.md` must have the six parts of REQ-013, as `##` titles in this order (`PARTS`):
the result of the full verification, `trace_check.py` on the four specs (and on phase 5), the documents against the
code, the sensitive areas of `AGENTS.md`, the known limits that the owner accepted, and the defects. The defects are a
table, one row per file of `docs/bugs/`, with a severity (`high`, `medium` or `low`) that is also the severity of the
bug file, and a relative link to that file; every file of `docs/bugs/` (a README or a dot file is not a bug) is in the
table, and every link of the report to `docs/bugs/` exists. A report without defects says so (`No defects`). Every
relative link of the report resolves.

AC-013: `python3 scripts/docs_check.py README.md README.it.md docs` (the standard library only) exits 0, so the links
and the Mermaid blocks of the READMEs, the guides and the report are valid.

AC-015 (the PRs of phase 5 change none of the files of REQ-101) is proved by `scripts/diff_scope.py` and by the diff
against `5b3767a`, which the report records in its first part with its command and its empty output: the diff cannot be
a standing test (the history is not there in a shallow clone, and the next phases change those files on purpose), so the
test here is that the evidence is recorded in the form the plan fixes and lists every path of REQ-101.

Every check is a function of a repository root, so that the tests below run it twice: on the real repository, and on a
small tree under `tmp_path` where the mistake is made on purpose (a part missing, a bug file that the report does not
link, a link to a bug that does not exist, a row without a valid severity, a severity that differs from the bug file,
...) and must be reported by name. A check that cannot fail would let any report pass.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.docs_support import FENCE, REPO_ROOT, lines_outside_code, read_text

REPORT = "docs/audit/2026-11-20-controllo-finale.md"
BUGS = "docs/bugs"
PARTS = (
    "1. Verification",
    "2. Traceability",
    "3. Documents against code",
    "4. Sensitive areas",
    "5. Known limits accepted",
    "6. Defects",
)
SEVERITIES = ("high", "medium", "low")
SPECS = (
    "2026-11-06-fase-1-fondazioni",
    "2026-11-07-fase-2-ingestion",
    "2026-11-17-fase-3-chat",
    "2026-11-20-fase-4-rerank-rewrite",
    "2026-11-20-fase-5-documentazione",
)
AREA_PAGES = ("fondazioni.md", "ingestion.md", "chat.md")
# The paths of REQ-101 (the plan: the diff of the phase against 5b3767a), each of which the recorded command must name
FORBIDDEN_PATHS = (
    "backend/app",
    "frontend/src",
    "backend/migrations",
    "nginx",
    "compose.yaml",
    "compose.tls.yaml",
    "compose.rerank-gpu.yaml",
    "compose.test.yaml",
    ".github/workflows",
    ".env.example",
)
# The bug files of the audit are dated with the day of the report; a bug file dated later belongs to a later piece of
# work (an `sdd-bugfix` of its own) and is not part of the report, which must not be edited for it
AUDIT_DATE = "2026-11-21"
BASE_COMMIT = "5b3767a"
NO_OUTPUT = "(no output)"
DOCS_CHECK = ["README.md", "README.it.md", "docs"]
URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
TWO_HASHES = re.compile(r"^ {0,3}##[ \t]+(.*\S)[ \t]*$")
SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
SEVERITY_LINE = re.compile(r"^- Severity / impact:\s*(\w+)", re.MULTILINE | re.IGNORECASE)
STATUS_LINE = re.compile(r"^- Status:\s*(\w+)", re.MULTILINE)


def label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


# The checks -----------------------------------------------------------------------------------------


def tagged_lines(text: str) -> list[tuple[str, bool]]:
    """Each line of a Markdown text with whether it is outside the fenced code blocks (the fences are inside)."""
    marker = ""
    tagged: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        fence = FENCE.match(line)
        if not marker and fence:
            marker = fence.group(1)
            tagged.append((line, False))
        elif (
            marker
            and fence
            and not fence.group(2)
            and fence.group(1)[0] == marker[0]
            and len(fence.group(1)) >= len(marker)
        ):
            marker = ""
            tagged.append((line, False))
        else:
            tagged.append((line, not marker))
    return tagged


def sections(text: str) -> dict[str, list[str]]:
    """The lines under each `##` title, by title, in order of appearance. A title inside a code block is not a title,
    and the lines of a code block belong to the section it is in."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line, outside in tagged_lines(text):
        match = TWO_HASHES.match(line) if outside else None
        if match:
            current = match.group(1)
            found.setdefault(current, [])
        elif current is not None:
            found[current].append(line)
    return found


def part_problems(root: Path) -> list[str]:
    """AC-014: the report exists, its `##` titles are the six parts in order, and none is empty."""
    path = root / REPORT
    if not path.is_file():
        return [f"{REPORT}: the report does not exist"]
    text = read_text(path)
    titles = [match.group(1) for line in lines_outside_code(text) if (match := TWO_HASHES.match(line))]
    problems = [f"{REPORT}: the part '## {part}' is missing" for part in PARTS if part not in titles]
    problems += [f"{REPORT}: '## {title}' is not one of the six parts" for title in titles if title not in PARTS]
    problems += [f"{REPORT}: the part '## {part}' appears twice" for part in PARTS if titles.count(part) > 1]
    if not problems and titles != list(PARTS):
        problems.append(f"{REPORT}: the six parts are not in the order of REQ-013: {', '.join(PARTS)}")
    body = sections(text)
    problems += [
        f"{REPORT}: the part '## {part}' is empty"
        for part in PARTS
        if part in body and not any(line.strip() for line in body[part])
    ]
    return problems


def part_text(root: Path, part: str) -> str:
    path = root / REPORT
    return "\n".join(sections(read_text(path)).get(part, [])) if path.is_file() else ""


def content_problems(root: Path) -> list[str]:
    """AC-014: what each part must show: the verification (`verify.py`), `trace_check.py` for each of the specs, the
    three area pages, every sensitive area of AGENTS.md (read from the file), and the three pages that hold the known
    limits."""
    problems: list[str] = []
    requirements = {
        PARTS[0]: ["verify.py"],
        PARTS[1]: ["trace_check.py", *SPECS],
        PARTS[2]: list(AREA_PAGES),
        PARTS[3]: sensitive_areas(root),
        PARTS[4]: list(AREA_PAGES),
    }
    for part, needed in requirements.items():
        text = part_text(root, part)
        problems += [f"{REPORT}: the part '## {part}' does not name {name}" for name in needed if name not in text]
    return problems


def sensitive_areas(root: Path) -> list[str]:
    """The paths of the `- Sensitive areas:` item of AGENTS.md (the backticked names, up to the next item)."""
    lines = read_text(root / "AGENTS.md").split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith("- Sensitive areas:"))
    item = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith(("- ", "#")) or not line.strip():
            break
        item.append(line)
    return re.findall(r"`([^`]+)`", " ".join(item))


DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})-")


def bug_files(root: Path) -> list[str]:
    """The names of the files of docs/bugs/ that the report must list: not a README, not a dot file, and not a file
    dated after the day of the report (a file with no date in its name is listed)."""
    folder = root / BUGS
    if not folder.is_dir():
        return []
    names = [
        path.name
        for path in folder.iterdir()
        if path.is_file() and not path.name.startswith(".") and not path.name.lower().startswith("readme")
    ]
    return sorted(name for name in names if not later_than_the_audit(name))


def later_than_the_audit(name: str) -> bool:
    dated = DATED.match(name)
    return dated is not None and dated.group(1) > AUDIT_DATE


def target_of(root: Path, href: str) -> Path | None:
    """The file that a relative link of the report points to (None for a URL or a pure anchor)."""
    if URL_SCHEME.match(href) or href.startswith("#"):
        return None
    return Path(os.path.normpath((root / REPORT).parent / unquote(re.split(r"[#?]", href)[0])))


def links_of(text: str) -> list[str]:
    return [href for line in lines_outside_code(text) for href in LINK.findall(line)]


def link_problems(root: Path) -> list[str]:
    """AC-014: every relative link or image of the report resolves to an existing file or folder."""
    path = root / REPORT
    if not path.is_file():
        return [f"{REPORT}: the report does not exist"]
    problems = []
    for href in links_of(read_text(path)):
        target = target_of(root, href)
        if target is not None and not target.exists():
            problems.append(f"{REPORT}: the link {href} does not exist")
    return problems


def table_rows(lines: list[str]) -> list[list[str]]:
    """The cells of the rows of the first table of the lines, header included (the separator row is left out)."""
    rows: list[list[str]] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            started = True
            if not SEPARATOR.match(stripped):
                rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
        elif started and stripped:
            break
    return rows


def defect_problems(root: Path) -> list[str]:
    """AC-014: the table of the defects has a row per file of docs/bugs/, each with a severity and a link to an existing
    file, the severity is the one of the bug file, every bug file is linked, every link to docs/bugs/ exists; a report
    with no bug files says `No defects`."""
    path = root / REPORT
    if not path.is_file():
        return [f"{REPORT}: the report does not exist"]
    problems: list[str] = []
    text = read_text(path)
    bugs = bug_files(root)
    folder = (root / BUGS).resolve()

    for href in links_of(text):
        target = target_of(root, href)
        if target is not None and target.resolve().parent == folder and not target.is_file():
            problems.append(f"{REPORT}: the link {href} to a bug file does not exist")

    rows = table_rows(sections(text).get(PARTS[5], []))
    header, body = (rows[0], rows[1:]) if rows else ([], [])
    if not bugs and not body:
        if "no defects" not in part_text(root, PARTS[5]).lower():
            problems.append(f"{REPORT}: no bug file and no row: the part '## {PARTS[5]}' must say 'No defects'")
        return problems
    severity_column = next((index for index, cell in enumerate(header) if cell.lower() == "severity"), None)
    if severity_column is None:
        return [*problems, f"{REPORT}: the table of the defects has no 'Severity' column"]

    linked_by_row: dict[str, int] = {}
    for number, row in enumerate(body, start=1):
        severity = row[severity_column].strip("` *").lower() if severity_column < len(row) else ""
        links = [
            target
            for href in LINK.findall(" ".join(row))
            if (target := target_of(root, href)) is not None and target.resolve().parent == folder
        ]
        if severity not in SEVERITIES:
            problems.append(f"{REPORT}: row {number} of the defects has the severity '{severity}', not {SEVERITIES}")
        if len(links) != 1:
            problems.append(f"{REPORT}: row {number} of the defects must link exactly one file of docs/bugs/")
            continue
        name = links[0].name
        linked_by_row[name] = linked_by_row.get(name, 0) + 1
        if not links[0].is_file():
            continue
        declared = SEVERITY_LINE.search(read_text(links[0]))
        if declared is None or declared.group(1).lower() != severity:
            problems.append(
                f"{BUGS}/{name}: its '- Severity / impact:' line must start with '{severity}', as in the report"
            )
    for name in bugs:
        count = linked_by_row.get(name, 0)
        if count == 0:
            problems.append(f"{BUGS}/{name}: the report has no row of the defects that links this file")
        elif count > 1:
            problems.append(f"{BUGS}/{name}: the report links this file in {count} rows, one is expected")
        if not STATUS_LINE.search(read_text(root / BUGS / name)):
            problems.append(f"{BUGS}/{name}: no '- Status:' line, the model of sdd-bugfix has one")
    return problems


def evidence_problems(root: Path) -> list[str]:
    """AC-015: the first part records the diff against 5b3767a of every path of REQ-101, as a command in a code block
    followed by its output, which is `(no output)`."""
    text = part_text(root, PARTS[0])
    lines = text.split("\n")
    prefix = f"git diff --stat {BASE_COMMIT}..HEAD --"
    commands = [index for index, line in enumerate(lines) if line.strip().removeprefix("$ ").startswith(prefix)]
    commands = [index for index in commands if all(path in lines[index] for path in FORBIDDEN_PATHS)]
    if not commands:
        return [
            f"{REPORT}: the part '## {PARTS[0]}' has no 'git diff --stat {BASE_COMMIT}..HEAD -- <paths>' naming "
            "all the paths of REQ-101"
        ]
    following = next((line.strip() for line in lines[commands[0] + 1 :] if line.strip()), "")
    if following != NO_OUTPUT:
        return [f"{REPORT}: the diff of the paths of REQ-101 must be followed by '{NO_OUTPUT}', it is {following!r}"]
    return []


def docs_check(root: Path) -> subprocess.CompletedProcess[str]:
    """`python3 scripts/docs_check.py README.md README.it.md docs` with the folder of the checked tree as the current
    one (the script is always the one of this repository)."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "docs_check.py"), *DOCS_CHECK],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def assert_no_problems(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# Trees under tmp_path ---------------------------------------------------------------------------------

AGENTS = "# AGENTS.md\n\n## Rules\n- Sensitive areas: `backend/app/auth/`, `compose.yaml`,\n  `nginx/`\n- Other: `x`\n"


def bug_text(severity: str = "low", status: str = "open") -> str:
    return (
        f"# BUG-001: a defect\n\n- Status: {status}\n- Severity / impact: {severity}, a note\n"
        "- Affected spec or area: x\n"
    )


def row(name: str, severity: str = "low") -> str:
    return f"| {severity} | what is wrong | `a.py:1` | evidence | [{name}](../bugs/{name}) |"


def report_text(rows: list[str] | None = None, parts: tuple[str, ...] = PARTS, extra: str = "") -> str:
    table = "\n".join(
        ["| Severity | What | Where | Evidence | Bug |", "|---|---|---|---|---|", *(rows or [])]
        if rows
        else ["No defects."]
    )
    bodies = {
        PARTS[0]: (
            "`python3 scripts/verify.py`: green.\n\n```\n"
            f"$ git diff --stat {BASE_COMMIT}..HEAD -- {' '.join(FORBIDDEN_PATHS)}\n{NO_OUTPUT}\n```\n"
        ),
        PARTS[1]: "`trace_check.py`: " + ", ".join(SPECS) + ".\n",
        PARTS[2]: "The pages " + ", ".join(AREA_PAGES) + ".\n",
        PARTS[3]: "Areas: backend/app/auth/, compose.yaml, nginx/.\n",
        PARTS[4]: "Limits of " + ", ".join(AREA_PAGES) + ".\n",
        PARTS[5]: table + "\n",
    }
    sections_text = "\n".join(f"## {part}\n\n{bodies[part]}" for part in parts)
    return f"# Final check\n\nIntro.\n\n{sections_text}{extra}"


def build_tree(root: Path, rows: list[str] | None = None, bugs: dict[str, str] | None = None) -> Path:
    """A small repository with a report and its bug files, all consistent. With no `bugs`, one bug and its row."""
    if bugs is None:
        bugs = {"2026-11-21-first.md": bug_text("low")}
        rows = [row("2026-11-21-first.md", "low")]
    write(root / "AGENTS.md", AGENTS)
    write(root / REPORT, report_text(rows))
    for name, text in bugs.items():
        write(root / BUGS / name, text)
    write(root / "README.md", "# R\n")
    write(root / "README.it.md", "# R\n")
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = read_text(path)
    assert old in text
    write(path, text.replace(old, new, 1))


# AC-014 on the repository -----------------------------------------------------------------------------


def test_AC_014_the_report_exists() -> None:
    assert (REPO_ROOT / REPORT).is_file()


def test_AC_014_the_report_has_the_six_parts_in_order() -> None:
    assert_no_problems(part_problems(REPO_ROOT))


def test_AC_014_every_part_shows_what_it_is_for() -> None:
    assert_no_problems(content_problems(REPO_ROOT))


def test_AC_014_every_file_of_docs_bugs_is_in_the_table_with_a_severity_and_a_link() -> None:
    assert_no_problems(defect_problems(REPO_ROOT))


def test_AC_014_the_defects_of_the_report_are_the_files_of_docs_bugs() -> None:
    assert (REPO_ROOT / REPORT).is_file()
    rows = table_rows(sections(read_text(REPO_ROOT / REPORT)).get(PARTS[5], []))[1:]
    assert len(rows) == len(bug_files(REPO_ROOT))
    assert bug_files(REPO_ROOT), "the final check found no defect in the bug folder: say so in the report instead"


def test_AC_014_every_relative_link_of_the_report_exists() -> None:
    assert_no_problems(link_problems(REPO_ROOT))


def test_AC_014_the_sensitive_areas_are_read_from_agents_md() -> None:
    assert set(sensitive_areas(REPO_ROOT)) >= {"backend/app/auth/", "compose.yaml", ".github/workflows/"}


# AC-015 (the record of the evidence) ------------------------------------------------------------------


def test_AC_015_the_report_records_the_empty_diff_of_every_path_of_req_101() -> None:
    assert_no_problems(evidence_problems(REPO_ROOT))


def test_AC_015_a_recorded_diff_with_output_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, NO_OUTPUT, " backend/app/main.py | 2 +-")

    assert any("must be followed by" in problem for problem in evidence_problems(root))


def test_AC_015_a_path_of_req_101_left_out_of_the_command_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, " nginx ", " ")

    assert any("has no 'git diff --stat" in problem for problem in evidence_problems(root))


def test_AC_015_a_report_without_the_diff_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, f"git diff --stat {BASE_COMMIT}..HEAD", f"git diff {BASE_COMMIT}..HEAD")

    assert evidence_problems(root)


def test_AC_015_the_consistent_tree_passes(tmp_path: Path) -> None:
    assert evidence_problems(build_tree(tmp_path)) == []


# AC-014 on trees under tmp_path ----------------------------------------------------------------------


def test_AC_014_a_consistent_tree_passes_every_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert part_problems(root) == []
    assert content_problems(root) == []
    assert defect_problems(root) == []
    assert link_problems(root) == []


def test_AC_014_a_missing_report_fails_and_names_the_file(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / REPORT).unlink()

    problems = part_problems(root)

    assert len(problems) == 1
    assert REPORT in problems[0]


@pytest.mark.parametrize("part", PARTS)
def test_AC_014_a_missing_part_fails_and_names_it(tmp_path: Path, part: str) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md")], parts=tuple(p for p in PARTS if p != part)))

    problems = part_problems(root)

    assert len(problems) == 1
    assert f"'## {part}' is missing" in problems[0]


def test_AC_014_a_part_with_another_title_fails_and_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, "## 2. Traceability", "## 2. Tracing")

    problems = part_problems(root)

    assert any("'## 2. Traceability' is missing" in problem for problem in problems)
    assert any("'## 2. Tracing' is not one of the six parts" in problem for problem in problems)


def test_AC_014_the_parts_in_another_order_fail(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md")], parts=(PARTS[1], PARTS[0], *PARTS[2:])))

    problems = part_problems(root)

    assert len(problems) == 1
    assert "order" in problems[0]


def test_AC_014_a_part_that_appears_twice_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, "## 3. Documents against code", f"## {PARTS[1]}\n\ntext\n\n## 3. Documents against code")

    assert any("appears twice" in problem for problem in part_problems(root))


def test_AC_014_an_empty_part_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, "Limits of fondazioni.md, ingestion.md, chat.md.\n", "")

    problems = part_problems(root)

    assert len(problems) == 1
    assert "is empty" in problems[0]


def test_AC_014_a_title_inside_a_code_block_is_not_a_part(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md")], extra="\n```\n## 7. Not a part\n```\n"))

    assert part_problems(root) == []


@pytest.mark.parametrize("spec", SPECS)
def test_AC_014_a_spec_that_the_traceability_does_not_name_fails(tmp_path: Path, spec: str) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, spec, "a-spec")

    problems = content_problems(root)

    assert any(spec in problem and PARTS[1] in problem for problem in problems)


def test_AC_014_a_sensitive_area_that_the_report_does_not_name_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, "nginx/.", "something.")

    problems = content_problems(root)

    assert len(problems) == 1
    assert "nginx/" in problems[0]
    assert PARTS[3] in problems[0]


def test_AC_014_a_report_that_does_not_name_the_verification_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, "scripts/verify.py", "scripts/other.py")

    assert any("verify.py" in problem for problem in content_problems(root))


def test_AC_014_a_bug_file_that_the_report_does_not_link_fails_and_names_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / BUGS / "2026-11-21-second.md", bug_text("medium"))

    problems = defect_problems(root)

    assert len(problems) == 1
    assert "docs/bugs/2026-11-21-second.md" in problems[0]
    assert "no row" in problems[0]


def test_AC_014_a_link_to_a_bug_that_does_not_exist_fails_and_names_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / BUGS / "2026-11-21-first.md").unlink()
    write(root / BUGS / "2026-11-21-other.md", bug_text("low"))

    problems = defect_problems(root)

    assert any("../bugs/2026-11-21-first.md" in problem and "does not exist" in problem for problem in problems)
    assert any("docs/bugs/2026-11-21-other.md" in problem and "no row" in problem for problem in problems)


def test_AC_014_a_link_to_a_missing_bug_outside_the_table_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md")], extra="\nSee [x](../bugs/missing.md).\n"))

    problems = defect_problems(root)

    assert len(problems) == 1
    assert "../bugs/missing.md" in problems[0]


@pytest.mark.parametrize("severity", ["", "critical", "info", "urgent"])
def test_AC_014_a_row_without_a_valid_severity_fails(tmp_path: Path, severity: str) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md", severity)]))

    problems = defect_problems(root)

    assert any(f"row 1 of the defects has the severity '{severity}'" in problem for problem in problems)


@pytest.mark.parametrize("severity", SEVERITIES)
def test_AC_014_each_severity_of_the_three_is_accepted(tmp_path: Path, severity: str) -> None:
    root = build_tree(tmp_path, [row("2026-11-21-first.md", severity)], {"2026-11-21-first.md": bug_text(severity)})

    assert defect_problems(root) == []


def test_AC_014_a_severity_that_differs_from_the_bug_file_fails_and_names_the_file(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / BUGS / "2026-11-21-first.md", bug_text("high"))

    problems = defect_problems(root)

    assert len(problems) == 1
    assert "docs/bugs/2026-11-21-first.md" in problems[0]
    assert "must start with 'low'" in problems[0]


def test_AC_014_a_row_without_a_link_to_a_bug_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text(["| low | what | `a.py:1` | evidence | none |"]))

    problems = defect_problems(root)

    assert any("must link exactly one file" in problem for problem in problems)
    assert any("no row" in problem for problem in problems)


def test_AC_014_a_bug_file_linked_in_two_rows_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md"), row("2026-11-21-first.md")]))

    problems = defect_problems(root)

    assert len(problems) == 1
    assert "2 rows" in problems[0]


def test_AC_014_a_table_without_a_severity_column_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / REPORT, "| Severity |", "| Gravity |")

    assert any("no 'Severity' column" in problem for problem in defect_problems(root))


def test_AC_014_a_bug_file_without_a_status_line_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / BUGS / "2026-11-21-first.md", "# BUG-001: x\n\n- Severity / impact: low\n")

    problems = defect_problems(root)

    assert len(problems) == 1
    assert "Status" in problems[0]


def test_AC_014_a_readme_and_a_dot_file_in_docs_bugs_are_not_bugs(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / BUGS / "README.md", "# Bugs\n")
    write(root / BUGS / ".gitkeep", "")

    assert bug_files(root) == ["2026-11-21-first.md"]
    assert defect_problems(root) == []


def test_AC_014_a_bug_file_dated_after_the_report_is_not_part_of_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / BUGS / "2026-11-22-later.md", bug_text("medium"))

    assert bug_files(root) == ["2026-11-21-first.md"]
    assert defect_problems(root) == []


@pytest.mark.parametrize("name", ["2026-11-21-same-day.md", "2026-11-20-earlier.md", "undated.md"])
def test_AC_014_a_bug_file_of_the_day_of_the_report_or_before_or_undated_must_be_listed(
    tmp_path: Path, name: str
) -> None:
    root = build_tree(tmp_path)
    write(root / BUGS / name, bug_text("low"))

    problems = defect_problems(root)

    assert len(problems) == 1
    assert f"docs/bugs/{name}" in problems[0]


def test_AC_014_no_bug_file_and_no_row_needs_the_statement_that_there_is_no_defect(tmp_path: Path) -> None:
    root = build_tree(tmp_path, [], {})

    assert defect_problems(root) == []

    edit(root / REPORT, "No defects.", "Nothing to say.")

    problems = defect_problems(root)
    assert len(problems) == 1
    assert "No defects" in problems[0]


def test_AC_014_a_row_for_a_bug_file_that_is_not_there_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path, [row("2026-11-21-first.md")], {})

    problems = defect_problems(root)

    assert any("../bugs/2026-11-21-first.md" in problem and "does not exist" in problem for problem in problems)


def test_AC_014_a_relative_link_that_does_not_resolve_fails_and_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(
        root / REPORT, report_text([row("2026-11-21-first.md")], extra="\n[gone](../system/gone.md) and ![i](a.png)\n")
    )

    problems = link_problems(root)

    assert len(problems) == 2
    assert "../system/gone.md" in problems[0]
    assert "a.png" in problems[1]


def test_AC_014_urls_anchors_and_links_in_code_blocks_are_not_checked(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    extra = "\n[w](https://example.org/x) [a](#top)\n\n```\n[gone](../system/gone.md)\n```\n"
    write(root / REPORT, report_text([row("2026-11-21-first.md")], extra=extra))

    assert link_problems(root) == []


# AC-013: docs_check.py ---------------------------------------------------------------------------------


def test_AC_013_docs_check_passes_on_the_readmes_the_guides_and_the_report() -> None:
    assert (REPO_ROOT / REPORT).is_file()

    result = docs_check(REPO_ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_AC_013_docs_check_uses_only_the_standard_library() -> None:
    script = read_text(REPO_ROOT / "scripts" / "docs_check.py")
    imported = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w]*)", script, flags=re.MULTILINE))

    assert imported <= set(sys.stdlib_module_names) | {"spec_lint"}


def test_AC_013_docs_check_passes_on_a_consistent_tree(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    result = docs_check(root)

    assert result.returncode == 0, result.stdout + result.stderr


def test_AC_013_docs_check_fails_on_a_broken_link_of_the_report(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md")], extra="\n[gone](../system/gone.md)\n"))

    result = docs_check(root)

    assert result.returncode == 1
    assert "gone.md" in result.stdout + result.stderr


def test_AC_013_docs_check_fails_on_an_invalid_mermaid_block_of_the_report(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / REPORT, report_text([row("2026-11-21-first.md")], extra="\n```mermaid\nnot a diagram\n```\n"))

    result = docs_check(root)

    assert result.returncode == 1
    assert "mermaid" in (result.stdout + result.stderr).lower()
