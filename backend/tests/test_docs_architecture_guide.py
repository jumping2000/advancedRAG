# SPEC-2026-11-20-fase-5-documentazione
"""The architecture and development guide, in both languages, has what a developer needs: AC-008 (REQ-007).

The guide must have a Mermaid `flowchart` of the components that names every service of `compose.yaml` (read from the
file, so a new service that the diagram forgets fails here) and the external services; the two paths (a document, a
question), the isolation per KB and the SDD flow, each with the technical names that make it checkable; every sensitive
area of `AGENTS.md`; the verify, test and screenshots commands exactly as `AGENTS.md` and the plan write them; and
links to `docs/system/`, `docs/specs/`, `docs/adr/`, `AGENTS.md` and `docs/constitution.md` that resolve from the guide.
The validity of the Mermaid syntax is the job of `scripts/docs_check.py`; the twins existing, their first line and their
images are checked by test_docs_alignment.py (the count of the titles is repeated here for this guide only).

Every check is a function of a repository root, so that the tests below run it twice: on the real repository, and on a
small tree under `tmp_path` where the mistake is made on purpose (no flowchart, a component missing, a broken link, a
command missing, ...) and must be reported by name. A check that cannot fail would let any guide pass. The commands of
the guide were run by hand and are listed, with their result, in the pull request (AC-017, REQ-103).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.docs_support import FENCE, LANGS, REPO_ROOT, guide_path, headings, lines_outside_code, read_text

ARCHITECTURE = "architecture.md"
URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
COMPOSE_SERVICES = re.compile(r"^services:\s*(#.*)?$")
COMPOSE_KEY = re.compile(r"^(\s+)[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"']?\s*:(\s|$)")

# The services that Compose does not start and the guide draws next to the ones it does (names of the docs and of the
# installation guide, in the language of each guide)
EXTERNAL = {
    "en": ("Ollama", "Docling", "ParadeDB", "generation provider", "rewriting service"),
    "it": ("Ollama", "Docling", "ParadeDB", "provider di generazione", "servizio di riscrittura"),
}
# What a developer reads in the guide, the area pages and the code under these names: any language, as written
DOCUMENT_PATH = ("QUEUED", "PROCESSING", "READY", "UPLOAD_DIR", "process_document_job", "BM25", "HNSW")
QUESTION_PATH = ("rewrite_enabled", "rerank_enabled", "BM25", "RRF", "SSE", "text/event-stream", "message_sources")
ISOLATION = ("KnowledgeBaseRepository", "list_for", "get_for", "kb_members", "profile_id")
SDD_FLOW = (
    "sdd-start",
    "sdd-plan",
    "sdd-task",
    "sdd-parallel",
    "sdd-reviewer",
    "Test-Lock",
    "spec_lint.py",
    "trace_check.py",
    "diff_scope.py",
    "docs_check.py",
)
# Commands as AGENTS.md writes them (the screenshots one as the plan fixes it): shown in a code block, to copy
COMMANDS = (
    "uv sync --project backend",
    "npm ci --prefix frontend",
    "docker compose -f compose.test.yaml up -d --wait",
    "python3 scripts/verify.py",
    "python3 scripts/verify.py --fast",
    "uv run --project backend pytest <path>::<name>",
    "npm --prefix frontend run test -- <file>",
    "uv run --directory backend pytest -m e2e",
    "uv run --frozen --directory backend playwright install --with-deps chromium",
    "docker compose up --build",
    "uv run --directory backend pytest -m screenshots",
)
SCREENSHOTS_COMMAND = "uv run --directory backend pytest -m screenshots"
# Said in words and named as written: the browser of the e2e tests, and what the screenshots command uses and writes
LITERALS = ("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "18980", "docs/guide/images/")
# The documents the guide links, each as a file or a folder of the repository
LINK_TARGETS = ("docs/system", "docs/specs", "docs/adr", "AGENTS.md", "docs/constitution.md")


# The checks -----------------------------------------------------------------------------------------


def label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def guide_file(root: Path, lang: str) -> Path:
    return guide_path(lang, ARCHITECTURE, root)


def code_blocks(text: str) -> list[tuple[str, str]]:
    """The fenced code blocks of a Markdown text as (language, code); the fence rules are those of `docs_support`."""
    blocks: list[tuple[str, str]] = []
    marker, language, buffer = "", "", []
    for line in text.split("\n"):
        fence = FENCE.match(line)
        if not marker and fence:
            marker, language, buffer = fence.group(1), fence.group(2), []
        elif (
            marker
            and fence
            and not fence.group(2)
            and fence.group(1)[0] == marker[0]
            and len(fence.group(1)) >= len(marker)
        ):
            blocks.append((language, "\n".join(buffer)))
            marker = ""
        elif marker:
            buffer.append(line)
    return blocks


def diagram_type(code: str) -> str:
    """The first word of a Mermaid diagram, after the front matter and the `%%` comments."""
    lines = [line.strip() for line in code.split("\n")]
    if lines and lines[0] == "---" and "---" in lines[1:]:
        lines = lines[lines.index("---", 1) + 1 :]
    first = next((line for line in lines if line and not line.startswith("%%")), "")
    return first.split()[0] if first else ""


def flowcharts(text: str) -> list[str]:
    return [code for language, code in code_blocks(text) if language == "mermaid" and diagram_type(code) == "flowchart"]


def compose_services(root: Path = REPO_ROOT) -> list[str]:
    """The names of the `services:` section of `compose.yaml`, in order, read line by line (the standard library only):
    the block starts at a `services:` line in column 0 and ends at the next line that starts in column 0; comments and
    blank lines are skipped, and only the keys at the first indentation of the block are names."""
    names: list[str] = []
    inside = False
    indent = -1
    for line in read_text(root / "compose.yaml").split("\n"):
        if not inside:
            inside = bool(COMPOSE_SERVICES.match(line))
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            break
        key = COMPOSE_KEY.match(line)
        if key and indent in (-1, len(key.group(1))):
            indent = len(key.group(1))
            names.append(key.group(2))
    return names


def sensitive_paths(root: Path = REPO_ROOT) -> list[str]:
    """The paths of the line `- Sensitive areas:` of AGENTS.md (it goes on in the indented lines under it)."""
    collected: list[str] = []
    inside = False
    for line in read_text(root / "AGENTS.md").split("\n"):
        if not inside:
            if line.startswith("- Sensitive areas:"):
                inside = True
                collected.append(line.split(":", 1)[1])
        elif line.startswith("  "):
            collected.append(line)
        else:
            break
    return re.findall(r"`([^`]+)`", " ".join(collected))


def guide_text(root: Path, lang: str) -> str:
    path = guide_file(root, lang)
    assert path.is_file(), f"{label(path, root)} does not exist"
    return read_text(path)


def flowchart_problems(text: str) -> list[str]:
    """AC-008: at least one Mermaid block that starts with `flowchart`."""
    if flowcharts(text):
        return []
    return ["the guide has no ```mermaid block that starts with flowchart"]


def component_problems(text: str, services: list[str], external: tuple[str, ...]) -> list[str]:
    """AC-008: the first flowchart (the diagram of the components) names every service of compose.yaml, as a word, and
    every external service."""
    diagrams = flowcharts(text)
    if not diagrams:
        return ["there is no flowchart to name the components"]
    diagram = diagrams[0]
    problems = [
        f"the flowchart does not name the service {name} of compose.yaml"
        for name in services
        if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", diagram)
    ]
    problems += [f"the flowchart does not name the external service {name}" for name in external if name not in diagram]
    return problems


def link_problems(path: Path, root: Path, targets: tuple[str, ...] = LINK_TARGETS) -> list[str]:
    """AC-008: every relative link of the guide (outside code) resolves to an existing file or folder, and each of the
    `targets` is reached by at least one link that resolves to it or inside it."""
    resolved: list[Path] = []
    problems: list[str] = []
    for line in lines_outside_code(read_text(path)):
        for destination in LINK.findall(re.sub(r"`[^`]*`", "", line)):
            if URL_SCHEME.match(destination) or destination.startswith(("#", "/")):
                continue
            target = Path(os.path.normpath(path.parent / unquote(re.split(r"[#?]", destination)[0])))
            if target.exists():
                resolved.append(target)
            else:
                problems.append(f"{label(path, root)}: broken link -> {destination}")
    for name in targets:
        wanted = Path(os.path.normpath(root / name))
        if not any(found == wanted or wanted in found.parents for found in resolved):
            problems.append(f"{label(path, root)}: no working link to {name}")
    return problems


def command_problems(text: str, commands: tuple[str, ...] = COMMANDS) -> list[str]:
    """AC-008: each command is a line of a fenced code block, as written (a `# comment` may follow it): a command that
    is the start of another one, such as `python3 scripts/verify.py` of the `--fast` line, does not count for it."""
    lines = [line.strip() for _, block in code_blocks(text) for line in block.split("\n")]
    return [
        f"the command {command} is not in a code block"
        for command in commands
        if not any(re.fullmatch(rf"{re.escape(command)}(\s+#.*)?", line) for line in lines)
    ]


def literal_problems(text: str, literals: tuple[str, ...], what: str) -> list[str]:
    return [f"{what}: {literal} is missing" for literal in literals if literal not in text]


def sensitive_problems(text: str, paths: list[str]) -> list[str]:
    """AC-008: every path of the sensitive areas of AGENTS.md is named, in backticks."""
    return [f"the sensitive area `{path}` of AGENTS.md is not named" for path in paths if f"`{path}`" not in text]


def level_counts(text: str) -> tuple[int, int]:
    counts = Counter(heading.split(" ", 1)[0] for heading in headings(text))
    return counts["##"], counts["###"]


def title_problems(root: Path) -> list[str]:
    first, second = (level_counts(guide_text(root, lang)) for lang in LANGS)
    if first == second:
        return []
    return [
        f"{label(guide_file(root, LANGS[0]), root)} and {label(guide_file(root, LANGS[1]), root)}: different titles, "
        f"{first[0]} '##' and {first[1]} '###' against {second[0]} '##' and {second[1]} '###'"
    ]


def all_problems(root: Path, lang: str) -> list[str]:
    """Every check on the guide of `lang`; empty when the guide has what AC-008 asks for."""
    path = guide_file(root, lang)
    if not path.is_file():
        return [f"{label(path, root)} does not exist"]
    text = read_text(path)
    return [
        *flowchart_problems(text),
        *component_problems(text, compose_services(root), EXTERNAL[lang]),
        *link_problems(path, root),
        *command_problems(text),
        *literal_problems(text, LITERALS, "screenshots and Chromium"),
        *literal_problems(text, DOCUMENT_PATH, "path of a document"),
        *literal_problems(text, QUESTION_PATH, "path of a question"),
        *literal_problems(text, ISOLATION, "isolation per KB"),
        *literal_problems(text, SDD_FLOW, "SDD flow"),
        *sensitive_problems(text, sensitive_paths(root)),
    ]


def assert_no_problems(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# Trees under tmp_path ---------------------------------------------------------------------------------

SERVICES = ["db", "redis", "backend", "worker", "nginx", "reranker"]
SENSITIVE = ["backend/app/auth/", "backend/migrations/", "compose.yaml", ".github/workflows/"]
COMPOSE_FILE = (
    "# a comment\nname: demo\nservices:\n  # a comment inside the block\n"
    + "".join(f"  {name}:\n    image: x\n    environment:\n      A: b\n\n" for name in SERVICES)
    + "volumes:\n  pgdata:\n"
)
AGENTS_FILE = (
    "# AGENTS.md\n\n## Rules\n- Do not modify: nothing\n- Sensitive areas: `backend/app/auth/`, `backend/migrations/`\n"
    "  (a comment), `compose.yaml`,\n  `.github/workflows/`\n- Dependencies: `docs/adr/` is not an area\n"
)


def complete_guide(lang: str) -> str:
    twin = next(other for other in LANGS if other != lang)
    names = " and ".join([*SERVICES, *EXTERNAL[lang]])
    diagram = "```mermaid\nflowchart LR\n    A[" + names.replace(" and ", "] --> B[") + "]\n```"
    commands = "```bash\n" + "\n".join(COMMANDS) + "\n```"
    words = " ".join([*LITERALS, *DOCUMENT_PATH, *QUESTION_PATH, *ISOLATION, *SDD_FLOW])
    areas = " ".join(f"`{path}`" for path in SENSITIVE)
    links = (
        "[system](../../system/chat.md) [specs](../../specs/) [adr](../../adr/ADR-001.md) "
        "[agents](../../../AGENTS.md) [constitution](../../constitution.md)"
    )
    return (
        f"# Guide {lang}\n\n[{twin}](../{twin}/{ARCHITECTURE})\n\n## Components\n\n{diagram}\n\n"
        f"### Words\n\n{words}\n\n"
        f"## Commands\n\n{commands}\n\n## Areas\n\n{areas}\n\n## References\n\n{links}\n"
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def build_tree(root: Path) -> Path:
    """A small repository with the sources the checks read and the guide in both languages, all complete."""
    write(root / "compose.yaml", COMPOSE_FILE)
    write(root / "AGENTS.md", AGENTS_FILE)
    write(root / "docs" / "constitution.md", "# Constitution\n")
    write(root / "docs" / "system" / "chat.md", "# Chat\n")
    write(root / "docs" / "specs" / "a-spec" / "spec.md", "# Spec\n")
    write(root / "docs" / "adr" / "ADR-001.md", "# ADR\n")
    for lang in LANGS:
        write(guide_file(root, lang), complete_guide(lang))
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = read_text(path)
    assert old in text
    write(path, text.replace(old, new))


# AC-008: the guide in the repository ----------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_exists_in_both_languages(lang: str) -> None:
    path = guide_file(REPO_ROOT, lang)

    assert path.is_file(), f"{label(path, REPO_ROOT)} does not exist"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_has_a_mermaid_flowchart_of_the_components(lang: str) -> None:
    assert_no_problems(flowchart_problems(guide_text(REPO_ROOT, lang)))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_flowchart_names_every_service_of_compose_and_the_external_services(lang: str) -> None:
    services = compose_services()
    assert services[:2] == ["db", "redis"], "compose.yaml services were not read"
    assert {"backend", "worker", "nginx", "reranker"} <= set(services)

    assert_no_problems(component_problems(guide_text(REPO_ROOT, lang), services, EXTERNAL[lang]))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_links_the_system_pages_the_specs_the_adrs_agents_and_the_constitution(lang: str) -> None:
    guide_text(REPO_ROOT, lang)

    assert_no_problems(link_problems(guide_file(REPO_ROOT, lang), REPO_ROOT))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_shows_the_setup_verify_test_and_screenshots_commands(lang: str) -> None:
    text = guide_text(REPO_ROOT, lang)

    assert_no_problems(command_problems(text))
    assert_no_problems(literal_problems(text, LITERALS, "screenshots and Chromium"))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_describes_the_path_of_a_document_and_the_path_of_a_question(lang: str) -> None:
    text = guide_text(REPO_ROOT, lang)

    assert_no_problems(literal_problems(text, DOCUMENT_PATH, "path of a document"))
    assert_no_problems(literal_problems(text, QUESTION_PATH, "path of a question"))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_explains_the_isolation_per_kb_and_names_the_sensitive_areas(lang: str) -> None:
    text = guide_text(REPO_ROOT, lang)
    paths = sensitive_paths()
    assert len(paths) >= 5, "the sensitive areas of AGENTS.md were not read"

    assert_no_problems(literal_problems(text, ISOLATION, "isolation per KB"))
    assert_no_problems(sensitive_problems(text, paths))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_the_guide_explains_the_sdd_flow(lang: str) -> None:
    assert_no_problems(literal_problems(guide_text(REPO_ROOT, lang), SDD_FLOW, "SDD flow"))


def test_AC_008_the_two_languages_have_the_same_number_of_titles() -> None:
    assert_no_problems(title_problems(REPO_ROOT))


# AC-008: the checks can fail --------------------------------------------------------------------------


def test_AC_008_a_complete_tree_passes_every_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert compose_services(root) == SERVICES
    assert sensitive_paths(root) == SENSITIVE
    for lang in LANGS:
        assert all_problems(root, lang) == []
    assert title_problems(root) == []


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_a_guide_that_does_not_exist_fails_and_names_the_file(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    guide_file(root, lang).unlink()

    problems = all_problems(root, lang)

    assert len(problems) == 1
    assert f"docs/guide/{lang}/{ARCHITECTURE}" in problems[0]
    with pytest.raises(AssertionError, match=ARCHITECTURE):
        guide_text(root, lang)


def test_AC_008_a_guide_without_a_flowchart_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(guide_file(root, "en"), "flowchart LR", "sequenceDiagram")

    problems = all_problems(root, "en")

    assert any("no ```mermaid block that starts with flowchart" in problem for problem in problems)
    assert any("no flowchart to name the components" in problem for problem in problems)
    assert all_problems(root, "it") == []


def test_AC_008_a_flowchart_in_a_block_that_is_not_mermaid_or_in_a_comment_is_not_a_flowchart(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(guide_file(root, "en"), "```mermaid\nflowchart LR", "```text\nflowchart LR")

    assert flowchart_problems(read_text(guide_file(root, "en"))) != []
    assert flowcharts("```mermaid\n%% a comment\nflowchart TD\n    A --> B\n```\n") != []
    assert flowcharts("```mermaid\n---\ntitle: t\n---\nflowchart TD\n    A --> B\n```\n") != []
    assert flowcharts("```mermaid\ngraph TD\n    A --> B\n```\n") == []


@pytest.mark.parametrize("name", SERVICES)
def test_AC_008_a_service_of_compose_missing_from_the_flowchart_fails_and_is_named(tmp_path: Path, name: str) -> None:
    root = build_tree(tmp_path)
    path = guide_file(root, "it")
    edit(path, f"[{name}]", "[x]")

    problems = [problem for problem in all_problems(root, "it") if "flowchart" in problem]

    assert problems == [f"the flowchart does not name the service {name} of compose.yaml"]
    assert all_problems(root, "en") == []


def test_AC_008_a_service_added_to_compose_fails_in_both_guides(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(
        root / "compose.yaml",
        read_text(root / "compose.yaml").replace("volumes:", "  metrics:\n    image: y\nvolumes:"),
    )

    for lang in LANGS:
        assert all_problems(root, lang) == ["the flowchart does not name the service metrics of compose.yaml"]


def test_AC_008_a_service_name_inside_another_word_does_not_count_as_the_service(tmp_path: Path) -> None:
    diagram = "```mermaid\nflowchart LR\n    A[ParadeDB] --> B[workers] --> C[my-redis]\n```\n"

    problems = component_problems(diagram, ["db", "redis", "worker"], ())

    assert problems == [
        "the flowchart does not name the service db of compose.yaml",
        "the flowchart does not name the service redis of compose.yaml",
        "the flowchart does not name the service worker of compose.yaml",
    ]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_an_external_service_missing_from_the_flowchart_fails_and_is_named(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    text = read_text(guide_file(root, lang))
    diagram = flowcharts(text)[0]
    write(guide_file(root, lang), text.replace(diagram, diagram.replace("Docling", "D")))

    problems = [problem for problem in all_problems(root, lang) if "flowchart" in problem]

    assert problems == ["the flowchart does not name the external service Docling"]


def test_AC_008_a_name_that_is_only_outside_the_flowchart_does_not_count(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    text = read_text(guide_file(root, "en"))
    diagram = flowcharts(text)[0]
    write(guide_file(root, "en"), text.replace(diagram, diagram.replace("Ollama", "O")) + "\nOllama is external.\n")

    assert component_problems(read_text(guide_file(root, "en")), SERVICES, EXTERNAL["en"]) == [
        "the flowchart does not name the external service Ollama"
    ]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_008_a_broken_link_fails_and_names_the_guide_and_the_link(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    edit(guide_file(root, lang), "(../../system/chat.md)", "(../../system/missing.md)")

    problems = link_problems(guide_file(root, lang), root)

    assert f"docs/guide/{lang}/{ARCHITECTURE}: broken link -> ../../system/missing.md" in problems
    assert f"docs/guide/{lang}/{ARCHITECTURE}: no working link to docs/system" in problems


@pytest.mark.parametrize("target", LINK_TARGETS)
def test_AC_008_a_missing_reference_fails_and_names_it(tmp_path: Path, target: str) -> None:
    root = build_tree(tmp_path)
    links = {
        "docs/system": "[system](../../system/chat.md) ",
        "docs/specs": "[specs](../../specs/) ",
        "docs/adr": "[adr](../../adr/ADR-001.md) ",
        "AGENTS.md": "[agents](../../../AGENTS.md) ",
        "docs/constitution.md": " [constitution](../../constitution.md)",
    }
    edit(guide_file(root, "en"), links[target], "")

    assert link_problems(guide_file(root, "en"), root) == [f"docs/guide/en/{ARCHITECTURE}: no working link to {target}"]
    assert link_problems(guide_file(root, "it"), root) == []


def test_AC_008_a_link_to_a_folder_or_inside_it_counts_and_a_link_in_code_or_a_url_does_not(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    path = guide_file(root, "en")
    edit(path, "[system](../../system/chat.md)", "[system](../../system/)")
    edit(path, "[specs](../../specs/)", "[specs](../../specs/a-spec/spec.md#top)")
    assert link_problems(path, root) == []

    edit(path, "[adr](../../adr/ADR-001.md)", "`[adr](../../adr/ADR-001.md)` [adr](https://example.com/docs/adr/)")
    edit(path, "## Commands", "```text\n[adr](../../adr/ADR-001.md)\n```\n\n## Commands")
    assert link_problems(path, root) == [f"docs/guide/en/{ARCHITECTURE}: no working link to docs/adr"]


@pytest.mark.parametrize("command", COMMANDS)
def test_AC_008_a_command_missing_from_the_guide_fails_and_is_named(tmp_path: Path, command: str) -> None:
    root = build_tree(tmp_path)
    path = guide_file(root, "it")
    # the line of the command goes away, every other one stays (a command can be the start of another one)
    write(path, read_text(path).replace(f"\n{command}\n", "\n"))

    assert command_problems(read_text(path)) == [f"the command {command} is not in a code block"]
    assert command_problems(read_text(guide_file(root, "en"))) == []


def test_AC_008_a_command_written_in_the_text_but_not_in_a_code_block_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    path = guide_file(root, "en")
    edit(path, "```bash\n", "Run `python3 scripts/verify.py --fast` while you work.\n\n```bash\n")
    edit(path, "\npython3 scripts/verify.py --fast\n", "\n")

    assert command_problems(read_text(path)) == ["the command python3 scripts/verify.py --fast is not in a code block"]


def test_AC_008_the_screenshots_command_is_one_of_the_commands() -> None:
    assert SCREENSHOTS_COMMAND in COMMANDS
    assert command_problems("```bash\n" + "\n".join(c for c in COMMANDS if c != SCREENSHOTS_COMMAND) + "\n```") == [
        f"the command {SCREENSHOTS_COMMAND} is not in a code block"
    ]


@pytest.mark.parametrize("literal", LITERALS)
def test_AC_008_a_literal_of_the_screenshots_or_of_chromium_missing_fails_and_is_named(literal: str) -> None:
    text = "\n".join(LITERALS)

    assert literal_problems(text, LITERALS, "screenshots and Chromium") == []
    assert literal_problems(text.replace(literal, ""), LITERALS, "screenshots and Chromium") == [
        f"screenshots and Chromium: {literal} is missing"
    ]


@pytest.mark.parametrize("word", [*DOCUMENT_PATH, *QUESTION_PATH, *ISOLATION, *SDD_FLOW])
def test_AC_008_a_technical_name_of_a_path_the_isolation_or_the_flow_missing_fails(tmp_path: Path, word: str) -> None:
    root = build_tree(tmp_path)
    path = guide_file(root, "en")
    head, tail = read_text(path).split("### Words")  # the diagram keeps its names: only the words of the section go
    write(path, head + "### Words" + tail.replace(word, ""))

    problems = all_problems(root, "en")

    assert problems
    assert all(f": {word} is missing" in problem for problem in problems)


@pytest.mark.parametrize("area", SENSITIVE)
def test_AC_008_a_sensitive_area_of_agents_md_missing_from_the_guide_fails_and_is_named(
    tmp_path: Path, area: str
) -> None:
    root = build_tree(tmp_path)
    edit(guide_file(root, "it"), f"`{area}`", area)

    assert all_problems(root, "it") == [f"the sensitive area `{area}` of AGENTS.md is not named"]


def test_AC_008_a_sensitive_area_added_to_agents_md_fails_in_both_guides(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "AGENTS.md", read_text(root / "AGENTS.md").replace("`compose.yaml`,", "`compose.yaml`, `secrets/`,"))

    assert "secrets/" in sensitive_paths(root)
    for lang in LANGS:
        assert all_problems(root, lang) == ["the sensitive area `secrets/` of AGENTS.md is not named"]


def test_AC_008_only_the_sensitive_areas_line_of_agents_md_is_read(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert "docs/adr/" not in sensitive_paths(root)
    assert "nothing" not in sensitive_paths(root)


def test_AC_008_guides_with_a_different_number_of_titles_fail_and_name_the_files(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert title_problems(root) == []

    edit(guide_file(root, "it"), "## References", "## References\n\n### One more")
    problems = title_problems(root)

    assert len(problems) == 1
    assert f"docs/guide/en/{ARCHITECTURE}" in problems[0]
    assert f"docs/guide/it/{ARCHITECTURE}" in problems[0]

    edit(guide_file(root, "it"), "### One more", "## One more")
    assert len(title_problems(root)) == 1


def test_AC_008_a_title_inside_a_code_block_is_not_a_title(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(guide_file(root, "en"), "```bash\n", "```bash\n## not a title\n")

    assert title_problems(root) == []


def test_AC_008_a_guide_with_windows_line_ends_and_a_bom_is_read_like_any_other(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(guide_file(root, "en"), "﻿" + complete_guide("en").replace("\n", "\r\n"))

    assert all_problems(root, "en") == []
    assert title_problems(root) == []
