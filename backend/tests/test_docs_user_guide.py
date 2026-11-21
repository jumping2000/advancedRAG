# SPEC-2026-11-20-fase-5-documentazione
"""The user guide, in both languages, and its screenshots: AC-005 (REQ-004) and AC-009 (REQ-008).

AC-005: the guide names the texts of the interface exactly as `frontend/src/i18n/<lang>.json` has them: sign-in,
language, the list of KBs, the chat (question, sources and excerpts, «Ricerca: …», «Riprova»), the conversations and
the error messages (REQUIRED_UI_KEYS). A text with an interpolation (`Ricerca: {{query}}`) is checked by what comes
before the `{{`. Each text stands between «» (so that «Riprova» is not found inside a longer message). The parity of
the two languages (headings, first line, images that exist) is test_docs_alignment.py (AC-012).

AC-009: the guide cites its three screenshots from the folder of its own language, the 8 screenshots exist in both
folders with the same names and are PNG files, and the command that captures them (`pytest -m screenshots`) is not part
of `scripts/verify.py` nor of the default run of pytest: a real nested `pytest --collect-only` shows it. The capture
itself is `backend/tests/e2e/test_screenshots.py`, a tool that needs Docker and Chromium.

The guide has no command block to run; the screenshots are checked by eye (AC-016), the review is in the pull request.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.docs_support import LANGS, REPO_ROOT, guide_path, image_refs, read_text, ui_text

USER = "user.md"
# Texts of the interface that the guide names, as the language file has them (no `{{...}}` in any of them)
REQUIRED_UI_KEYS = (
    "login.title",
    "login.username",
    "login.password",
    "login.submit",
    "nav.home",
    "nav.logout",
    "language.label",
    "language.it",
    "language.en",
    "kbs.title",
    "kbs.titleAll",
    "kbs.empty",
    "kbs.back",
    "kbs.errors.kb_not_found",
    "chat.conversations",
    "chat.loading",
    "chat.you",
    "chat.assistant",
    "chat.new",
    "chat.noConversations",
    "chat.empty",
    "chat.rename",
    "chat.renameLabel",
    "chat.save",
    "chat.cancel",
    "chat.delete",
    "chat.confirm",
    "chat.question",
    "chat.send",
    "chat.generating",
    "chat.sources",
    "chat.showExcerpt",
    "chat.hideExcerpt",
    "chat.sourceUnavailable",
    "chat.retry",
    "chat.failures.embedding_error",
    "chat.failures.generation_error",
    "chat.failures.generation_timeout",
    "chat.failures.internal_error",
    "chat.failures.interrupted",
    "chat.notices.no_documents",
    "chat.errors.generation_not_configured",
    "chat.errors.answer_in_progress",
    "chat.errors.conversation_not_found",
    "chat.errors.not_retryable",
    "chat.errors.validation_error",
    "settings.menu",
    "settings.rewrite",
    "settings.rerank",
    "documents.menu",
    "convAdmin.menu",
    "errors.invalid_credentials",
    "errors.not_authenticated",
    "errors.validation_error",
    "errors.network_error",
    "errors.unknown",
)
# Texts with an interpolation: the guide must hold what comes before the first `{{` («Ricerca:», the start of the
# confirmation of a deletion, the start of the greeting of the home page)
PREFIX_UI_KEYS = ("chat.searchQuery", "home.signedInAs", "chat.confirmDelete")
USER_SCREENSHOTS = ("login.png", "kb-list.png", "chat.png")
SCREENSHOTS = (
    "login.png",
    "kb-list.png",
    "chat.png",
    "admin-users.png",
    "admin-kbs.png",
    "admin-documents.png",
    "admin-conversations.png",
    "admin-settings.png",
)
SYSTEM_PAGES = ("fondazioni.md", "chat.md")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 67  # a PNG with its signature, IHDR, one IDAT and IEND cannot be smaller
BACKEND = REPO_ROOT / "backend"


def guide_text(lang: str, root: Path = REPO_ROOT) -> str:
    path = guide_path(lang, USER, root)
    assert path.is_file(), f"{path.relative_to(root).as_posix()} does not exist"
    return read_text(path)


def quoted(value: str) -> str:
    """How the guide writes a text of the interface: between «», so that «Riprova» is not found in a longer message."""
    return f"«{value}»"


def quoted_texts(text: str) -> list[str]:
    """What the guide puts between «» outside its italics. A text of the interface that has a changing part (a user
    name, a question, a title) is written in italics with `…` in place of that part, so its own «» (the Italian
    confirmation of a deletion has some) are not read."""
    return re.findall(r"«([^»]*)»", re.sub(r"\*[^*\n]+\*", "", text))


def not_interface_texts(lang: str, text: str, root: Path = REPO_ROOT) -> list[str]:
    """The texts between «» that are not, as written, a text of `<lang>.json`: «» is only for what is on screen."""
    path = root / "frontend" / "src" / "i18n" / f"{lang}.json"
    known = set(strings(json.loads(read_text(path))))
    return [quoted for quoted in quoted_texts(text) if quoted not in known]


def strings(node: object) -> list[str]:
    if isinstance(node, dict):
        return [text for value in node.values() for text in strings(value)]
    return [node] if isinstance(node, str) else []


def interpolation_prefix(value: str) -> str:
    """What a text holds before its first `{{name}}`, without the spaces around it."""
    return value.split("{{")[0].strip()


def missing_ui_texts(lang: str, text: str, root: Path = REPO_ROOT) -> list[str]:
    """The keys of REQUIRED_UI_KEYS whose text of `<lang>.json` is not in `text` between «», and the keys of
    PREFIX_UI_KEYS whose beginning is not in it."""
    missing = [key for key in REQUIRED_UI_KEYS if quoted(ui_text(lang, key, root)) not in text]
    return missing + [key for key in PREFIX_UI_KEYS if interpolation_prefix(ui_text(lang, key, root)) not in text]


def uncited_screenshots(lang: str, text: str) -> list[str]:
    """The user screenshots that the guide does not show from the folder `../images/<lang>/` of its own language."""
    cited = image_refs(text)
    return [name for name in USER_SCREENSHOTS if f"../images/{lang}/{name}" not in cited]


def images_dir(lang: str, root: Path = REPO_ROOT) -> Path:
    return root / "docs" / "guide" / "images" / lang


def image_problems(root: Path = REPO_ROOT) -> list[str]:
    """What is wrong with the screenshots, each problem naming its file: a name of SCREENSHOTS missing from a language
    folder, a file that is not a PNG (signature) or is empty, a file that only one language has."""
    problems: list[str] = []
    names: dict[str, set[str]] = {}
    for lang in LANGS:
        folder = images_dir(lang, root)
        names[lang] = {path.name for path in folder.iterdir() if path.is_file()} if folder.is_dir() else set()
        for name in SCREENSHOTS:
            path = folder / name
            if not path.is_file():
                problems.append(f"docs/guide/images/{lang}/{name} does not exist")
                continue
            content = path.read_bytes()
            if not content.startswith(PNG_SIGNATURE):
                problems.append(f"docs/guide/images/{lang}/{name} is not a PNG file")
            elif len(content) < MIN_PNG_BYTES:
                problems.append(f"docs/guide/images/{lang}/{name} is empty")
    first, second = LANGS
    for name in sorted(names[first] ^ names[second]):
        only = first if name in names[first] else second
        problems.append(f"docs/guide/images/{only}/{name} has no twin in the other language")
    return problems


def addopts_excludes_screenshots(pyproject: str) -> bool:
    """True when the `screenshots` marker is declared and the default `-m` of pytest leaves out the tests with it."""
    options = tomllib.loads(pyproject)["tool"]["pytest"]["ini_options"]
    declared = any(str(marker).startswith("screenshots:") for marker in options.get("markers", []))
    return declared and "not screenshots" in str(options.get("addopts", ""))


def verify_mentions_screenshots(verify_script: str) -> bool:
    return "screenshots" in verify_script


def collected(*arguments: str) -> tuple[list[str], str]:
    """The ids that a nested `pytest --collect-only` of the capture test collects with the repository configuration
    (the addopts of backend/pyproject.toml), and its whole output."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *arguments,
            "tests/e2e/test_screenshots.py",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    return [line for line in output.splitlines() if "test_screenshots.py::" in line], output


def complete_text(lang: str, root: Path = REPO_ROOT) -> str:
    """A guide made of nothing but everything the checks ask for."""
    parts = [quoted(ui_text(lang, key, root)) for key in REQUIRED_UI_KEYS]
    parts += [f"*{re.sub(r'{{[^}]*}}', '…', ui_text(lang, key, root))}*" for key in PREFIX_UI_KEYS]
    parts += [f"![{name}](../images/{lang}/{name})" for name in USER_SCREENSHOTS]
    return "\n\n".join(parts)


def write_png(path: Path, content: bytes = PNG_SIGNATURE + b"\x00" * 64) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def image_tree(root: Path) -> Path:
    for lang in LANGS:
        for name in SCREENSHOTS:
            write_png(images_dir(lang, root) / name)
    return root


def i18n_tree(root: Path) -> Path:
    """A tree with the real language files, for the checks that read them from a root."""
    for lang in LANGS:
        target = root / "frontend" / "src" / "i18n" / f"{lang}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / "frontend" / "src" / "i18n" / f"{lang}.json", target)
    return root


# AC-005: the guide of each language


@pytest.mark.parametrize("lang", LANGS)
def test_AC_005_the_guide_names_the_interface_texts_exactly_as_the_language_file_has_them(lang: str) -> None:
    assert missing_ui_texts(lang, guide_text(lang)) == [], f"missing from the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_005_the_guide_links_the_installation_guide_and_the_area_pages(lang: str) -> None:
    text = guide_text(lang)

    assert "](installation.md)" in text
    assert [page for page in SYSTEM_PAGES if f"](../../system/{page})" not in text] == []


@pytest.mark.parametrize("lang", LANGS)
def test_AC_005_the_required_keys_exist_in_both_languages_and_have_no_interpolation(lang: str) -> None:
    for key in REQUIRED_UI_KEYS:
        assert "{{" not in ui_text(lang, key), (
            f"{key} in {lang}.json has an interpolation: it belongs to PREFIX_UI_KEYS"
        )
    for key in PREFIX_UI_KEYS:
        assert "{{" in ui_text(lang, key), f"{key} in {lang}.json has no interpolation: it belongs to REQUIRED_UI_KEYS"
        assert interpolation_prefix(ui_text(lang, key)) != ""


@pytest.mark.parametrize("lang", LANGS)
def test_AC_005_everything_between_guillemets_is_a_text_of_the_interface_as_the_language_file_has_it(
    lang: str,
) -> None:
    assert not_interface_texts(lang, guide_text(lang)) == [], f"not interface texts, in the guide in '{lang}'"


def test_AC_005_a_guillemet_text_that_is_not_on_screen_is_reported_and_an_italic_one_is_not() -> None:
    text = complete_text("en")

    assert not_interface_texts("en", text) == []
    assert quoted_texts("a «Sign in» and a «forgot my password»") == ["Sign in", "forgot my password"]
    assert not_interface_texts("en", text + "\n«forgot my password» and «Search: …»") == [
        "forgot my password",
        "Search: …",
    ]
    assert not_interface_texts("en", text + "\n*Search: …* and *Delete the conversation «…»?*") == []
    assert not_interface_texts("it", complete_text("it") + " «Messaggi d'errore»") == ["Messaggi d'errore"]
    assert not_interface_texts("en", text.replace("«Retry»", "«retry»")) == ["retry"]


@pytest.mark.parametrize(
    "key",
    [
        "kbs.titleAll",
        "chat.loading",
        "chat.you",
        "chat.assistant",
        "settings.menu",
        "settings.rewrite",
        "settings.rerank",
        "documents.menu",
        "convAdmin.menu",
    ],
)
def test_AC_005_the_texts_that_the_guides_name_besides_the_chat_are_checked_too(key: str) -> None:
    assert key in REQUIRED_UI_KEYS
    for lang in LANGS:
        value = ui_text(lang, key)
        same = [other for other in REQUIRED_UI_KEYS if ui_text(lang, other) == value]
        changed = complete_text(lang).replace(quoted(value), quoted(value + "!"))

        assert key in same
        assert missing_ui_texts(lang, complete_text(lang)) == []
        assert missing_ui_texts(lang, changed) == same


def test_AC_005_the_search_line_is_checked_by_its_base_text_in_each_language() -> None:
    assert interpolation_prefix(ui_text("it", "chat.searchQuery")) == "Ricerca:"
    assert interpolation_prefix(ui_text("en", "chat.searchQuery")) == "Search:"


def test_AC_005_a_complete_text_passes_the_content_checks() -> None:
    for lang in LANGS:
        assert missing_ui_texts(lang, complete_text(lang)) == []
        assert uncited_screenshots(lang, complete_text(lang)) == []


def test_AC_005_an_empty_guide_misses_every_text_and_each_key_is_named() -> None:
    for lang in LANGS:
        assert missing_ui_texts(lang, "") == [*REQUIRED_UI_KEYS, *PREFIX_UI_KEYS]


def test_AC_005_a_text_that_differs_by_one_character_or_a_forgotten_one_is_reported() -> None:
    text = complete_text("it")
    in_progress = ui_text("it", "chat.errors.answer_in_progress")

    assert missing_ui_texts("it", text.replace("«Riprova»", "«riprova»")) == ["chat.retry"]
    assert missing_ui_texts("it", text.replace("«Riprova»", "Riprova")) == ["chat.retry"]
    assert missing_ui_texts("it", text.replace(quoted(in_progress), quoted(in_progress.removesuffix(".")))) == [
        "chat.errors.answer_in_progress"
    ]
    assert missing_ui_texts("it", text.replace("Ricerca:", "Ricerca")) == ["chat.searchQuery"]
    assert "chat.searchQuery" not in missing_ui_texts("it", text)


def test_AC_005_the_english_text_is_not_enough_for_the_italian_guide() -> None:
    assert missing_ui_texts("it", complete_text("en")) != []
    assert "chat.retry" in missing_ui_texts("it", complete_text("en"))
    assert "chat.retry" in missing_ui_texts("en", complete_text("it"))


def test_AC_005_a_text_is_checked_against_the_language_file_of_the_root_it_is_given(tmp_path: Path) -> None:
    root = i18n_tree(tmp_path)
    path = root / "frontend" / "src" / "i18n" / "it.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"retry": "Riprova"', '"retry": "Ripeti"'), encoding="utf-8"
    )

    assert missing_ui_texts("it", complete_text("it"), root) == ["chat.retry"]
    assert missing_ui_texts("it", complete_text("it", root), root) == []


# AC-009: the screenshots


@pytest.mark.parametrize("lang", LANGS)
def test_AC_009_the_guide_shows_its_three_screenshots_from_the_folder_of_its_language(lang: str) -> None:
    assert uncited_screenshots(lang, guide_text(lang)) == [], f"not shown by the guide in '{lang}'"


def test_AC_009_a_screenshot_of_the_other_language_or_a_link_that_is_not_an_image_is_not_cited() -> None:
    text = complete_text("it")

    assert uncited_screenshots("it", text.replace("../images/it/chat.png", "../images/en/chat.png")) == ["chat.png"]
    assert uncited_screenshots("it", text.replace("![login.png](", "[login.png](")) == ["login.png"]
    assert uncited_screenshots("en", text) == list(USER_SCREENSHOTS)
    assert uncited_screenshots("it", "") == list(USER_SCREENSHOTS)


def test_AC_009_the_eight_screenshots_exist_in_both_languages_with_the_same_names_and_are_png_files() -> None:
    assert image_problems() == []


def test_AC_009_a_tree_with_the_eight_screenshots_in_both_languages_has_no_problem(tmp_path: Path) -> None:
    assert image_problems(image_tree(tmp_path)) == []


def test_AC_009_a_missing_screenshot_is_named(tmp_path: Path) -> None:
    root = image_tree(tmp_path)
    (images_dir("it", root) / "admin-kbs.png").unlink()

    assert image_problems(root) == [
        "docs/guide/images/it/admin-kbs.png does not exist",
        "docs/guide/images/en/admin-kbs.png has no twin in the other language",
    ]


def test_AC_009_a_file_that_is_not_a_png_or_is_empty_is_named(tmp_path: Path) -> None:
    root = image_tree(tmp_path)
    write_png(images_dir("en", root) / "chat.png", b"<html>not an image</html>" + b" " * 64)
    write_png(images_dir("it", root) / "login.png", b"")
    write_png(images_dir("it", root) / "kb-list.png", PNG_SIGNATURE)

    assert image_problems(root) == [
        "docs/guide/images/en/chat.png is not a PNG file",
        "docs/guide/images/it/login.png is not a PNG file",
        "docs/guide/images/it/kb-list.png is empty",
    ]


def test_AC_009_an_extra_screenshot_in_one_language_only_is_named(tmp_path: Path) -> None:
    root = image_tree(tmp_path)
    write_png(images_dir("en", root) / "extra.png")

    assert image_problems(root) == ["docs/guide/images/en/extra.png has no twin in the other language"]


def test_AC_009_no_screenshot_folders_at_all_is_reported_for_every_name(tmp_path: Path) -> None:
    assert len(image_problems(tmp_path)) == len(SCREENSHOTS) * len(LANGS)


# AC-009: the capture command is not part of the verification


def test_AC_009_the_screenshots_marker_is_declared_and_left_out_of_the_default_run() -> None:
    assert addopts_excludes_screenshots(read_text(BACKEND / "pyproject.toml"))


def test_AC_009_a_configuration_that_does_not_exclude_the_marker_or_does_not_declare_it_is_reported() -> None:
    def config(markers: str, addopts: str) -> str:
        return f'[tool.pytest.ini_options]\nmarkers = [{markers}]\naddopts = "{addopts}"\n'

    declared = '"e2e: x", "screenshots: capture the screenshots of the guides"'
    default = "-m 'not e2e and not live'"

    excluded = "-m 'not e2e and not live and not screenshots'"

    assert addopts_excludes_screenshots(config(declared, excluded))
    assert not addopts_excludes_screenshots(config(declared, default))
    assert not addopts_excludes_screenshots(config('"e2e: x"', excluded))
    assert not addopts_excludes_screenshots(config('"e2e: x"', default))


def test_AC_009_verify_does_not_run_the_screenshots() -> None:
    assert not verify_mentions_screenshots(read_text(REPO_ROOT / "scripts" / "verify.py"))
    assert verify_mentions_screenshots('("shots", ["pytest", "-m", "screenshots"], False)')


def test_AC_009_pytest_collects_no_capture_test_by_default_and_collects_them_with_the_marker() -> None:
    by_default, output = collected()
    marked, marked_output = collected("-m", "screenshots")

    assert by_default == [], output
    assert "deselected" in output, output
    assert marked != [], marked_output
