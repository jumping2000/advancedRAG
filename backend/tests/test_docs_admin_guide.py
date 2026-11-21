# SPEC-2026-11-20-fase-5-documentazione
"""The admin guide, in both languages, and its screenshots: AC-006 (REQ-005), AC-009 (REQ-008), AC-016 (REQ-102).

AC-006: the guide names the texts of the interface exactly as `frontend/src/i18n/<lang>.json` has them: the five pages
of the admin menu, the forms, buttons and messages of the users, the knowledge bases (and their assigned users), the
documents (formats, statuses, reasons, «Retry»), the conversations and the two switches of the settings with «Not
configured» (REQUIRED_UI_KEYS). Each text stands between «» (so that «Retry» is not found inside a longer message),
and everything between «» is a text of the language file as it is written. A text with a changing part (a user name,
a title) is written in italics with `…` in place of that part, and the italics are reserved for these texts
(INTERPOLATED_UI_KEYS): the whole text, with `…` for each `{{name}}`, must be in the guide. The guide links the
installation guide (the variables of the two steps), the user guide and the three area pages that hold the technical
details, and every relative link resolves, to a heading when it has an anchor. The names of `.env` variables it writes
between backticks are real. The parity of the two languages (headings, first line, images that exist) is
test_docs_alignment.py (AC-012) and is not repeated here.

AC-009: the guide shows its five admin screenshots from the folder of its own language and no image of the other one;
together the user guide and the admin guide of a language show all the 8 screenshots of that language. The existence of
the files and the capture command are in test_docs_user_guide.py. Two notes of the owner (review of T-02) are checked by
a stable phrase of each language: the grey boxes that hide the dates in the screenshots of «Documents» and
«Conversations» ("grey boxes", "riquadri grigi"), and the text of the native file chooser, which the browser draws in
its own language ("drawn by the browser", "disegnato dal browser", with "Choose Files" and "No file chosen").

AC-016: the guide holds no internal address, e-mail address, password or key (the screenshots are looked at by eye and
the review is in the pull request; `scripts/secrets_check.py` runs in verify).

The guide has no command block; the screenshots were captured in T-02.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.docs_support import (
    LANGS,
    REPO_ROOT,
    guide_path,
    image_refs,
    lines_outside_code,
    read_text,
    ui_text,
)

ADMIN = "admin.md"
# Texts of the interface that the guide names, as the language file has them (no `{{...}}` in any of them)
REQUIRED_UI_KEYS = (
    # the menu and the pages
    "nav.home",
    "kbs.titleAll",
    "users.menu",
    "users.title",
    "kbAdmin.menu",
    "kbAdmin.title",
    "documents.menu",
    "documents.title",
    "convAdmin.menu",
    "convAdmin.title",
    "settings.menu",
    "settings.title",
    # users
    "users.newUser",
    "users.username",
    "users.initialPassword",
    "users.role",
    "users.roles.user",
    "users.roles.admin",
    "users.create",
    "users.status",
    "users.actions",
    "users.active",
    "users.inactive",
    "users.deactivate",
    "users.reactivate",
    "users.resetPassword",
    "users.newPassword",
    "users.savePassword",
    "users.cancel",
    "users.signInAgain",
    "users.errors.username_taken",
    "users.errors.password_too_short",
    "users.errors.invalid_username",
    "users.errors.cannot_deactivate_self",
    "users.errors.user_not_found",
    "errors.invalid_credentials",
    # knowledge bases
    "kbs.title",
    "kbs.errors.kb_not_found",
    "kbAdmin.newKb",
    "kbAdmin.name",
    "kbAdmin.description",
    "kbAdmin.create",
    "kbAdmin.actions",
    "kbAdmin.rename",
    "kbAdmin.newName",
    "kbAdmin.newDescription",
    "kbAdmin.save",
    "kbAdmin.cancel",
    "kbAdmin.delete",
    "kbAdmin.members",
    "kbAdmin.noMembers",
    "kbAdmin.inactive",
    "kbAdmin.userToAssign",
    "kbAdmin.chooseUser",
    "kbAdmin.assign",
    "kbAdmin.remove",
    "kbAdmin.errors.kb_name_taken",
    "kbAdmin.errors.validation_error",
    # documents
    "documents.kb",
    "documents.chooseKb",
    "documents.files",
    "documents.uploading",
    "documents.empty",
    "documents.name",
    "documents.size",
    "documents.status",
    "documents.reason",
    "documents.date",
    "documents.actions",
    "documents.retry",
    "documents.delete",
    "documents.confirm",
    "documents.cancel",
    "documents.statuses.QUEUED",
    "documents.statuses.PROCESSING",
    "documents.statuses.READY",
    "documents.statuses.FAILED",
    "documents.statuses.UNSUPPORTED",
    "documents.reasons.docling_unreachable",
    "documents.reasons.docling_error",
    "documents.reasons.parse_failed",
    "documents.reasons.embedding_unreachable",
    "documents.reasons.embedding_error",
    "documents.reasons.embedding_invalid_response",
    "documents.reasons.embedding_dimension_mismatch",
    "documents.reasons.verification_failed",
    "documents.reasons.no_text_extracted",
    "documents.reasons.unexpected_error",
    "documents.errors.duplicate_document",
    "documents.errors.unsupported_type",
    "documents.errors.file_too_large",
    "documents.errors.empty_file",
    "documents.errors.not_retryable",
    "documents.errors.original_missing",
    "chat.notices.no_documents",
    "chat.sourceUnavailable",
    # conversations
    "convAdmin.kb",
    "convAdmin.user",
    "convAdmin.allKbs",
    "convAdmin.allUsers",
    "convAdmin.titleColumn",
    "convAdmin.date",
    "convAdmin.count",
    "convAdmin.actions",
    "chat.noConversations",
    "chat.showExcerpt",
    "chat.hideExcerpt",
    "chat.retry",
    "chat.delete",
    "chat.confirm",
    "chat.cancel",
    "chat.errors.conversation_not_found",
    # settings
    "settings.rewrite",
    "settings.rewriteHelp",
    "settings.rerank",
    "settings.rerankHelp",
    "settings.notConfigured",
    "settings.errors.service_not_configured",
)
# Texts with a changing part: the guide writes each one in italics, with `…` for every `{{name}}`
INTERPOLATED_UI_KEYS = (
    "users.created",
    "users.resetTitle",
    "users.passwordReset",
    "users.confirmDeactivate",
    "kbAdmin.renameTitle",
    "kbAdmin.membersTitle",
    "kbAdmin.confirmDelete",
    "kbAdmin.created",
    "kbAdmin.renamed",
    "kbAdmin.deleted",
    "documents.confirmDelete",
    "chat.confirmDelete",
    "chat.searchQuery",
)
ADMIN_SCREENSHOTS = (
    "admin-users.png",
    "admin-kbs.png",
    "admin-documents.png",
    "admin-conversations.png",
    "admin-settings.png",
)
USER_SCREENSHOTS = ("login.png", "kb-list.png", "chat.png")
ALL_SCREENSHOTS = USER_SCREENSHOTS + ADMIN_SCREENSHOTS
# The pages that hold the technical details and the guides next to this one
REQUIRED_LINKS = (
    "installation.md",
    "user.md",
    "../../system/fondazioni.md",
    "../../system/ingestion.md",
    "../../system/chat.md",
)
# A stable phrase of each language for the two notes of the owner, and what the file-chooser note must show
GREY_BOX_PHRASE = {"en": "grey boxes", "it": "riquadri grigi"}
BROWSER_PHRASE = {"en": "drawn by the browser", "it": "disegnato dal browser"}
FILE_CHOOSER_TEXTS = ("Choose Files", "No file chosen")

ITALICS = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+)(?<!\*)\*(?!\*)")
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
IMAGE_WITH_ALT = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
HEADING_ANY = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*\S)[ \t]*#*[ \t]*$")
ENV_NAME_IN_CODE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`")
# What no guide may hold (REQ-102): internal addresses, mail addresses, passwords, keys
SENSITIVE = {
    "an IPv4 address": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    "localhost": re.compile(r"(?i)\blocalhost\b"),
    "an internal host name": re.compile(r"(?i)\b[\w-]+\.(?:local|internal|lan|corp|intranet)\b"),
    "an e-mail address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "a password assignment": re.compile(r"(?i)\b\w*PASSWORD\w*\s*[=:]\s*\S{6,}"),
    "a private key or a token": re.compile(
        r"(?i)BEGIN [A-Z ]*PRIVATE KEY|\bsk-[A-Za-z0-9]{10,}|\bgh[pousr]_[A-Za-z0-9]{10,}|\bAKIA[0-9A-Z]{12,}"
    ),
}


def label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def other_language(lang: str) -> str:
    return next(other for other in LANGS if other != lang)


def guide_text(lang: str, root: Path = REPO_ROOT, name: str = ADMIN) -> str:
    path = guide_path(lang, name, root)
    assert path.is_file(), f"{label(path, root)} does not exist"
    return read_text(path)


def i18n_file(lang: str, root: Path = REPO_ROOT) -> Path:
    return root / "frontend" / "src" / "i18n" / f"{lang}.json"


def quoted(value: str) -> str:
    """How the guide writes a text of the interface: between «», so that «Retry» is not found in a longer message."""
    return f"«{value}»"


def without_italics(text: str) -> str:
    return ITALICS.sub("", text)


def quoted_texts(text: str) -> list[str]:
    """What the guide puts between «» outside its italics (an interpolated text has its own «» in Italian)."""
    return re.findall(r"«([^»]*)»", without_italics(text))


def strings(node: object) -> list[str]:
    if isinstance(node, dict):
        return [text for value in node.values() for text in strings(value)]
    return [node] if isinstance(node, str) else []


def language_strings(lang: str, root: Path = REPO_ROOT) -> list[str]:
    return strings(json.loads(read_text(i18n_file(lang, root))))


def italic_form(value: str) -> str:
    """A text with changing parts as the guide writes it: in italics, `…` in place of each `{{name}}`."""
    return f"*{PLACEHOLDER.sub('…', value)}*"


def not_interface_texts(lang: str, text: str, root: Path = REPO_ROOT) -> list[str]:
    """The texts between «» that are not, as written, a text of `<lang>.json`: «» is only for what is on screen."""
    known = set(language_strings(lang, root))
    return [found for found in quoted_texts(text) if found not in known]


def stray_italics(lang: str, text: str, root: Path = REPO_ROOT) -> list[str]:
    """The italic spans that are not an interpolated text of `<lang>.json` in its italic form: italics are only for
    the texts that have a changing part."""
    known = {italic_form(value) for value in language_strings(lang, root) if "{{" in value}
    return [match.group(0) for match in ITALICS.finditer(text) if match.group(0) not in known]


def missing_ui_texts(lang: str, text: str, root: Path = REPO_ROOT) -> list[str]:
    """The keys of REQUIRED_UI_KEYS whose text of `<lang>.json` is not in `text` between «», and the keys of
    INTERPOLATED_UI_KEYS whose italic form is not in it."""
    missing = [key for key in REQUIRED_UI_KEYS if quoted(ui_text(lang, key, root)) not in text]
    return missing + [key for key in INTERPOLATED_UI_KEYS if italic_form(ui_text(lang, key, root)) not in text]


def image_problems(lang: str, text: str) -> list[str]:
    """AC-009: each admin screenshot is shown from `../images/<lang>/` with a description, and no image of the other
    language is shown."""
    refs = image_refs(text)
    problems = [
        f"{name} is not shown from ../images/{lang}/"
        for name in ADMIN_SCREENSHOTS
        if f"../images/{lang}/{name}" not in refs
    ]
    other = other_language(lang)
    problems += [f"{ref} is an image of the other language" for ref in refs if f"/images/{other}/" in ref]
    for line in lines_outside_code(text):
        for alt, ref in IMAGE_WITH_ALT.findall(line):
            if not alt.strip():
                problems.append(f"{ref} has no description")
    return problems


def note_problems(lang: str, text: str) -> list[str]:
    """AC-009: the sentence about the grey boxes names the two pages whose screenshots have them, and the sentence
    about the file chooser names the two native texts; each is found by the stable phrase of its language."""
    problems: list[str] = []
    lines = text.split("\n")
    grey = [line for line in lines if GREY_BOX_PHRASE[lang] in line]
    if not grey:
        problems.append(f"no sentence has the phrase '{GREY_BOX_PHRASE[lang]}' (the grey boxes over the dates)")
    for key in ("documents.title", "convAdmin.title"):
        if grey and not any(quoted(ui_text(lang, key)) in line for line in grey):
            problems.append(f"the sentence of the grey boxes does not name {quoted(ui_text(lang, key))}")
    chooser = [line for line in lines if BROWSER_PHRASE[lang] in line]
    if not chooser:
        problems.append(f"no sentence has the phrase '{BROWSER_PHRASE[lang]}' (the text of the file chooser)")
    for native in FILE_CHOOSER_TEXTS:
        if chooser and not any(native in line for line in chooser):
            problems.append(f"the sentence of the file chooser does not show '{native}'")
    return problems


def slugs(path: Path) -> set[str]:
    """The anchors of the headings of a Markdown file as GitHub makes them: lower case, no punctuation but `-` and
    `_`, spaces as `-`; the repeated ones get `-1`, `-2`."""
    found: dict[str, int] = {}
    for line in lines_outside_code(read_text(path)):
        match = HEADING_ANY.match(line)
        if match:
            slug = re.sub(r"[^\w\- ]", "", re.sub(r"[*`]", "", match.group(1)).lower()).replace(" ", "-")
            count = found.get(slug, 0)
            found[slug + (f"-{count}" if count else "")] = 1
            found[slug] = count + 1
    return set(found)


def link_problems(path: Path, root: Path, required: tuple[str, ...] = REQUIRED_LINKS) -> list[str]:
    """AC-006: every relative link of the guide (outside code; the images are checked by test_docs_alignment.py)
    resolves to an existing file, to a heading of it when it has an anchor, and each of the `required` targets is
    reached by one."""
    resolved: set[Path] = set()
    problems: list[str] = []
    for line in lines_outside_code(read_text(path)):
        for destination in LINK.findall(re.sub(r"`[^`]*`", "", line)):
            if URL_SCHEME.match(destination) or destination.startswith(("#", "/")):
                continue
            file_part, _, fragment = destination.partition("#")
            target = Path(os.path.normpath(path.parent / unquote(file_part.split("?")[0])))
            if not target.exists():
                problems.append(f"{label(path, root)}: broken link -> {destination}")
                continue
            resolved.add(target)
            if fragment and target.suffix == ".md" and fragment not in slugs(target):
                problems.append(f"{label(path, root)}: the anchor of {destination} is not a heading of the page")
    for name in required:
        wanted = Path(os.path.normpath(path.parent / name))
        if wanted not in resolved:
            problems.append(f"{label(path, root)}: no working link to {name}")
    return problems


def env_example_names(root: Path = REPO_ROOT) -> set[str]:
    lines = read_text(root / ".env.example").split("\n")
    return {match.group(1) for match in (re.match(r"^\s*([A-Z][A-Z0-9_]*)=", line) for line in lines) if match}


def unknown_variables(text: str, root: Path = REPO_ROOT) -> list[str]:
    """The names in capitals with an underscore that the guide writes between backticks outside code blocks and that
    are not in `.env.example` (a variable of the `.env` file that does not exist)."""
    known = env_example_names(root)
    names = [name for line in lines_outside_code(text) for name in ENV_NAME_IN_CODE.findall(line)]
    return [name for name in dict.fromkeys(names) if name not in known]


def sensitive_problems(text: str) -> list[str]:
    """AC-016: what the guide holds that a public guide must not (REQ-102)."""
    return [f"the guide holds {what}" for what, pattern in SENSITIVE.items() if pattern.search(text)]


def screenshots_not_shown(lang: str, texts: list[str]) -> list[str]:
    """AC-009: the screenshots of ALL_SCREENSHOTS that none of the guides of `lang` (user and admin) shows from the
    folder of its own language."""
    cited = [ref for text in texts for ref in image_refs(text)]
    return [name for name in ALL_SCREENSHOTS if f"../images/{lang}/{name}" not in cited]


def all_problems(lang: str, root: Path = REPO_ROOT) -> list[str]:
    """Every check on the admin guide of `lang`; empty when the guide has what AC-006, AC-009 and AC-016 ask for."""
    path = guide_path(lang, ADMIN, root)
    if not path.is_file():
        return [f"{label(path, root)} does not exist"]
    text = read_text(path)
    return [
        *[f"missing: {key}" for key in missing_ui_texts(lang, text, root)],
        *[f"not an interface text: {found}" for found in not_interface_texts(lang, text, root)],
        *[f"italics that are not an interface text: {found}" for found in stray_italics(lang, text, root)],
        *image_problems(lang, text),
        *note_problems(lang, text),
        *link_problems(path, root),
        *[f"not a variable of .env.example: {name}" for name in unknown_variables(text, root)],
        *sensitive_problems(text),
    ]


# Trees under tmp_path ---------------------------------------------------------------------------------

ANCHOR = {"en": "rewriting-and-reranker", "it": "riscrittura-e-reranker"}
ANCHOR_HEADING = {"en": "Rewriting and reranker", "it": "Riscrittura e reranker"}
SYSTEM_PAGES = ("fondazioni.md", "ingestion.md", "chat.md")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def i18n_tree(root: Path) -> Path:
    """A tree with the real language files and a small `.env.example`, for the checks that read them from a root."""
    for lang in LANGS:
        target = i18n_file(lang, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(i18n_file(lang), target)
    write(root / ".env.example", "MAX_UPLOAD_SIZE_MB=20\nREWRITE_BASE_URL=\n")
    return root


def complete_text(lang: str, root: Path = REPO_ROOT) -> str:
    """A guide made of nothing but everything the checks ask for, with the real texts of the interface."""
    parts = [quoted(ui_text(lang, key, root)) for key in REQUIRED_UI_KEYS]
    parts += [italic_form(ui_text(lang, key, root)) for key in INTERPOLATED_UI_KEYS]
    parts += [f"![{name}](../images/{lang}/{name})" for name in ADMIN_SCREENSHOTS]
    parts += [
        " ".join(
            [GREY_BOX_PHRASE[lang], quoted(ui_text(lang, "documents.title")), quoted(ui_text(lang, "convAdmin.title"))]
        ),
        f"{BROWSER_PHRASE[lang]} {' '.join(FILE_CHOOSER_TEXTS)}",
        "[installation](installation.md#" + ANCHOR[lang] + ") [user](user.md)",
        " ".join(f"[{page}](../../system/{page})" for page in SYSTEM_PAGES),
        "`MAX_UPLOAD_SIZE_MB` and `REWRITE_BASE_URL`",
    ]
    return "\n\n".join(parts) + "\n"


def build_tree(root: Path) -> Path:
    """A small repository with the sources the checks read and the guide in both languages, all complete."""
    i18n_tree(root)
    for lang in LANGS:
        write(guide_path(lang, ADMIN, root), f"# Guide {lang}\n\n" + complete_text(lang, root))
        write(guide_path(lang, "user.md", root), "# User\n")
        write(guide_path(lang, "installation.md", root), f"# Installation\n\n### {ANCHOR_HEADING[lang]}\n")
    for page in SYSTEM_PAGES:
        write(root / "docs" / "system" / page, "# Page\n")
    return root


def assert_no_problems(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# AC-006: the guide of each language ---------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_guide_exists_in_both_languages(lang: str) -> None:
    path = guide_path(lang, ADMIN)

    assert path.is_file(), f"{label(path, REPO_ROOT)} does not exist"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_guide_names_the_interface_texts_exactly_as_the_language_file_has_them(lang: str) -> None:
    assert missing_ui_texts(lang, guide_text(lang)) == [], f"missing from the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_everything_between_guillemets_is_a_text_of_the_interface_as_the_language_file_has_it(
    lang: str,
) -> None:
    assert not_interface_texts(lang, guide_text(lang)) == [], f"not interface texts, in the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_italics_of_the_guide_are_the_interface_texts_with_a_changing_part(lang: str) -> None:
    assert stray_italics(lang, guide_text(lang)) == [], f"italics that are not interface texts, in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_guide_names_the_five_admin_pages_of_the_menu_in_its_language(lang: str) -> None:
    text = guide_text(lang)

    for key in ("users.menu", "kbAdmin.menu", "documents.menu", "convAdmin.menu", "settings.menu"):
        assert quoted(ui_text(lang, key)) in text, f"{key} is not named in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_guide_links_the_installation_and_user_guides_and_the_three_area_pages(lang: str) -> None:
    guide_text(lang)

    assert_no_problems(link_problems(guide_path(lang, ADMIN), REPO_ROOT))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_variables_the_guide_names_are_variables_of_the_env_example(lang: str) -> None:
    assert unknown_variables(guide_text(lang)) == []


@pytest.mark.parametrize("lang", LANGS)
def test_AC_006_the_guide_does_not_name_sources_as_a_title_that_is_not_on_screen(lang: str) -> None:
    # the list of sources is only labelled «Sources» for screen readers: the screen shows the "[n] name" line
    assert quoted(ui_text(lang, "chat.sources")) not in guide_text(lang)


def test_AC_006_the_required_keys_exist_in_both_languages_with_and_without_a_changing_part() -> None:
    for lang in LANGS:
        for key in REQUIRED_UI_KEYS:
            assert "{{" not in ui_text(lang, key), f"{key} in {lang}.json has an interpolation: it is INTERPOLATED"
        for key in INTERPOLATED_UI_KEYS:
            assert "{{" in ui_text(lang, key), f"{key} in {lang}.json has no interpolation: it is REQUIRED"
    assert len(set(REQUIRED_UI_KEYS)) == len(REQUIRED_UI_KEYS)
    assert not set(REQUIRED_UI_KEYS) & set(INTERPOLATED_UI_KEYS)


def test_AC_006_the_statuses_the_guide_names_are_the_ones_the_interface_shows_in_each_language() -> None:
    assert [ui_text("en", f"documents.statuses.{status}") for status in ("QUEUED", "PROCESSING", "READY")] == [
        "Queued",
        "Processing",
        "Ready",
    ]
    assert [ui_text("en", f"documents.statuses.{status}") for status in ("FAILED", "UNSUPPORTED")] == [
        "Failed",
        "Unsupported",
    ]
    assert [ui_text("it", f"documents.statuses.{status}") for status in ("QUEUED", "READY", "UNSUPPORTED")] == [
        "In coda",
        "Pronto",
        "Non supportato",
    ]


# AC-006: the checks can fail ------------------------------------------------------------------------------


def test_AC_006_a_complete_text_passes_the_content_checks() -> None:
    for lang in LANGS:
        text = complete_text(lang)

        assert missing_ui_texts(lang, text) == []
        assert not_interface_texts(lang, text) == []
        assert stray_italics(lang, text) == []


def test_AC_006_an_empty_guide_misses_every_text_and_each_key_is_named() -> None:
    for lang in LANGS:
        assert missing_ui_texts(lang, "") == [*REQUIRED_UI_KEYS, *INTERPOLATED_UI_KEYS]


def test_AC_006_a_text_that_differs_by_one_character_or_a_forgotten_one_is_reported() -> None:
    text = complete_text("it")
    ready = ui_text("it", "documents.statuses.READY")
    failure = ui_text("it", "documents.errors.original_missing")

    def same_text(key: str) -> list[str]:
        value = ui_text("it", key)
        return [other for other in REQUIRED_UI_KEYS if ui_text("it", other) == value]

    assert missing_ui_texts("it", text.replace(quoted(ready), quoted(ready.lower()))) == same_text(
        "documents.statuses.READY"
    )
    assert missing_ui_texts("it", text.replace(quoted(ready), ready)) == same_text("documents.statuses.READY")
    assert missing_ui_texts("it", text.replace(quoted(failure), quoted(failure.removesuffix(".")))) == [
        "documents.errors.original_missing"
    ]
    assert missing_ui_texts("it", text.replace("«Riprova»", "«Riprova!»")) == same_text("documents.retry")


@pytest.mark.parametrize(
    "key",
    [
        "documents.statuses.QUEUED",
        "documents.statuses.FAILED",
        "documents.retry",
        "settings.rewrite",
        "settings.rerank",
        "settings.notConfigured",
        "convAdmin.menu",
    ],
)
def test_AC_006_the_texts_that_name_the_statuses_the_retry_and_the_switches_are_checked_in_both_languages(
    key: str,
) -> None:
    assert key in REQUIRED_UI_KEYS
    for lang in LANGS:
        value = ui_text(lang, key)
        same = [other for other in REQUIRED_UI_KEYS if ui_text(lang, other) == value]
        changed = complete_text(lang).replace(quoted(value), quoted(value + "!"))

        assert key in same
        assert missing_ui_texts(lang, complete_text(lang)) == []
        assert missing_ui_texts(lang, changed) == same


def test_AC_006_the_english_text_is_not_enough_for_the_italian_guide() -> None:
    assert "documents.retry" in missing_ui_texts("it", complete_text("en"))
    assert "documents.retry" in missing_ui_texts("en", complete_text("it"))
    assert "settings.rerankHelp" in missing_ui_texts("it", complete_text("en"))


def test_AC_006_a_text_between_guillemets_that_is_not_in_the_language_file_is_reported() -> None:
    text = complete_text("en")

    assert quoted_texts("a «Retry» and a «forgot my password»") == ["Retry", "forgot my password"]
    assert not_interface_texts("en", text) == []
    assert not_interface_texts("en", text + "\n«forgot my password» and «Search: …»") == [
        "forgot my password",
        "Search: …",
    ]
    assert not_interface_texts("en", text.replace("«Retry»", "«retry»")) == ["retry", "retry"]
    assert not_interface_texts("it", complete_text("it") + " «Messaggi d'errore»") == ["Messaggi d'errore"]
    assert not_interface_texts("en", text + " «Choose Files»") == ["Choose Files"]


def test_AC_006_an_interpolated_text_is_read_in_its_italic_form_and_its_own_guillemets_are_not() -> None:
    delete_kb = ui_text("it", "kbAdmin.confirmDelete")
    text = complete_text("it")

    assert (
        italic_form(delete_kb)
        == "*Eliminare la knowledge base …? Anche le assegnazioni dei suoi utenti verranno rimosse.*"
    )
    assert italic_form(ui_text("en", "chat.confirmDelete")) == (
        '*Delete the conversation "…"? Its messages will be removed too.*'
    )
    assert missing_ui_texts("it", text) == []
    assert missing_ui_texts("it", text.replace(italic_form(delete_kb), delete_kb)) == ["kbAdmin.confirmDelete"]
    assert missing_ui_texts("it", text.replace("*Utente … creato.*", "*Utente creato.*")) == ["users.created"]
    assert missing_ui_texts("en", complete_text("en").replace("*Search: …*", "*Search…*")) == ["chat.searchQuery"]


def test_AC_006_an_italic_text_that_is_not_an_interface_text_is_reported_and_bold_is_not_read() -> None:
    text = complete_text("en")

    assert stray_italics("en", text) == []
    assert stray_italics("en", text + "\nsome *emphasis* here") == ["*emphasis*"]
    assert stray_italics("en", text + "\n*User … created!*") == ["*User … created!*"]
    assert stray_italics("en", text + "\nsome **bold** here") == []


def test_AC_006_the_texts_are_checked_against_the_language_file_of_the_root_it_is_given(tmp_path: Path) -> None:
    root = i18n_tree(tmp_path)
    path = i18n_file("it", root)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"retry": "Riprova"', '"retry": "Ripeti"'), encoding="utf-8"
    )

    assert missing_ui_texts("it", complete_text("it"), root) == ["documents.retry", "chat.retry"]
    assert missing_ui_texts("it", complete_text("it", root), root) == []


def test_AC_006_a_complete_tree_passes_every_check_in_both_languages(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    for lang in LANGS:
        assert_no_problems(all_problems(lang, root))


def test_AC_006_a_missing_guide_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    guide_path("it", ADMIN, root).unlink()

    assert all_problems("it", root) == ["docs/guide/it/admin.md does not exist"]
    assert all_problems("en", root) == []


def test_AC_006_a_forgotten_text_a_stray_guillemet_text_and_a_stray_italic_are_named_by_the_whole_check(
    tmp_path: Path,
) -> None:
    root = build_tree(tmp_path)
    path = guide_path("en", ADMIN, root)
    text = read_text(path)
    write(path, text.replace(quoted("Reranker"), "Reranker") + "\n«forgot my password» and *emphasis*\n")

    assert all_problems("en", root) == [
        "missing: settings.rerank",
        "not an interface text: forgot my password",
        "italics that are not an interface text: *emphasis*",
    ]


# AC-006: links and variables --------------------------------------------------------------------------


def test_AC_006_a_missing_required_link_or_a_broken_one_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    path = guide_path("en", ADMIN, root)
    original = read_text(path)

    write(path, original.replace("[user](user.md)", ""))
    assert link_problems(path, root) == ["docs/guide/en/admin.md: no working link to user.md"]

    write(path, original.replace("(../../system/ingestion.md)", "(../../system/ingestion-gone.md)"))
    assert link_problems(path, root) == [
        "docs/guide/en/admin.md: broken link -> ../../system/ingestion-gone.md",
        "docs/guide/en/admin.md: no working link to ../../system/ingestion.md",
    ]

    write(path, original + "\n[the README](../../../README.md)\n")
    assert link_problems(path, root) == ["docs/guide/en/admin.md: broken link -> ../../../README.md"]

    write(path, original)
    assert link_problems(path, root) == []


def test_AC_006_an_anchor_that_is_not_a_heading_of_the_page_is_named_and_a_code_link_is_not_read(
    tmp_path: Path,
) -> None:
    root = build_tree(tmp_path)
    path = guide_path("it", ADMIN, root)
    original = read_text(path)

    write(path, original.replace("#" + ANCHOR["it"], "#riscrittura-e-rerank"))
    assert link_problems(path, root) == [
        "docs/guide/it/admin.md: the anchor of installation.md#riscrittura-e-rerank is not a heading of the page"
    ]

    write(path, original + "\n`[gone](gone.md)` and\n\n```\n[gone](gone.md)\n```\n")
    assert link_problems(path, root) == []
    assert slugs(guide_path("it", "installation.md", root)) == {"installation", ANCHOR["it"]}


def test_AC_006_the_slug_of_a_heading_is_made_as_github_makes_it(tmp_path: Path) -> None:
    page = write(
        tmp_path / "page.md", "# Title\n\n## The variables of the .env file\n\n### Il reranker su CPU\n\n## A\n\n## A\n"
    )

    assert slugs(page) == {"title", "the-variables-of-the-env-file", "il-reranker-su-cpu", "a", "a-1"}


def test_AC_006_a_variable_that_is_not_in_the_env_example_is_named(tmp_path: Path) -> None:
    root = i18n_tree(tmp_path)

    assert unknown_variables("`MAX_UPLOAD_SIZE_MB` and `REWRITE_BASE_URL`", root) == []
    assert unknown_variables("`MAX_UPLOAD_MB`, `RERANK_BASE_URL` and `MAX_UPLOAD_MB` again", root) == [
        "MAX_UPLOAD_MB",
        "RERANK_BASE_URL",
    ]
    assert unknown_variables("`GET`, `HTTP`, `UTF-8` and `PDF` are not variables", root) == []
    assert unknown_variables("```\n`NO_SUCH_VARIABLE`\n```", root) == []


# AC-009: the screenshots ------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", ADMIN_SCREENSHOTS)
def test_AC_009_the_guide_shows_each_admin_screenshot_from_the_folder_of_its_own_language(lang: str, name: str) -> None:
    assert f"../images/{lang}/{name}" in image_refs(guide_text(lang)), f"{name} is not shown by the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_009_the_guide_shows_no_image_of_the_other_language_and_describes_each_image(lang: str) -> None:
    assert image_problems(lang, guide_text(lang)) == []
    assert not [ref for ref in image_refs(guide_text(lang)) if f"/images/{other_language(lang)}/" in ref]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_009_the_user_and_the_admin_guide_of_a_language_show_all_the_eight_screenshots_of_that_language(
    lang: str,
) -> None:
    texts = [guide_text(lang, name=name) for name in ("user.md", ADMIN)]

    assert screenshots_not_shown(lang, texts) == []


def test_AC_009_an_image_of_the_wrong_language_a_missing_citation_or_a_link_that_is_not_an_image_is_reported() -> None:
    text = complete_text("it")

    assert image_problems("it", text) == []
    assert image_problems("it", text.replace("../images/it/admin-kbs.png", "../images/en/admin-kbs.png")) == [
        "admin-kbs.png is not shown from ../images/it/",
        "../images/en/admin-kbs.png is an image of the other language",
    ]
    assert image_problems("it", text.replace("![admin-users.png](../images/it/admin-users.png)", "")) == [
        "admin-users.png is not shown from ../images/it/"
    ]
    assert image_problems("it", text.replace("![admin-settings.png](", "[admin-settings.png](")) == [
        "admin-settings.png is not shown from ../images/it/"
    ]
    assert image_problems("it", text + "\n![](../images/it/admin-users.png)") == [
        "../images/it/admin-users.png has no description"
    ]
    assert image_problems("en", text) == [f"{name} is not shown from ../images/en/" for name in ADMIN_SCREENSHOTS] + [
        f"../images/it/{name} is an image of the other language" for name in ADMIN_SCREENSHOTS
    ]
    assert image_problems("it", "") == [f"{name} is not shown from ../images/it/" for name in ADMIN_SCREENSHOTS]
    assert image_problems("it", text.replace("../images/it/admin-kbs.png", "../images/it/admin-kbs.jpg")) == [
        "admin-kbs.png is not shown from ../images/it/"
    ]


def test_AC_009_a_screenshot_of_the_user_guide_missing_from_both_guides_is_reported() -> None:
    texts = [complete_text("en")]

    assert screenshots_not_shown("en", texts) == list(USER_SCREENSHOTS)
    assert screenshots_not_shown("en", [*texts, "![login](../images/en/login.png)"]) == ["kb-list.png", "chat.png"]
    assert screenshots_not_shown("it", [*texts, "![login](../images/en/login.png)"]) == list(ALL_SCREENSHOTS)
    assert screenshots_not_shown("en", []) == list(ALL_SCREENSHOTS)


@pytest.mark.parametrize("lang", LANGS)
def test_AC_009_the_guide_says_that_grey_boxes_cover_the_dates_and_that_the_browser_draws_the_file_chooser(
    lang: str,
) -> None:
    assert note_problems(lang, guide_text(lang)) == []


def test_AC_009_the_two_notes_are_found_by_their_phrase_and_reported_when_missing_or_incomplete() -> None:
    text = complete_text("it")
    grey = f"{GREY_BOX_PHRASE['it']} «Documenti» «Conversazioni»"

    assert note_problems("it", text) == []
    assert note_problems("it", text.replace(grey, "")) == [
        "no sentence has the phrase 'riquadri grigi' (the grey boxes over the dates)"
    ]
    assert note_problems("it", text.replace(grey, grey.replace(" «Conversazioni»", ""))) == [
        "the sentence of the grey boxes does not name «Conversazioni»"
    ]
    assert note_problems("it", text.replace(" No file chosen", "")) == [
        "the sentence of the file chooser does not show 'No file chosen'"
    ]
    assert note_problems("it", text.replace("disegnato dal browser", "")) == [
        "no sentence has the phrase 'disegnato dal browser' (the text of the file chooser)"
    ]
    assert note_problems("en", text) != []
    assert note_problems("en", complete_text("en")) == []


def test_AC_009_the_notes_of_a_tree_are_reported_by_the_whole_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    path = guide_path("en", ADMIN, root)
    write(path, read_text(path).replace("grey boxes", "boxes"))

    assert all_problems("en", root) == ["no sentence has the phrase 'grey boxes' (the grey boxes over the dates)"]


# AC-016: nothing sensitive in the guide ---------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_016_the_guide_holds_no_internal_address_mail_address_password_or_key(lang: str) -> None:
    assert sensitive_problems(guide_text(lang)) == []


def test_AC_016_an_address_a_mail_a_password_or_a_key_in_the_guide_is_reported() -> None:
    text = complete_text("en")

    assert sensitive_problems(text) == []
    assert sensitive_problems(text + "\nopen http://192.168.1.20:8080/") == ["the guide holds an IPv4 address"]
    assert sensitive_problems(text + "\nopen localhost") == ["the guide holds localhost"]
    assert sensitive_problems(text + "\nhttp://ollama.example.internal:11434") == [
        "the guide holds an internal host name"
    ]
    assert sensitive_problems(text + "\nwrite to mario.rossi@example.com") == ["the guide holds an e-mail address"]
    assert sensitive_problems(text + "\nADMIN_PASSWORD=Sup3rS3cretValue") == ["the guide holds a password assignment"]
    assert sensitive_problems(text + "\nsk-abcdefghijklmnop0123") == ["the guide holds a private key or a token"]
    assert sensitive_problems(text + "\nthe variable `ADMIN_PASSWORD` is set in the .env") == []


def test_AC_016_a_tree_with_a_password_in_the_guide_is_reported_by_the_whole_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    path = guide_path("it", ADMIN, root)
    write(path, read_text(path) + "\nPassword: hunter2hunter2\n")

    assert all_problems("it", root) == ["the guide holds a password assignment"]
