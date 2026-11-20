# SPEC-2026-11-20-fase-5-documentazione
"""The guides stay aligned with the code and with each other: AC-011 (REQ-010) and AC-012 (REQ-011).

The checks are functions of a repository root, so that the tests below run them twice: on the real repository (the
guides that exist must be aligned; the files that later tasks add are not required yet) and on small trees built under
`tmp_path`, where every kind of mistake is made on purpose and must be reported with the name of the file (or of the
variable and the guide). A check that cannot fail would let any guide pass.

Review focus of T-01: names in capitals that are not variables (`GET`, `HTTP`, `E2E_*`, `LIVE_SERVICES`) are not read
as variables, only the first column of the variable table is; a file with Windows line ends or a BOM is read like any
other.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.docs_support import (
    LANGS,
    REPO_ROOT,
    compose_volumes,
    env_example_names,
    first_line_after_title,
    guide_path,
    headings,
    image_refs,
    read_text,
    ui_text,
    variable_table_names,
)

INSTALLATION = "installation.md"
README = {"en": "README.md", "it": "README.it.md"}
URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
LOCAL_ENV = ["POSTGRES_PASSWORD", "HTTP_PORT", "ADMIN_USERNAME", "ADMIN_PASSWORD"]


# The checks -----------------------------------------------------------------------------------------


def label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def other_language(lang: str) -> str:
    return next(other for other in LANGS if other != lang)


def variable_problems(root: Path, lang: str) -> list[str]:
    """AC-011: the variables of .env.example and the names of the first column of the table of the installation guide
    of `lang` are the same set; each problem names the guide and the variable."""
    guide = guide_path(lang, INSTALLATION, root)
    if not guide.is_file():
        return [f"{label(guide, root)}: the installation guide in '{lang}' does not exist"]
    in_env = env_example_names(root)
    in_table = variable_table_names(read_text(guide))
    problems = [
        f"{label(guide, root)}: the variable {name} of .env.example is missing from its variable table"
        for name in in_env
        if name not in in_table
    ]
    problems += [
        f"{label(guide, root)}: the variable table names {name}, which is not in .env.example"
        for name in dict.fromkeys(in_table)
        if name not in in_env
    ]
    return problems


def table_order_problems(root: Path, lang: str) -> list[str]:
    """One row per variable, in the order of .env.example (the format of the plan)."""
    guide = guide_path(lang, INSTALLATION, root)
    if not guide.is_file():
        return [f"{label(guide, root)}: the installation guide in '{lang}' does not exist"]
    in_env = env_example_names(root)
    in_table = variable_table_names(read_text(guide))
    problems = [
        f"{label(guide, root)}: {name} has {count} rows in the variable table, one is expected"
        for name, count in Counter(in_table).items()
        if count > 1
    ]
    known = [name for name in dict.fromkeys(in_table) if name in in_env]
    if known != [name for name in in_env if name in known]:
        problems.append(f"{label(guide, root)}: the rows of the variable table are not in the order of .env.example")
    return problems


def files_of(directory: Path, pattern: str) -> list[str]:
    """The names (relative, with `/`) of the files under `directory` that match `pattern`; an absent directory has
    none, and a dot file (`.gitkeep`) is not a guide or an image."""
    if not directory.is_dir():
        return []
    return sorted(
        path.relative_to(directory).as_posix()
        for path in directory.glob(pattern)
        if path.is_file() and not path.name.startswith(".")
    )


def missing_twin_problems(root: Path) -> list[str]:
    """AC-012: a guide (`docs/guide/<lang>/*.md`), an image (`docs/guide/images/<lang>/**`) or a README that exists in
    one language only. The files that do not exist in either language are not required."""
    problems: list[str] = []
    for subfolder, pattern in (("", "*.md"), ("images", "**/*")):
        folders = {lang: root / "docs" / "guide" / subfolder / lang for lang in LANGS}
        names = {lang: files_of(folder, pattern) for lang, folder in folders.items()}
        for lang in LANGS:
            other = other_language(lang)
            problems += [
                f"{label(folders[lang] / name, root)}: exists in '{lang}' only, its twin "
                f"{label(folders[other] / name, root)} is missing"
                for name in names[lang]
                if name not in names[other]
            ]
    for lang in LANGS:
        mine, twin = root / README[lang], root / README[other_language(lang)]
        if mine.is_file() and not twin.is_file():
            problems.append(f"{README[lang]}: exists, its twin {README[other_language(lang)]} is missing")
    return problems


def twin_pairs(root: Path) -> list[tuple[Path, Path]]:
    """The pairs (English, Italian) of Markdown files that exist in both languages: the guides and the READMEs."""
    first, second = LANGS
    folders = {lang: root / "docs" / "guide" / lang for lang in LANGS}
    common = sorted(set(files_of(folders[first], "*.md")) & set(files_of(folders[second], "*.md")))
    pairs = [(folders[first] / name, folders[second] / name) for name in common]
    if (root / README[first]).is_file() and (root / README[second]).is_file():
        pairs.append((root / README[first], root / README[second]))
    return pairs


def level_counts(text: str) -> tuple[int, int]:
    counts = Counter(heading.split(" ", 1)[0] for heading in headings(text))
    return counts["##"], counts["###"]


def heading_problems(root: Path) -> list[str]:
    """AC-012: two twins have the same number of `##` and the same number of `###` titles."""
    problems = []
    for first, second in twin_pairs(root):
        count_first, count_second = level_counts(read_text(first)), level_counts(read_text(second))
        if count_first != count_second:
            problems.append(
                f"{label(first, root)} and {label(second, root)}: different titles, {count_first[0]} '##' and "
                f"{count_first[1]} '###' against {count_second[0]} '##' and {count_second[1]} '###'"
            )
    return problems


def link_problems(root: Path) -> list[str]:
    """The first line after the title of each twin links the other one with a relative link."""
    problems = []
    for first, second in twin_pairs(root):
        for path, twin in ((first, second), (second, first)):
            target = os.path.relpath(twin, path.parent).replace(os.sep, "/")
            line = first_line_after_title(read_text(path))
            if f"]({target})" not in line:
                problems.append(
                    f"{label(path, root)}: the first line after the title must link its twin ({target}), it is {line!r}"
                )
    return problems


def image_problems(root: Path) -> list[str]:
    """AC-012: every local image cited by a guide or a README exists (a URL is not read)."""
    documents = [path for lang in LANGS for path in sorted((root / "docs" / "guide" / lang).glob("*.md"))]
    documents += [root / name for name in README.values() if (root / name).is_file()]
    problems = []
    for document in documents:
        for ref in image_refs(read_text(document)):
            if URL_SCHEME.match(ref):
                continue
            target = Path(os.path.normpath(document.parent / unquote(re.split(r"[#?]", ref)[0])))
            if not target.is_file():
                problems.append(f"{label(document, root)}: the image {ref} does not exist")
    return problems


def assert_no_problems(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# Trees under tmp_path ---------------------------------------------------------------------------------


def variable_table(names: list[str]) -> str:
    rows = ["| Variable | Meaning | Default | Required |", "|---|---|---|---|"]
    rows += [f"| `{name}` | what it does | a value | no |" for name in names]
    return "\n".join(rows)


def guide_text(lang: str, names: list[str] = LOCAL_ENV, link: str | None = None, extra: str = "") -> str:
    twin = link if link is not None else f"[{other_language(lang)}](../{other_language(lang)}/{INSTALLATION})"
    return (
        f"# Guide {lang}\n\n{twin}\n\n## Start\n\nOpen the page with GET, check HTTP, ignore E2E_FAKE and "
        f"LIVE_SERVICES.\n\n### Table\n\n{variable_table(names)}\n{extra}"
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def build_tree(root: Path, names: list[str] = LOCAL_ENV) -> Path:
    """A small repository: .env.example and the installation guide in both languages, all aligned."""
    write(root / ".env.example", "# a comment\n\n" + "".join(f"{name}=\n" for name in names))
    for lang in LANGS:
        write(guide_path(lang, INSTALLATION, root), guide_text(lang, names))
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = read_text(path)
    assert old in text
    write(path, text.replace(old, new))


# AC-011: the variables --------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_011_every_variable_of_env_example_is_in_the_installation_guide(lang: str) -> None:
    assert env_example_names(), ".env.example has no variable"
    assert_no_problems(variable_problems(REPO_ROOT, lang))


@pytest.mark.parametrize("lang", LANGS)
def test_AC_011_the_variable_table_has_one_row_per_variable_in_the_order_of_env_example(lang: str) -> None:
    assert env_example_names(), ".env.example has no variable"
    assert_no_problems(table_order_problems(REPO_ROOT, lang))


def test_AC_011_a_tree_with_every_variable_in_both_guides_passes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    for lang in LANGS:
        assert variable_problems(root, lang) == []
        assert table_order_problems(root, lang) == []


def test_AC_011_a_variable_removed_from_one_guide_fails_and_names_the_variable_and_the_guide(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(guide_path("it", INSTALLATION, root), "| `ADMIN_PASSWORD` | what it does | a value | no |\n", "")

    problems = variable_problems(root, "it")

    assert len(problems) == 1
    assert "ADMIN_PASSWORD" in problems[0]
    assert "docs/guide/it/installation.md" in problems[0]
    assert variable_problems(root, "en") == []


def test_AC_011_a_variable_added_to_env_example_fails_in_both_guides(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / ".env.example", read_text(root / ".env.example") + "NEW_SETTING=1\n")

    for lang in LANGS:
        problems = variable_problems(root, lang)
        assert len(problems) == 1
        assert "NEW_SETTING" in problems[0]
        assert f"docs/guide/{lang}/installation.md" in problems[0]


def test_AC_011_a_name_in_the_table_that_is_not_in_env_example_fails_and_is_named(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(
        guide_path("en", INSTALLATION, root),
        "| `HTTP_PORT` |",
        "| `HTTP_PROT` | a typo | a value | no |\n| `HTTP_PORT` |",
    )

    problems = variable_problems(root, "en")

    assert len(problems) == 1
    assert "HTTP_PROT" in problems[0]
    assert "docs/guide/en/installation.md" in problems[0]
    assert variable_problems(root, "it") == []


def test_AC_011_a_missing_installation_guide_fails_and_names_the_file(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    guide_path("it", INSTALLATION, root).unlink()

    problems = variable_problems(root, "it")

    assert len(problems) == 1
    assert "docs/guide/it/installation.md" in problems[0]


def test_AC_011_capital_words_outside_the_first_column_are_not_variables(tmp_path: Path) -> None:
    extra = (
        "\nGET /api/health answers over HTTP; E2E_FAKE and LIVE_SERVICES are for the tests.\n\n"
        "| State | Meaning |\n|---|---|\n| `ok` | fine |\n| `not_configured` | `HTTP_PORT` is not read here |\n\n"
        "| Volume | Content |\n|---|---|\n| `pgdata` | the database |\n\n"
        "```\n| `FROM_A_CODE_BLOCK` | not a row |\n```\n"
    )
    root = build_tree(tmp_path)
    for lang in LANGS:
        write(guide_path(lang, INSTALLATION, root), guide_text(lang, extra=extra))

    for lang in LANGS:
        assert variable_problems(root, lang) == []


def test_AC_011_a_variable_listed_twice_or_out_of_order_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    twice = "| `HTTP_PORT` | what it does | a value | no |\n"
    edit(guide_path("en", INSTALLATION, root), twice, twice + twice)
    edit(
        guide_path("it", INSTALLATION, root),
        "| `ADMIN_USERNAME` | what it does | a value | no |\n| `ADMIN_PASSWORD` | what it does | a value | no |\n",
        "| `ADMIN_PASSWORD` | what it does | a value | no |\n| `ADMIN_USERNAME` | what it does | a value | no |\n",
    )

    assert any("HTTP_PORT" in problem and "2 rows" in problem for problem in table_order_problems(root, "en"))
    assert any("order" in problem for problem in table_order_problems(root, "it"))


def test_AC_011_a_guide_with_windows_line_ends_and_a_bom_is_read_like_any_other(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    windows = "\ufeff" + guide_text("en").replace("\n", "\r\n")
    write(guide_path("en", INSTALLATION, root), windows)

    for lang in LANGS:
        assert variable_problems(root, lang) == []
        assert table_order_problems(root, lang) == []
    assert heading_problems(root) == []
    assert link_problems(root) == []


# AC-012: parity of the languages ------------------------------------------------------------------------


def test_AC_012_the_guides_that_exist_are_in_both_languages_with_their_images() -> None:
    assert_no_problems(missing_twin_problems(REPO_ROOT))


def test_AC_012_the_installation_guide_is_checked_in_both_languages() -> None:
    assert INSTALLATION in {first.name for first, _ in twin_pairs(REPO_ROOT)}


def test_AC_012_the_twins_have_the_same_number_of_titles() -> None:
    assert_no_problems(heading_problems(REPO_ROOT))


def test_AC_012_the_first_line_after_the_title_links_the_twin() -> None:
    assert_no_problems(link_problems(REPO_ROOT))


def test_AC_012_every_image_of_a_guide_exists() -> None:
    assert_no_problems(image_problems(REPO_ROOT))


def test_AC_012_a_tree_without_other_guides_images_or_readmes_passes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert not (root / "docs" / "guide" / "images").exists()
    assert not (root / "README.md").exists()
    assert missing_twin_problems(root) == []
    assert heading_problems(root) == []
    assert link_problems(root) == []
    assert image_problems(root) == []
    assert [first.name for first, _ in twin_pairs(root)] == [INSTALLATION]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_012_a_guide_in_one_language_only_fails_and_names_the_file(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    write(guide_path(lang, "user.md", root), "# Only here\n")

    problems = missing_twin_problems(root)

    assert len(problems) == 1
    assert f"docs/guide/{lang}/user.md" in problems[0]
    assert f"docs/guide/{other_language(lang)}/user.md" in problems[0]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_012_an_image_in_one_language_only_fails_and_names_the_file(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    for both in LANGS:
        write(root / "docs" / "guide" / "images" / both / "login.png", "png")
    write(root / "docs" / "guide" / "images" / lang / "chat.png", "png")

    problems = missing_twin_problems(root)

    assert len(problems) == 1
    assert f"docs/guide/images/{lang}/chat.png" in problems[0]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_012_a_readme_in_one_language_only_fails_and_names_the_file(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    write(root / README[lang], f"# Readme {lang}\n")

    problems = missing_twin_problems(root)

    assert len(problems) == 1
    assert problems[0].startswith(README[lang])


def test_AC_012_a_dot_file_in_one_image_folder_is_not_an_image(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "docs" / "guide" / "images" / "en" / ".gitkeep", "")

    assert missing_twin_problems(root) == []


def test_AC_012_twins_with_a_different_number_of_titles_fail_and_name_both_files(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert heading_problems(root) == []
    edit(guide_path("it", INSTALLATION, root), "### Table", "### Table\n\n### One more")

    problems = heading_problems(root)

    assert len(problems) == 1
    assert "docs/guide/en/installation.md" in problems[0]
    assert "docs/guide/it/installation.md" in problems[0]


def test_AC_012_a_title_moved_from_one_level_to_the_other_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    edit(guide_path("it", INSTALLATION, root), "### Table", "## Table")

    assert len(heading_problems(root)) == 1


def test_AC_012_titles_in_code_blocks_and_deeper_titles_are_not_counted(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    extra = (
        "\n```bash\n## a comment of a script\n### another one\n```\n\n~~~\n## in a tilde block\n~~~\n\n#### Deeper\n"
    )
    write(guide_path("en", INSTALLATION, root), guide_text("en", extra=extra))

    assert heading_problems(root) == []


def test_AC_012_twin_readmes_are_compared_like_the_guides(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "README.md", "# Readme\n\n[Italiano](README.it.md)\n\n## One\n\n## Two\n")
    write(root / "README.it.md", "# Leggimi\n\n[English](README.md)\n\n## Uno\n")

    problems = heading_problems(root)

    assert len(problems) == 1
    assert "README.md" in problems[0]
    assert "README.it.md" in problems[0]
    assert link_problems(root) == []


def test_AC_012_a_first_line_that_does_not_link_the_twin_fails_and_names_the_file(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert link_problems(root) == []
    write(guide_path("it", INSTALLATION, root), guide_text("it", link="Just text, no link."))
    write(guide_path("en", INSTALLATION, root), guide_text("en", link="[Italiano](../it/user.md)"))

    problems = link_problems(root)

    assert len(problems) == 2
    assert any("docs/guide/it/installation.md" in problem for problem in problems)
    assert any("docs/guide/en/installation.md" in problem for problem in problems)


def test_AC_012_the_readmes_link_each_other_from_the_first_line_after_the_title(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "README.md", "# Readme\n\n[Italiano](README.it.md)\n")
    write(root / "README.it.md", "# Leggimi\n\nText first.\n\n[English](README.md)\n")

    problems = link_problems(root)

    assert len(problems) == 1
    assert problems[0].startswith("README.it.md")


def test_AC_012_a_cited_image_that_is_missing_fails_and_names_the_guide_and_the_image(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "docs" / "guide" / "images" / "en" / "login.png", "png")
    write(guide_path("en", INSTALLATION, root), guide_text("en", extra="\n![Login](../images/en/login.png)\n"))
    assert image_problems(root) == []

    write(guide_path("it", INSTALLATION, root), guide_text("it", extra="\n![Accesso](../images/it/login.png)\n"))
    problems = image_problems(root)

    assert len(problems) == 1
    assert "docs/guide/it/installation.md" in problems[0]
    assert "../images/it/login.png" in problems[0]


def test_AC_012_an_image_that_is_a_url_is_not_looked_up_on_disk(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    extra = "\n![badge](https://img.shields.io/badge/a-b-green)\n"
    write(guide_path("en", INSTALLATION, root), guide_text("en", extra=extra))

    assert image_problems(root) == []


# The helpers --------------------------------------------------------------------------------------------


def test_AC_012_read_text_strips_the_bom_and_normalizes_the_line_ends(tmp_path: Path) -> None:
    path = tmp_path / "windows.md"
    path.write_bytes("\ufeff# Titolo\r\n\r\nPerché «sì»\r\n".encode())

    assert read_text(path) == "# Titolo\n\nPerché «sì»\n"


def test_AC_012_headings_are_the_levels_two_and_three_outside_code_blocks() -> None:
    text = (
        "# Title\n\n## First\n\n```bash\n## not a title\n```\n\n### Sub one\n\n#### Too deep\n\n"
        "~~~\n### nor this\n~~~\n\n    ## indented code\n\n##\n\n#hashtag\n\n## Second ##\n"
    )

    assert headings(text) == ["## First", "### Sub one", "## Second ##"]


def test_AC_012_the_first_line_after_the_title_skips_blank_lines() -> None:
    text = "# Installation\n\n\n[Italiano](../it/installation.md)\n\n## Next\n"

    assert first_line_after_title(text) == "[Italiano](../it/installation.md)"


def test_AC_012_the_first_line_after_the_title_ignores_a_hash_line_in_a_code_block() -> None:
    text = "```\n# not the title\n```\n\n# The title\n\nThe line.\n"

    assert first_line_after_title(text) == "The line."


def test_AC_012_image_refs_lists_markdown_images_outside_code_blocks() -> None:
    text = (
        "![One](../images/en/login.png) and [a link](other.md) and ![Two](a/b.png 'title') "
        '![Three](c.png "T")\n\n```\n![code](skip.png)\n```\n![](bare.png)\n'
    )

    assert image_refs(text) == ["../images/en/login.png", "a/b.png", "c.png", "bare.png"]


def test_AC_011_variable_table_names_reads_the_first_column_of_capital_rows_only() -> None:
    text = (
        "| Variable | Meaning |\n|---|---|\n| `HTTP_PORT` | uses `HTTPS_PORT` too |\n| `ok` | a state |\n"
        "| HTTP | not in backticks |\n| `E2E_X1` | a name with a digit |\n|`NO_SPACE`| tight |\n"
        "Prose with `HTTP_PORT` and GET.\n```\n| `IN_CODE` | x |\n```\n"
    )

    assert variable_table_names(text) == ["HTTP_PORT", "E2E_X1", "NO_SPACE"]


def test_AC_011_env_example_names_are_the_assignments_in_order_without_comments(tmp_path: Path) -> None:
    write(
        tmp_path / ".env.example",
        "# Copy to .env\n\n# docker compose -f a.yaml up\nB_SECOND=\nA_FIRST=with = sign\n# C_COMMENTED=1\nD_LAST=x\n",
    )

    assert env_example_names(tmp_path) == ["B_SECOND", "A_FIRST", "D_LAST"]


def test_env_example_names_of_the_repository_are_unique_names_in_capitals() -> None:
    names = env_example_names()

    assert names
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in names)


def test_ui_text_reads_a_dotted_key_of_each_language() -> None:
    assert ui_text("it", "login.title") == "Accedi"
    assert ui_text("en", "login.title") == "Sign in"
    assert ui_text("it", "settings.notConfigured") == "Non configurato"
    assert ui_text("en", "settings.notConfigured") == "Not configured"


def test_ui_text_of_an_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        ui_text("en", "login.no_such_key")


def test_compose_volumes_are_the_top_level_names_in_order(tmp_path: Path) -> None:
    write(
        tmp_path / "compose.yaml",
        "x-anchor: &anchor\n  A: b\nservices:\n  db:\n    volumes:\n      - not_a_top_level_volume:/data\n"
        "volumes:\n  first:\n  second:\n  third-one:\n",
    )

    assert compose_volumes(tmp_path) == ["first", "second", "third-one"]


def test_compose_volumes_of_the_repository_include_the_data_volumes() -> None:
    assert {"pgdata", "uploads", "reranker-models"} <= set(compose_volumes())
