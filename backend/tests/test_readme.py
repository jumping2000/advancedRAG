# SPEC-2026-11-20-fase-5-documentazione
"""The two READMEs, their badges and the license: AC-001, AC-002 (REQ-001), AC-003 (REQ-002) and AC-010 (REQ-009).

Every check is a function of a repository root, so that the tests below run it twice: on the real repository, and on a
small tree under `tmp_path` where the mistake is made on purpose (a badge missing, two badges swapped, a raw space in a
URL, a section left empty, a command that the installation guide does not have, a license with one byte changed, ...)
and must be reported by name. A check that cannot fail would let any README pass.

What is checked, and how:
- AC-003: the badges are read as `[![alt](image)](link)`. The groups are not counted by line: they are derived from the
  order. A badge whose image is `https://img.shields.io/badge/...` is static, any other is dynamic (the workflow badge
  of GitHub Actions and the dynamic ones of shields.io), so the kinds, in order, must be 5 dynamic, 8 static, 5 dynamic,
  2 static: status, technologies, repository, languages and method. The two READMEs must have the same images and the
  same links in the same order; every URL is encoded (no raw space and no `|`).
- AC-001: the six sections by position and exact title (the titles of REQ-001 in the language of the README), none of
  them empty; the badges stay between the title and the first section; the section Documentation links the four guides
  of its own language (and no README link goes to the guides of the other one), `docs/system/`, `docs/specs/` and
  `docs/adr/`; every relative link and image resolves. The first line after the title linking the twin is checked for
  all the twins by test_docs_alignment.py and is not repeated here.
- AC-002: the commands of the Quick start are in code blocks, are the ones of the installation guide of the same
  language (every line, word for word, so the two cannot drift), come in the order clone, `cd`, `cp`, the variables,
  start, and name every variable that Compose refuses to start without (read from `compose.yaml`) and the first admin.
  The running of the commands is done by hand and listed, with its result, in the pull request (AC-017, REQ-103).
- AC-010: `LICENSE` is the official Apache-2.0 text, the whole of it with its APPENDIX (202 lines): its SHA-256 is a
  constant of this file, and the hash is taken after the line ends are normalised to `\\n` and a BOM is dropped, so a
  checkout with `core.autocrlf` on Windows passes and any other change (one byte, a cut, another license) fails; there
  is no `NOTICE`; the section License names Apache-2.0 and links the file.
- Badge links: each of the 20 links is also compared with a reference list (`REFERENCE_BADGES`), so that the same
  wrong link in both READMEs fails.
That GitHub shows the license as `Apache-2.0` is seen on the page of the repository after the merge, not here.

REQ-102: the READMEs name no address but the placeholders of `.env.example`, `localhost` and the sites of the badges.
"""

from __future__ import annotations

import hashlib
import os
import re
from itertools import groupby
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.docs_support import FENCE, GUIDES, LANGS, REPO_ROOT, guide_path, read_text

README = {"en": "README.md", "it": "README.it.md"}
OWNER = "jumping2000/advancedRAG"
GITHUB = f"https://github.com/{OWNER}"
SHIELDS = "https://img.shields.io/"
STATIC = f"{SHIELDS}badge/"
WORKFLOW_IMAGE = f"{GITHUB}/actions/workflows/verify.yml/badge.svg?branch=main"
WORKFLOW_LINK = f"{GITHUB}/actions/workflows/verify.yml"
# The kinds in order, run by run: status (5), technologies (8), repository (5), languages and method (2)
GROUPS = [("dynamic", 5), ("static", 8), ("dynamic", 5), ("static", 2)]
# The dynamic badges of the two dynamic groups, in order: the workflow, then the paths of shields.io (without a query)
STATUS_IMAGES = [
    WORKFLOW_IMAGE,
    f"{SHIELDS}github/license/{OWNER}",
    f"{SHIELDS}github/last-commit/{OWNER}",
    f"{SHIELDS}github/issues/{OWNER}",
    f"{SHIELDS}github/issues-pr/{OWNER}",
]
REPOSITORY_IMAGES = [
    f"{SHIELDS}github/languages/top/{OWNER}",
    f"{SHIELDS}github/languages/code-size/{OWNER}",
    f"{SHIELDS}github/stars/{OWNER}",
    f"{SHIELDS}github/forks/{OWNER}",
    f"{SHIELDS}github/contributors/{OWNER}",
]
# The words that each static badge of the technologies shows, in order (read from the decoded image URL)
TECHNOLOGIES = [
    ("Python", "3.12"),
    ("FastAPI",),
    ("React", "19"),
    ("TypeScript",),
    ("PostgreSQL", "ParadeDB"),
    ("Redis",),
    ("Docker Compose",),
    ("Nginx",),
]
LANGUAGES_IMAGE = f"{STATIC}IT%20%7C%20EN-"  # «IT | EN»: the space as %20 and the pipe as %7C
METHOD_WORDS = "SDD playbook"
# The image (without its query) and the link of each of the 20 badges, in order: the reference of the links
REFERENCE_BADGES = [
    (WORKFLOW_IMAGE, WORKFLOW_LINK),
    (STATUS_IMAGES[1], f"{GITHUB}/blob/main/LICENSE"),
    (STATUS_IMAGES[2], f"{GITHUB}/commits/main"),
    (STATUS_IMAGES[3], f"{GITHUB}/issues"),
    (STATUS_IMAGES[4], f"{GITHUB}/pulls"),
    (f"{STATIC}Python-3.12-3776AB", f"{GITHUB}/blob/main/backend/pyproject.toml"),
    (f"{STATIC}FastAPI-009688", f"{GITHUB}/blob/main/backend/pyproject.toml"),
    (f"{STATIC}React-19-20232A", f"{GITHUB}/blob/main/frontend/package.json"),
    (f"{STATIC}TypeScript-3178C6", f"{GITHUB}/blob/main/frontend/package.json"),
    (f"{STATIC}PostgreSQL-ParadeDB-4169E1", f"{GITHUB}/blob/main/compose.yaml"),
    (f"{STATIC}Redis-DC382D", f"{GITHUB}/blob/main/compose.yaml"),
    (f"{STATIC}Docker%20Compose-2496ED", f"{GITHUB}/blob/main/compose.yaml"),
    (f"{STATIC}Nginx-009639", f"{GITHUB}/tree/main/nginx"),
    (REPOSITORY_IMAGES[0], GITHUB),
    (REPOSITORY_IMAGES[1], GITHUB),
    (REPOSITORY_IMAGES[2], f"{GITHUB}/stargazers"),
    (REPOSITORY_IMAGES[3], f"{GITHUB}/forks"),
    (REPOSITORY_IMAGES[4], f"{GITHUB}/graphs/contributors"),
    (f"{STATIC}IT%20%7C%20EN-blue", f"{GITHUB}/tree/main/docs/guide"),
    (f"{STATIC}SDD%20playbook-6f42c1", f"{GITHUB}/blob/main/AGENTS.md"),
]
# SHA-256 of the official text of the Apache License 2.0 (apache.org/licenses/LICENSE-2.0.txt, 202 lines with the
# APPENDIX), with `\n` line ends and no BOM
OFFICIAL_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

SECTIONS = {
    "en": ("Overview", "Main features", "Architecture at a glance", "Quick start", "Documentation", "License"),
    "it": ("Panoramica", "Funzioni principali", "Architettura in breve", "Avvio rapido", "Documentazione", "Licenza"),
}
SECTION_NAMES = ("overview", "features", "architecture", "quick start", "documentation", "license")
MIN_SECTION_CHARS = 40
CLONE = "git clone https://github.com/jumping2000/advancedRAG.git"
QUICK_START_ORDER = (CLONE, "cd advancedRAG", "cp .env.example .env", "docker compose up --build -d")
FIRST_ADMIN = ("ADMIN_USERNAME", "ADMIN_PASSWORD")
INSTALLATION = "installation.md"
DOCUMENTATION_FOLDERS = ("docs/system", "docs/specs", "docs/adr")
CORE_SERVICES = ("nginx", "backend", "worker", "redis", "db")
# Hosts that a README may name: the sites of the badges, `localhost`, and the placeholders of .env.example
ALLOWED_HOSTS = {"github.com", "img.shields.io", "localhost", "ollama.example.internal", "docling.example.internal"}

BADGE = re.compile(r"\[!\[([^\]]*)\]\(([^)\s]*)\)\]\(([^)\s]*)\)")
LINK = re.compile(r"\]\(\s*([^)\s]+)")
URL = re.compile(r"https?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)", re.IGNORECASE)
URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
PRIVATE_IP = re.compile(
    r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|127\.\d+\.\d+\.\d+)\b"
)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REQUIRED_BY_COMPOSE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")
COMPOSE_SERVICES = re.compile(r"^services:\s*(#.*)?$")
COMPOSE_KEY = re.compile(r"^(\s+)[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"']?\s*:(\s|$)")


# Reading the READMEs ----------------------------------------------------------------------------------


def badges(text: str) -> list[tuple[str, str, str]]:
    """The badges `[![alt](image)](link)` of a Markdown text, in order, as (alt, image URL, link); the text of a code
    block is not read. A link may be empty (the check names it); a bare image `![alt](image)` is not a badge."""
    outside = "\n".join(lines_outside_blocks(text))
    return [(alt, image, link) for alt, image, link in BADGE.findall(outside)]


def lines_outside_blocks(text: str) -> list[str]:
    return [line for line, kind in classified_lines(text) if kind == "text"]


def classified_lines(text: str) -> list[tuple[str, str]]:
    """Each line with its kind: `text`, `open` and `close` (the fence lines of a fenced code block) or `code`. A fence
    closes with the same character, at least as long, and no text after it."""
    marker = ""
    result = []
    for line in text.split("\n"):
        fence = FENCE.match(line)
        if not marker and fence:
            marker = fence.group(1)
            result.append((line, "open"))
        elif (
            marker
            and fence
            and not fence.group(2)
            and fence.group(1)[0] == marker[0]
            and len(fence.group(1)) >= len(marker)
        ):
            marker = ""
            result.append((line, "close"))
        else:
            result.append((line, "code" if marker else "text"))
    return result


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The text before the first `## ` title (the title, the link to the twin, the badges) and the (title, body) of
    each `## ` section; a `###` title stays in the body of its section. A `## ...` line in a code block is no title."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line, kind in classified_lines(text):
        title = re.match(r"^ {0,3}##[ \t]+(.*\S)[ \t]*$", line) if kind == "text" else None
        if title:
            sections.append((title.group(1), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble), [(title, "\n".join(body)) for title, body in sections]


def code_blocks(text: str) -> list[str]:
    """The text of each fenced code block, without its fence lines."""
    blocks: list[list[str]] = []
    for line, kind in classified_lines(text):
        if kind == "open":
            blocks.append([])
        elif kind == "code":
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def commands(text: str) -> list[str]:
    """The lines of the code blocks of a text that are commands or settings: stripped, no blank lines, no comments."""
    return [
        stripped
        for block in code_blocks(text)
        for stripped in (line.strip() for line in block.split("\n"))
        if stripped and not stripped.startswith("#")
    ]


def normal(target: str) -> str:
    """A relative link target without fragment, query, `./` or final `/` (`docs/system/` is `docs/system`)."""
    return os.path.normpath(re.split(r"[#?]", unquote(target))[0]).replace(os.sep, "/")


def is_local(target: str) -> bool:
    return bool(target) and not URL_SCHEME.match(target) and not target.startswith("#")


def other_language(lang: str) -> str:
    return next(other for other in LANGS if other != lang)


def load(root: Path, lang: str) -> str | None:
    path = root / README[lang]
    return read_text(path) if path.is_file() else None


def compose_services(root: Path) -> list[str]:
    """The names of the first level of `services:` in `compose.yaml`."""
    names: list[str] = []
    inside, indent = False, -1
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


def required_by_compose(root: Path) -> list[str]:
    """The variables that Compose refuses to start without (`${NAME:?message}`), in order, once each."""
    return list(dict.fromkeys(REQUIRED_BY_COMPOSE.findall(read_text(root / "compose.yaml"))))


# The checks -------------------------------------------------------------------------------------------


def badge_kind(image: str) -> str:
    return "static" if image.startswith(STATIC) else "dynamic"


def badge_problems(root: Path) -> list[str]:
    """AC-003: 20 badges in each README, the same in both, in order, in four groups, each with a link."""
    problems: list[str] = []
    found: dict[str, list[tuple[str, str, str]]] = {}
    for lang in LANGS:
        text = load(root, lang)
        if text is None:
            problems.append(f"{README[lang]}: the file does not exist")
            continue
        found[lang] = badges(text)
        problems += single_readme_badge_problems(README[lang], found[lang])
    if len(found) == len(LANGS):
        first, second = (found[lang] for lang in LANGS)
        for position, (one, two) in enumerate(zip(first, second, strict=False), 1):
            if one[1:] != two[1:]:
                problems.append(
                    f"badge {position}: {README['en']} has {one[1]} -> {one[2]}, "
                    f"{README['it']} has {two[1]} -> {two[2]}"
                )
        if len(first) != len(second):
            problems.append(f"{README['en']} has {len(first)} badges and {README['it']} has {len(second)}")
    return problems


def single_readme_badge_problems(name: str, items: list[tuple[str, str, str]]) -> list[str]:
    problems = []
    if len(items) != sum(count for _, count in GROUPS):
        problems.append(f"{name}: {len(items)} badges, {sum(count for _, count in GROUPS)} are expected")
    if not items:
        return problems
    for position, (alt, image, link) in enumerate(items, 1):
        where = f"{name}, badge {position}"
        if not alt.strip():
            problems.append(f"{where}: empty alternative text")
        if not link.strip():
            problems.append(f"{where}: no link")
        if re.search(r"[ |]", image) or re.search(r"[ |]", link):
            problems.append(f"{where}: a raw space or '|' in the URL (encode them as %20 and %7C): {image} {link}")
        if not link.startswith(f"{GITHUB}") and link.strip():
            problems.append(f"{where}: the link must be a page of {GITHUB}, it is {link}")
        if image != WORKFLOW_IMAGE and not image.startswith(SHIELDS):
            problems.append(f"{where}: the image must be on {SHIELDS} (or the workflow badge), it is {image}")
        if badge_kind(image) == "dynamic" and OWNER not in image:
            problems.append(f"{where}: a dynamic badge must point to {OWNER}, it is {image}")
    for position, ((_, image, link), (reference_image, reference_link)) in enumerate(
        zip(items, REFERENCE_BADGES, strict=False), 1
    ):
        base = image if image == WORKFLOW_IMAGE else image.split("?")[0]
        if base != reference_image or link != reference_link:
            problems.append(
                f"{name}, badge {position}: expected {reference_image} -> {reference_link}, it is {image} -> {link}"
            )
    if items[0][1] != WORKFLOW_IMAGE:
        problems.append(f"{name}: the first badge must be {WORKFLOW_IMAGE}, it is {items[0][1]}")
    elif items[0][2] != WORKFLOW_LINK:
        problems.append(f"{name}: the workflow badge must link {WORKFLOW_LINK}, it links {items[0][2]}")
    kinds = [(kind, len(list(run))) for kind, run in groupby(badge_kind(image) for _, image, _ in items)]
    if kinds != GROUPS:
        problems.append(f"{name}: the groups of badges by kind and size are {kinds}, expected {GROUPS}")
        return problems
    dynamic = [image.split("?")[0] if image != WORKFLOW_IMAGE else image for _, image, _ in items]
    if dynamic[:5] != STATUS_IMAGES:
        problems.append(f"{name}: the status badges are {dynamic[:5]}, expected {STATUS_IMAGES}")
    if dynamic[13:18] != REPOSITORY_IMAGES:
        problems.append(f"{name}: the repository badges are {dynamic[13:18]}, expected {REPOSITORY_IMAGES}")
    for (_, image, _), words in zip(items[5:13], TECHNOLOGIES, strict=True):
        if not all(word in unquote(image) for word in words):
            problems.append(f"{name}: the badge {image} must show {' and '.join(words)}")
    if not items[18][1].startswith(LANGUAGES_IMAGE):
        problems.append(f"{name}: the languages badge must start with {LANGUAGES_IMAGE}, it is {items[18][1]}")
    if METHOD_WORDS not in unquote(items[19][1]):
        problems.append(f"{name}: the last badge must show {METHOD_WORDS}, it is {items[19][1]}")
    return problems


def version_problems(items: list[tuple[str, str, str]], python: str, react: str) -> list[str]:
    """The static badges say the versions of the manifests: Python and React."""
    images = [unquote(image) for _, image, _ in items[5:13]]
    problems = []
    if len(images) == 8 and f"Python-{python}" not in images[0]:
        problems.append(f"the Python badge must say {python} (backend/pyproject.toml), it is {images[0]}")
    if len(images) == 8 and f"React-{react}" not in images[2]:
        problems.append(f"the React badge must say {react} (frontend/package.json), it is {images[2]}")
    return problems


def section_problems(root: Path, lang: str) -> list[str]:
    """AC-001: the title, the six sections in order, none empty, the badges before the first section."""
    text = load(root, lang)
    if text is None:
        return [f"{README[lang]}: the file does not exist"]
    name = README[lang]
    preamble, sections = split_sections(text)
    problems = []
    first = next((line for line in preamble.split("\n") if line.strip()), "")
    if not re.match(r"^# +\S", first) or "advancedRAG" not in first:
        problems.append(f"{name}: the first line must be the title `# ...` with the name advancedRAG, it is {first!r}")
    if len(badges(preamble)) != len(badges(text)):
        problems.append(f"{name}: the badges must stay between the title and the first section")
    titles = [title for title, _ in sections]
    if titles != list(SECTIONS[lang]):
        problems.append(f"{name}: the sections are {titles}, expected {list(SECTIONS[lang])}")
    for title, body in sections:
        words = "\n".join(line for line in body.split("\n") if not line.lstrip().startswith("#")).strip()
        if len(words) < MIN_SECTION_CHARS:
            problems.append(f"{name}: the section {title!r} is empty or almost ({len(words)} characters)")
    return problems


def section_body(text: str, lang: str, which: str) -> str | None:
    """The body of a section by position (`SECTION_NAMES`), or None when the README has no such section."""
    _, sections = split_sections(text)
    index = SECTION_NAMES.index(which)
    return sections[index][1] if index < len(sections) and sections[index][0] == SECTIONS[lang][index] else None


def link_problems(root: Path, lang: str) -> list[str]:
    """AC-001: the section Documentation links the four guides of the language and the three folders of docs/, and
    every relative link or image of the README resolves; no link goes to the guides of the other language."""
    text = load(root, lang)
    if text is None:
        return [f"{README[lang]}: the file does not exist"]
    name = README[lang]
    problems = []
    documentation = section_body(text, lang, "documentation")
    if documentation is None:
        problems.append(f"{name}: no section {SECTIONS[lang][4]!r} in its place")
    else:
        linked = {normal(target) for target in LINK.findall(documentation) if is_local(target)}
        required = [f"docs/guide/{lang}/{guide}" for guide in GUIDES] + list(DOCUMENTATION_FOLDERS)
        problems += [
            f"{name}: the section {SECTIONS[lang][4]!r} must link {path}" for path in required if path not in linked
        ]
    for target in LINK.findall("\n".join(lines_outside_blocks(text))):
        if not is_local(target):
            continue
        if normal(target).startswith(f"docs/guide/{other_language(lang)}/"):
            problems.append(f"{name}: the link {target} goes to the guides of the other language")
        if not (root / normal(target)).exists():
            problems.append(f"{name}: the relative link {target} does not resolve")
    return problems


def quick_start_problems(root: Path, lang: str) -> list[str]:
    """AC-002: the commands of the Quick start are those of the installation guide of the same language."""
    text = load(root, lang)
    if text is None:
        return [f"{README[lang]}: the file does not exist"]
    name = README[lang]
    body = section_body(text, lang, "quick start")
    if body is None:
        return [f"{name}: no section {SECTIONS[lang][3]!r} in its place"]
    guide = guide_path(lang, INSTALLATION, root)
    if not guide.is_file():
        return [f"{name}: the installation guide {guide.relative_to(root).as_posix()} does not exist"]
    problems = []
    lines = commands(body)
    in_guide = commands(read_text(guide))
    problems += [
        f"{name}: the Quick start line {line!r} is not a line of a code block of {guide.relative_to(root).as_posix()}"
        for line in lines
        if line not in in_guide
    ]
    positions = [lines.index(command) if command in lines else -1 for command in QUICK_START_ORDER]
    for command, position in zip(QUICK_START_ORDER, positions, strict=True):
        if position < 0:
            problems.append(f"{name}: the Quick start has no line {command!r} in a code block")
    found = [position for position in positions if position >= 0]
    if found != sorted(found):
        problems.append(f"{name}: the commands of the Quick start must come in the order {list(QUICK_START_ORDER)}")
    settings = [line for line in lines if ASSIGNMENT.match(line)]
    for variable in [*required_by_compose(root), *FIRST_ADMIN]:
        if not any(line.startswith(f"{variable}=") for line in settings):
            problems.append(f"{name}: the Quick start must set {variable} in a code block")
    if positions[2] >= 0 and positions[3] >= 0 and settings:
        last_setting = max(lines.index(line) for line in settings)
        if not positions[2] < min(lines.index(line) for line in settings) <= last_setting < positions[3]:
            problems.append(f"{name}: the settings come after `cp .env.example .env` and before the start")
    if "http://localhost" not in body:
        problems.append(f"{name}: the Quick start must give the address http://localhost of the login page")
    target = f"docs/guide/{lang}/{INSTALLATION}"
    if target not in {normal(link) for link in LINK.findall(body) if is_local(link)}:
        problems.append(f"{name}: the Quick start must link the installation guide {target}")
    return problems


def architecture_problems(root: Path, lang: str) -> list[str]:
    """The Architecture at a glance names the services of Compose (they exist in compose.yaml) and the two external
    ones, in a Mermaid `flowchart`."""
    text = load(root, lang)
    if text is None:
        return [f"{README[lang]}: the file does not exist"]
    name = README[lang]
    body = section_body(text, lang, "architecture")
    if body is None:
        return [f"{name}: no section {SECTIONS[lang][2]!r} in its place"]
    services = compose_services(root)
    problems = [f"compose.yaml has no service {service}" for service in CORE_SERVICES if service not in services]
    diagrams = [block for block in re.findall(r"```mermaid\n(.*?)```", body, re.DOTALL)]
    if not any(diagram.lstrip().startswith("flowchart") for diagram in diagrams):
        problems.append(f"{name}: the Architecture at a glance needs a Mermaid flowchart")
    shown = " ".join(diagrams)
    problems += [
        f"{name}: the flowchart does not show {word}"
        for word in (*CORE_SERVICES, "Ollama", "Docling")
        if word not in shown
    ]
    return problems


def address_problems(root: Path, lang: str) -> list[str]:
    """REQ-102: no address but the allowed hosts, and no private IP address."""
    text = load(root, lang)
    if text is None:
        return [f"{README[lang]}: the file does not exist"]
    name = README[lang]
    problems = [
        f"{name}: the host {host} is not one of {sorted(ALLOWED_HOSTS)}"
        for host in dict.fromkeys(URL.findall(text))
        if host.lower() not in ALLOWED_HOSTS
    ]
    problems += [f"{name}: the private address {match.group(0)}" for match in PRIVATE_IP.finditer(text)]
    return problems


def normalized(data: bytes) -> bytes:
    """The bytes of a text file with a leading BOM dropped and `\\r\\n` and `\\r` read as `\\n`, as a checkout on
    Windows with `core.autocrlf` writes it."""
    return data.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def license_problems(root: Path, sha256: str) -> list[str]:
    """AC-010: `LICENSE` is the official text (SHA-256 after the line ends are normalised); there is no NOTICE."""
    path = root / "LICENSE"
    if not path.is_file():
        return ["LICENSE: the file does not exist"]
    data = normalized(path.read_bytes())
    problems = []
    found = hashlib.sha256(data).hexdigest()
    if found != sha256:
        lines = data.count(b"\n")
        problems.append(f"LICENSE: it is not the official text: sha256 {found} ({lines} lines), expected {sha256}")
    if (root / "NOTICE").exists():
        problems.append("NOTICE: there must be no NOTICE file (REQ-009)")
    return problems


def license_section_problems(root: Path, lang: str) -> list[str]:
    """The section License names Apache-2.0 and links the file LICENSE."""
    text = load(root, lang)
    if text is None:
        return [f"{README[lang]}: the file does not exist"]
    name = README[lang]
    body = section_body(text, lang, "license")
    if body is None:
        return [f"{name}: no section {SECTIONS[lang][5]!r} in its place"]
    problems = []
    if "Apache-2.0" not in body:
        problems.append(f"{name}: the section {SECTIONS[lang][5]!r} must name Apache-2.0")
    if "LICENSE" not in {normal(link) for link in LINK.findall(body) if is_local(link)}:
        problems.append(f"{name}: the section {SECTIONS[lang][5]!r} must link the file LICENSE")
    return problems


def assert_no_problems(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# Trees under tmp_path ---------------------------------------------------------------------------------


def sample_badges() -> list[tuple[str, str, str]]:
    """20 valid badges: what the checks accept (the images and links of `REFERENCE_BADGES`)."""
    alts = [
        "verify", "license", "last commit", "issues", "pull requests", "Python", "FastAPI", "React", "TypeScript",
        "PostgreSQL", "Redis", "Docker Compose", "Nginx", "language", "size", "stars", "forks", "contributors",
        "IT | EN", "SDD playbook",
    ]  # fmt: skip
    items = [(alt, image, link) for alt, (image, link) in zip(alts, REFERENCE_BADGES, strict=True)]
    items[2] = (items[2][0], f"{items[2][1]}?style=flat", items[2][2])  # a query is allowed on a dynamic badge
    return items


def badge_markdown(items: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"[![{alt}]({image})]({link})" for alt, image, link in items)


FILLER = "A sentence long enough to count as the text of a section of this README."
START_BLOCKS = (
    f"{CLONE}\ncd advancedRAG\ncp .env.example .env",
    "POSTGRES_PASSWORD=change-me\nOLLAMA_BASE_URL=http://ollama.example.internal:11434\n"
    "DOCLING_BASE_URL=http://docling.example.internal:5001\nADMIN_USERNAME=admin\n"
    "ADMIN_PASSWORD=change-me-at-least-12-characters",
    "docker compose up --build -d",
)
FLOWCHART = (
    "```mermaid\nflowchart LR\n    nginx --> backend\n    backend --> db\n    backend --> redis\n    worker --> redis\n"
    "    backend --> Ollama\n    worker --> Docling\n```"
)


def readme_text(lang: str, items: list[tuple[str, str, str]] | None = None, **replace: str) -> str:
    """A valid README of the language; `replace` swaps the text of a part (`quick`, `documentation`, ...)."""
    items = sample_badges() if items is None else items
    twin = f"[{other_language(lang).upper()}]({README[other_language(lang)]})"
    titles = SECTIONS[lang]
    guides = "\n".join(f"- [{guide}](docs/guide/{lang}/{guide})" for guide in GUIDES)
    parts = {
        "overview": FILLER,
        "features": FILLER,
        "architecture": f"{FILLER}\n\n{FLOWCHART}",
        "quick": "\n\n".join(f"```bash\n{block}\n```" for block in START_BLOCKS)
        + f"\n\nOpen http://localhost. More in [the guide](docs/guide/{lang}/{INSTALLATION}).",
        "documentation": f"{guides}\n- [system](docs/system/)\n- [specs](docs/specs/)\n- [adr](docs/adr/)",
        "license": "Apache-2.0, see [LICENSE](LICENSE), the license of the whole repository.",
    }
    parts.update(replace)
    keys = ("overview", "features", "architecture", "quick", "documentation", "license")
    sections = "\n\n".join(f"## {title}\n\n{parts[key]}" for title, key in zip(titles, keys, strict=True))
    return f"# advancedRAG\n\n{twin}\n\n{badge_markdown(items)}\n\n{sections}\n"


REFERENCE_TERMS = (
    b"\n                                 Apache License\n                           Version 2.0, January 2004\n"
    b"\n   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION\n\n   1. Definitions.\n"
    b"\n   END OF TERMS AND CONDITIONS\n"
)
REFERENCE_APPENDIX = (
    b"\n   APPENDIX: How to apply the Apache License to your work.\n\n   Copyright [yyyy] [name of copyright owner]\n"
    b'\n   Licensed under the Apache License, Version 2.0 (the "License");\n'
)
REFERENCE_LICENSE = REFERENCE_TERMS + REFERENCE_APPENDIX
REFERENCE_SHA256 = hashlib.sha256(REFERENCE_LICENSE).hexdigest()


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def build_tree(root: Path) -> Path:
    """A small repository where everything is right: both READMEs, the installation guides with the commands, the four
    guides of each language, the folders of docs/, compose.yaml, LICENSE."""
    write(
        root / "compose.yaml",
        "services:\n  nginx:\n  backend:\n  worker:\n  redis:\n  db:\n    environment:\n"
        "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}\n      O: ${OLLAMA_BASE_URL:?set it}\n"
        "      D: ${DOCLING_BASE_URL:?set it}\n      P: ${POSTGRES_PASSWORD:?again}\nvolumes:\n  pgdata:\n",
    )
    for lang in LANGS:
        write(root / README[lang], readme_text(lang))
        for guide in GUIDES:
            write(guide_path(lang, guide, root), f"# {guide}\n")
        write(guide_path(lang, INSTALLATION, root), "# Installation\n\n```bash\n" + "\n".join(START_BLOCKS) + "\n```\n")
    for folder in DOCUMENTATION_FOLDERS:
        write(root / folder / "index.md", "# index\n")
    write(root / "LICENSE", REFERENCE_LICENSE)
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = read_text(path)
    assert old in text
    write(path, text.replace(old, new, 1))


# AC-003: the badges -----------------------------------------------------------------------------------


def test_AC_003_the_reader_extracts_alt_image_and_link_of_each_badge_and_skips_code_blocks() -> None:
    text = (
        "[![a b](https://img.shields.io/badge/IT%20%7C%20EN-blue?x=1&y=2)](https://github.com/o/r)\n"
        "![bare](https://img.shields.io/badge/x-y)\n[![empty](https://img.shields.io/badge/e-f)]()\n"
        "```\n[![code](https://img.shields.io/badge/c-d)](https://github.com/o/r)\n```\n"
    )

    assert badges(text) == [
        ("a b", "https://img.shields.io/badge/IT%20%7C%20EN-blue?x=1&y=2", "https://github.com/o/r"),
        ("empty", "https://img.shields.io/badge/e-f", ""),
    ]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_003_each_readme_has_20_badges_in_four_groups_each_with_a_link(lang: str) -> None:
    text = load(REPO_ROOT, lang)
    assert text is not None, f"{README[lang]} does not exist"
    assert_no_problems(single_readme_badge_problems(README[lang], badges(text)))


def test_AC_003_the_two_readmes_have_the_same_badges_in_the_same_order() -> None:
    assert_no_problems(badge_problems(REPO_ROOT))


def test_AC_003_the_first_badge_is_the_workflow_verify_on_main_and_the_languages_badge_is_encoded() -> None:
    for lang in LANGS:
        text = load(REPO_ROOT, lang)
        assert text is not None, f"{README[lang]} does not exist"
        items = badges(text)
        assert items[0][1] == (
            "https://github.com/jumping2000/advancedRAG/actions/workflows/verify.yml/badge.svg?branch=main"
        )
        assert items[18][1].startswith("https://img.shields.io/badge/IT%20%7C%20EN-")


def test_AC_003_the_static_badges_say_the_versions_of_the_manifests() -> None:
    python = re.search(r"requires-python\s*=\s*\">=(\d+\.\d+)", read_text(REPO_ROOT / "backend" / "pyproject.toml"))
    react = re.search(r"\"react\":\s*\"[^\d]*(\d+)\.", read_text(REPO_ROOT / "frontend" / "package.json"))
    assert python and react
    for lang in LANGS:
        text = load(REPO_ROOT, lang)
        assert text is not None, f"{README[lang]} does not exist"
        assert_no_problems(version_problems(badges(text), python.group(1), react.group(1)))


def test_AC_003_a_tree_with_the_same_valid_badges_in_both_readmes_passes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert len(sample_badges()) == 20
    assert badge_problems(root) == []
    assert version_problems(sample_badges(), "3.12", "19") == []


def test_AC_003_a_badge_missing_from_one_readme_fails_and_names_the_count(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["it"], readme_text("it", sample_badges()[:-1]))

    problems = badge_problems(root)

    assert any("README.it.md: 19 badges, 20 are expected" in problem for problem in problems)
    assert any("README.md has 20 badges and README.it.md has 19" in problem for problem in problems)


def test_AC_003_two_badges_swapped_in_one_readme_fail_and_name_the_position(tmp_path: Path) -> None:
    items = sample_badges()
    items[1], items[2] = items[2], items[1]
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en", items))

    problems = badge_problems(root)

    assert any(problem.startswith("badge 2:") for problem in problems)
    assert any(problem.startswith("badge 3:") for problem in problems)


def test_AC_003_a_different_link_in_one_readme_fails(tmp_path: Path) -> None:
    items = sample_badges()
    items[3] = (items[3][0], items[3][1], f"{GITHUB}/issues?q=is%3Aopen")
    root = build_tree(tmp_path)
    write(root / README["it"], readme_text("it", items))

    assert any(problem.startswith("badge 4:") for problem in badge_problems(root))


def test_AC_003_the_same_wrong_link_in_both_readmes_fails_against_the_reference(tmp_path: Path) -> None:
    items = sample_badges()
    items[3] = (items[3][0], items[3][1], f"{GITHUB}/pulls")  # the issues badge links the pull requests, twice
    items[18] = (items[18][0], items[18][1], f"{GITHUB}/tree/main/docs")
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    problems = badge_problems(root)

    assert not any(problem.startswith("badge ") for problem in problems), "the two READMEs agree with each other"
    for name in README.values():
        assert any(f"{name}, badge 4: expected" in problem and f"{GITHUB}/issues" in problem for problem in problems)
        assert any(f"{name}, badge 19: expected" in problem for problem in problems)
    assert len([problem for problem in problems if ": expected" in problem]) == 4


def test_AC_003_a_badge_with_another_image_at_its_place_fails_against_the_reference(tmp_path: Path) -> None:
    items = sample_badges()
    items[10] = (items[10][0], f"{STATIC}Redis-FF0000", items[10][2])  # same words, another color: not the reference
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    assert any("badge 11: expected" in problem for problem in badge_problems(root))


def test_AC_003_a_badge_without_a_link_fails(tmp_path: Path) -> None:
    items = sample_badges()
    items[7] = (items[7][0], items[7][1], "")
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    problems = badge_problems(root)

    assert any("README.md, badge 8: no link" in problem for problem in problems)
    assert any("README.it.md, badge 8: no link" in problem for problem in problems)


def test_AC_003_the_workflow_badge_must_be_the_exact_url_with_branch_main(tmp_path: Path) -> None:
    items = sample_badges()
    items[0] = (items[0][0], WORKFLOW_IMAGE.replace("?branch=main", ""), items[0][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    assert any("the first badge must be" in problem for problem in badge_problems(root))


def test_AC_003_a_raw_space_in_an_image_url_breaks_the_badge_and_is_counted(tmp_path: Path) -> None:
    # a space cannot be written inside a Markdown destination: the reader does not see the badge any more
    items = sample_badges()
    items[18] = ("IT | EN", f"{STATIC}IT EN-blue", items[18][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    assert any("19 badges, 20 are expected" in problem for problem in badge_problems(root))


def test_AC_003_a_pipe_in_the_languages_badge_is_reported_with_the_rule(tmp_path: Path) -> None:
    items = sample_badges()
    items[18] = ("IT | EN", f"{STATIC}IT%20|%20EN-blue", items[18][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    problems = badge_problems(root)

    assert any("a raw space or '|' in the URL" in problem for problem in problems)
    assert any("the languages badge must start with" in problem for problem in problems)


def test_AC_003_a_dynamic_badge_of_another_repository_fails(tmp_path: Path) -> None:
    items = sample_badges()
    items[4] = (items[4][0], f"{SHIELDS}github/issues-pr/someone/else", items[4][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    problems = badge_problems(root)

    assert any("must point to jumping2000/advancedRAG" in problem for problem in problems)


def test_AC_003_an_image_on_another_site_fails(tmp_path: Path) -> None:
    items = sample_badges()
    items[6] = (items[6][0], "https://badges.example.org/Redis.svg", items[6][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    assert any("the image must be on https://img.shields.io/" in problem for problem in badge_problems(root))


def test_AC_003_a_dynamic_badge_in_the_group_of_the_technologies_breaks_the_groups(tmp_path: Path) -> None:
    items = sample_badges()
    items[8] = (items[8][0], f"{SHIELDS}github/stars/{OWNER}", items[8][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    assert any("the groups of badges by kind and size" in problem for problem in badge_problems(root))


def test_AC_003_a_wrong_technology_or_version_fails(tmp_path: Path) -> None:
    items = sample_badges()
    items[9] = (items[9][0], f"{STATIC}Postgres-4169E1", items[9][2])
    items[5] = (items[5][0], f"{STATIC}Python-3.11-3776AB", items[5][2])
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(root / README[lang], readme_text(lang, items))

    assert any("must show Python and 3.12" in problem for problem in badge_problems(root))
    assert any("must show PostgreSQL and ParadeDB" in problem for problem in badge_problems(root))
    assert version_problems(items, "3.12", "19")
    assert version_problems(sample_badges(), "3.12", "20")


def test_AC_003_a_missing_readme_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / README["it"]).unlink()

    assert badge_problems(root) == ["README.it.md: the file does not exist"]


# AC-001: sections, links, quick start -------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_001_each_readme_has_the_title_and_the_six_sections_in_order_none_empty(lang: str) -> None:
    assert_no_problems(section_problems(REPO_ROOT, lang))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_001_the_documentation_links_the_four_guides_of_the_language_and_every_link_resolves(lang: str) -> None:
    assert_no_problems(link_problems(REPO_ROOT, lang))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_001_the_architecture_names_the_services_of_compose_in_a_flowchart(lang: str) -> None:
    assert_no_problems(architecture_problems(REPO_ROOT, lang))


@pytest.mark.parametrize("lang", LANGS)
def test_REQ_102_the_readme_names_no_address_but_the_placeholders(lang: str) -> None:
    assert_no_problems(address_problems(REPO_ROOT, lang))


def test_AC_001_a_tree_with_valid_readmes_passes_every_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    for lang in LANGS:
        assert section_problems(root, lang) == []
        assert link_problems(root, lang) == []
        assert quick_start_problems(root, lang) == []
        assert architecture_problems(root, lang) == []
        assert address_problems(root, lang) == []
        assert license_section_problems(root, lang) == []


def test_AC_001_a_renamed_section_fails_and_names_the_sections(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en").replace("## Documentation", "## Docs"))

    problems = section_problems(root, "en")

    assert len(problems) == 1
    assert "the sections are" in problems[0] and "'Docs'" in problems[0]
    assert section_problems(root, "it") == []


def test_AC_001_sections_out_of_order_or_one_more_fail(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    text = readme_text("it")
    swapped = (
        text.replace("## Avvio rapido", "## TEMP")
        .replace("## Documentazione", "## Avvio rapido")
        .replace("## TEMP", "## Documentazione")
    )
    write(root / README["it"], swapped)
    write(root / README["en"], readme_text("en") + "\n## Contributing\n\n" + FILLER + "\n")

    assert any("the sections are" in problem for problem in section_problems(root, "it"))
    assert any("the sections are" in problem for problem in section_problems(root, "en"))


def test_AC_001_an_empty_section_fails_and_names_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en", features=""))
    write(root / README["it"], readme_text("it", overview="### Solo un titolo"))

    assert any("the section 'Main features' is empty" in problem for problem in section_problems(root, "en"))
    assert any("the section 'Panoramica' is empty" in problem for problem in section_problems(root, "it"))


def test_AC_001_a_readme_without_the_title_or_with_a_badge_below_a_section_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en").replace("# advancedRAG", "advancedRAG", 1))
    write(root / README["it"], readme_text("it", overview=f"{FILLER}\n\n{badge_markdown(sample_badges()[:1])}"))

    assert any("the first line must be the title" in problem for problem in section_problems(root, "en"))
    assert any(
        "must stay between the title and the first section" in problem for problem in section_problems(root, "it")
    )


def test_AC_001_a_header_inside_a_code_block_is_not_a_section() -> None:
    text = "# T\n\n## One\n\n```\n## Not a section\n```\n\n## Two\n\ntext\n"

    assert [title for title, _ in split_sections(text)[1]] == ["One", "Two"]


@pytest.mark.parametrize(
    "missing", ["user.md", "admin.md", "installation.md", "architecture.md", "docs/system/", "docs/specs/", "docs/adr/"]
)
def test_AC_001_the_documentation_without_a_required_link_fails_and_names_it(tmp_path: Path, missing: str) -> None:
    root = build_tree(tmp_path)
    path = root / README["en"]
    wanted = missing if missing.startswith("docs/") else f"docs/guide/en/{missing}"
    text = read_text(path)
    kept = "\n".join(line for line in text.split("\n") if f"]({wanted})" not in line)
    write(path, kept)

    problems = link_problems(root, "en")

    assert any(f"must link {wanted.rstrip('/')}" in problem for problem in problems), problems


def test_AC_001_a_link_to_the_guides_of_the_other_language_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["it"], readme_text("it", features=f"{FILLER} Vedi [admin](docs/guide/en/admin.md)."))

    problems = link_problems(root, "it")

    assert any("goes to the guides of the other language" in problem for problem in problems)
    assert link_problems(root, "en") == []


def test_AC_001_a_broken_relative_link_or_image_fails_and_names_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(
        root / README["en"],
        readme_text(
            "en",
            overview="See [x](docs/missing.md) and ![s](docs/guide/images/en/none.png). "
            "A [site](https://example.org/x) and an [anchor](#a) are not read.",
        ),
    )

    problems = link_problems(root, "en")

    assert len([problem for problem in problems if "does not resolve" in problem]) == 2
    assert any("docs/missing.md" in problem for problem in problems)
    assert any("none.png" in problem for problem in problems)


def test_AC_001_a_link_to_a_file_with_a_fragment_resolves(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en", overview="See [x](docs/system/index.md#a-title) and [y](LICENSE)."))

    assert link_problems(root, "en") == []


def test_AC_001_a_missing_readme_is_named_by_every_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / README["en"]).unlink()

    for check in (
        section_problems,
        link_problems,
        quick_start_problems,
        architecture_problems,
        address_problems,
        license_section_problems,
    ):
        assert check(root, "en") == ["README.md: the file does not exist"]


# AC-002: the quick start --------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_002_the_quick_start_commands_are_those_of_the_installation_guide(lang: str) -> None:
    assert_no_problems(quick_start_problems(REPO_ROOT, lang))


def test_AC_002_the_compose_variables_come_from_compose_yaml() -> None:
    assert required_by_compose(REPO_ROOT) == ["POSTGRES_PASSWORD", "OLLAMA_BASE_URL", "DOCLING_BASE_URL"]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_002_a_command_that_the_installation_guide_does_not_have_fails_and_is_named(
    tmp_path: Path, lang: str
) -> None:
    root = build_tree(tmp_path)
    edit(root / README[lang], "docker compose up --build -d", "docker compose up --build --detach")

    problems = quick_start_problems(root, lang)

    assert any("'docker compose up --build --detach' is not a line of a code block of" in p for p in problems)
    assert any("has no line 'docker compose up --build -d'" in p for p in problems)


def test_AC_002_a_command_of_the_guide_changed_makes_the_readme_fail(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(guide_path("it", INSTALLATION, root), "cp .env.example .env", "cp .env.example .env.local")

    assert any("'cp .env.example .env' is not a line" in problem for problem in quick_start_problems(root, "it"))
    assert quick_start_problems(root, "en") == []


def test_AC_002_commands_outside_a_code_block_do_not_count(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / README["en"], "```bash\ndocker compose up --build -d\n```", "Run docker compose up --build -d now.")

    assert any(
        "has no line 'docker compose up --build -d' in a code block" in p for p in quick_start_problems(root, "en")
    )


def test_AC_002_the_commands_out_of_order_fail(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(root / README["en"], "cd advancedRAG\ncp .env.example .env", "cp .env.example .env\ncd advancedRAG")

    assert any("must come in the order" in problem for problem in quick_start_problems(root, "en"))


@pytest.mark.parametrize(
    "variable", ["POSTGRES_PASSWORD", "OLLAMA_BASE_URL", "DOCLING_BASE_URL", "ADMIN_USERNAME", "ADMIN_PASSWORD"]
)
def test_AC_002_a_variable_that_the_stack_needs_and_the_quick_start_does_not_set_fails(
    tmp_path: Path, variable: str
) -> None:
    root = build_tree(tmp_path)
    path = root / README["en"]
    write(path, "\n".join(line for line in read_text(path).split("\n") if not line.startswith(f"{variable}=")))

    assert any(f"must set {variable}" in problem for problem in quick_start_problems(root, "en"))


def test_AC_002_a_variable_that_compose_requires_is_read_from_compose_yaml(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(
        root / "compose.yaml",
        "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}",
        "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}\n      X: ${NEW_REQUIRED:?set it}",
    )

    assert any("must set NEW_REQUIRED" in problem for problem in quick_start_problems(root, "en"))


def test_AC_002_the_settings_before_the_copy_of_the_env_file_fail(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    blocks = [f"```bash\n{block}\n```" for block in START_BLOCKS]
    write(
        root / README["en"],
        readme_text(
            "en",
            quick="\n\n".join([blocks[1], blocks[0], blocks[2]])
            + f"\n\nOpen http://localhost. [guide](docs/guide/en/{INSTALLATION})",
        ),
    )

    assert any("the settings come after" in problem for problem in quick_start_problems(root, "en"))


def test_AC_002_a_quick_start_without_the_address_or_the_guide_link_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en", quick="\n\n".join(f"```bash\n{block}\n```" for block in START_BLOCKS)))

    problems = quick_start_problems(root, "en")

    assert any("must give the address http://localhost" in problem for problem in problems)
    assert any("must link the installation guide docs/guide/en/installation.md" in problem for problem in problems)


def test_AC_002_a_missing_installation_guide_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    guide_path("it", INSTALLATION, root).unlink()

    problems = quick_start_problems(root, "it")

    assert len(problems) == 1
    assert "docs/guide/it/installation.md does not exist" in problems[0]


def test_AC_001_the_architecture_without_a_flowchart_or_a_service_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en", architecture=f"{FILLER} Nothing drawn."))
    write(root / README["it"], readme_text("it", architecture=FLOWCHART.replace("worker --> Docling\n", "")))
    edit(root / "compose.yaml", "  worker:\n", "")

    assert any("needs a Mermaid flowchart" in problem for problem in architecture_problems(root, "en"))
    assert any("does not show Docling" in problem for problem in architecture_problems(root, "it"))
    assert any("compose.yaml has no service worker" in problem for problem in architecture_problems(root, "it"))


def test_REQ_102_an_internal_address_or_another_host_fails_and_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(
        root / README["en"],
        readme_text("en", overview="Ollama at http://10.1.2.3:11434 and https://corp.example.org/wiki."),
    )

    problems = address_problems(root, "en")

    assert any("the host corp.example.org" in problem for problem in problems)
    assert any("the private address 10.1.2.3" in problem for problem in problems)
    assert address_problems(root, "it") == []


# AC-010: the license ---------------------------------------------------------------------------------------


def test_AC_010_license_has_the_appendix_of_the_official_text() -> None:
    path = REPO_ROOT / "LICENSE"
    text = path.read_bytes() if path.is_file() else b""

    assert b"APPENDIX: How to apply the Apache License to your work." in text, "LICENSE has no APPENDIX"


def test_AC_010_license_is_the_official_apache_text() -> None:
    assert_no_problems(license_problems(REPO_ROOT, OFFICIAL_LICENSE_SHA256))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_010_the_license_section_names_apache_2_0_and_links_the_file(lang: str) -> None:
    assert_no_problems(license_section_problems(REPO_ROOT, lang))


def test_AC_010_a_license_equal_to_the_reference_passes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert license_problems(root, REFERENCE_SHA256) == []


def test_AC_010_one_changed_byte_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    changed = bytearray(REFERENCE_LICENSE)
    changed[40] ^= 0x01
    write(root / "LICENSE", bytes(changed))

    problems = license_problems(root, REFERENCE_SHA256)

    assert len(problems) == 1
    assert "it is not the official text" in problems[0]


def test_AC_010_a_different_license_text_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "LICENSE", "MIT License\n\nPermission is hereby granted, free of charge, to any person...\n")

    assert any("it is not the official text" in problem for problem in license_problems(root, REFERENCE_SHA256))


def test_AC_010_the_text_without_the_appendix_fails(tmp_path: Path) -> None:
    # the 177-line version of the terms alone (the one that the distribution packaging ships) is not the official file
    root = build_tree(tmp_path)
    write(root / "LICENSE", REFERENCE_TERMS)

    assert license_problems(root, REFERENCE_SHA256)


def test_AC_010_a_truncated_or_extended_license_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "LICENSE", REFERENCE_LICENSE[:-10])
    assert license_problems(root, REFERENCE_SHA256)
    write(root / "LICENSE", REFERENCE_LICENSE + b"Copyright 2026 Someone\n")
    assert license_problems(root, REFERENCE_SHA256)
    write(root / "LICENSE", REFERENCE_LICENSE.replace(b"Apache", b"Apachf", 1))
    assert license_problems(root, REFERENCE_SHA256)


@pytest.mark.parametrize(
    "variant",
    [
        REFERENCE_LICENSE.replace(b"\n", b"\r\n"),
        REFERENCE_LICENSE.replace(b"\n", b"\r"),
        b"\xef\xbb\xbf" + REFERENCE_LICENSE,
        b"\xef\xbb\xbf" + REFERENCE_LICENSE.replace(b"\n", b"\r\n"),
    ],
    ids=["crlf", "cr", "bom", "bom-and-crlf"],
)
def test_AC_010_a_checkout_with_windows_line_ends_or_a_bom_passes(tmp_path: Path, variant: bytes) -> None:
    root = build_tree(tmp_path)
    write(root / "LICENSE", variant)

    assert license_problems(root, REFERENCE_SHA256) == []


def test_AC_010_a_change_of_words_in_a_crlf_copy_still_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "LICENSE", REFERENCE_LICENSE.replace(b"\n", b"\r\n").replace(b"License", b"Licence", 1))

    assert license_problems(root, REFERENCE_SHA256)


def test_AC_010_a_missing_license_or_an_added_notice_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "NOTICE", "advancedRAG\n")
    assert license_problems(root, REFERENCE_SHA256) == ["NOTICE: there must be no NOTICE file (REQ-009)"]
    (root / "LICENSE").unlink()
    assert license_problems(root, REFERENCE_SHA256) == ["LICENSE: the file does not exist"]


def test_AC_010_the_license_section_without_apache_or_the_link_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / README["en"], readme_text("en", license="MIT, see the file LICENSE in the root of the repository."))
    write(
        root / README["it"], readme_text("it", license="Apache-2.0, vedi il file LICENSE nella radice del repository.")
    )

    en = license_section_problems(root, "en")
    it = license_section_problems(root, "it")

    assert any("must name Apache-2.0" in problem for problem in en)
    assert any("must link the file LICENSE" in problem for problem in en)
    assert any("must link the file LICENSE" in problem for problem in it)
    assert not any("must name" in problem for problem in it)
