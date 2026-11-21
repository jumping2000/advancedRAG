# SPEC-2026-11-20-fase-5-documentazione
"""The four guides exist in both languages, each linking its twin: AC-004 (REQ-003).

`docs/guide/en/` and `docs/guide/it/` hold exactly `user.md`, `admin.md`, `installation.md` and `architecture.md` (the
names of `GUIDES`): none missing, none extra, in either language; and each of the eight files has, in the first line
after its title, the relative link to the same guide in the other language (`../it/user.md` from the English one and
`../en/user.md` from the Italian one).

test_docs_alignment.py already checks the twins that exist (a guide in one language only, the number of titles, the link
of a twin that exists, the images). This file is the one that knows which guides there must be: it fails when a guide
is missing in BOTH languages, which the parity check cannot see, and when a file that is not a guide is in the folder.
It could not be written before the last guide existed (the revision of the plan of phase 5: it moved from T-04 to T-06).

Every check is a function of a repository root, so that the tests below run it twice: on the real repository, and on a
small tree under `tmp_path` where the mistake is made on purpose (a guide missing in one language or in both, an extra
file, a folder, a missing or a wrong link to the twin) and must be reported by name. A check that cannot fail would let
any folder pass. A dot file (`.gitkeep`) is not a guide, and a file saved on Windows (a BOM, `\\r\\n`) reads like
any other.

This is a characterization test of the finished state: the four guides exist on `main` when it is written, so it is
green from the start (its Test-Lock commit has no red run); the mutation tests are what proves it can fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.docs_support import GUIDES, LANGS, REPO_ROOT, first_line_after_title, guide_path, read_text

TITLES = {"en": "# Guide", "it": "# Guida"}


def label(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def other_language(lang: str) -> str:
    return next(other for other in LANGS if other != lang)


# The checks -----------------------------------------------------------------------------------------


def folder_problems(root: Path) -> list[str]:
    """AC-004: per language, the folder holds exactly the four guides: each problem names the file and the language."""
    problems: list[str] = []
    for lang in LANGS:
        folder = guide_path(lang, "", root)
        if not folder.is_dir():
            problems.append(f"{label(folder, root)}: the folder of the guides in '{lang}' does not exist")
            continue
        present = {entry.name for entry in folder.iterdir() if not entry.name.startswith(".")}
        problems += [
            f"{label(guide_path(lang, name, root), root)}: the guide is missing in '{lang}'"
            for name in GUIDES
            if not guide_path(lang, name, root).is_file()
        ]
        problems += [
            f"{label(folder / name, root)}: not a guide, '{lang}' must hold only {', '.join(GUIDES)}"
            for name in sorted(present - set(GUIDES))
        ]
        problems += [
            f"{label(guide_path(lang, name, root), root)}: must be a file, it is a folder"
            for name in GUIDES
            if guide_path(lang, name, root).is_dir()
        ]
    return problems


def twin_link_problems(root: Path) -> list[str]:
    """AC-004: the first line after the title of each guide that exists links the same guide in the other language."""
    problems: list[str] = []
    for lang in LANGS:
        for name in GUIDES:
            path = guide_path(lang, name, root)
            if not path.is_file():
                continue
            target = f"../{other_language(lang)}/{name}"
            line = first_line_after_title(read_text(path))
            if f"]({target})" not in line:
                problems.append(
                    f"{label(path, root)}: the first line after the title must link the same guide in "
                    f"'{other_language(lang)}' ({target}), it is {line!r}"
                )
    return problems


def assert_no_problems(problems: list[str]) -> None:
    assert not problems, "\n".join(problems)


# Trees under tmp_path ---------------------------------------------------------------------------------


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def guide_text(lang: str, name: str, link: str | None = None) -> str:
    twin = link if link is not None else f"[{other_language(lang)}](../{other_language(lang)}/{name})"
    return f"{TITLES[lang]} {name}\n\n{twin}\n\n## Start\n\nText.\n"


def build_tree(root: Path) -> Path:
    """A small repository with the eight guides, each linking its twin."""
    for lang in LANGS:
        for name in GUIDES:
            write(guide_path(lang, name, root), guide_text(lang, name))
    return root


# AC-004 on the repository ------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_AC_004_the_folder_holds_exactly_the_four_guides(lang: str) -> None:
    folder = guide_path(lang, "", REPO_ROOT)
    assert sorted(entry.name for entry in folder.iterdir() if not entry.name.startswith(".")) == sorted(GUIDES)


def test_AC_004_the_four_guides_are_user_admin_installation_and_architecture() -> None:
    assert sorted(GUIDES) == ["admin.md", "architecture.md", "installation.md", "user.md"]
    assert LANGS == ("en", "it")


def test_AC_004_no_guide_is_missing_or_extra_in_either_language() -> None:
    assert_no_problems(folder_problems(REPO_ROOT))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", GUIDES)
def test_AC_004_the_first_line_after_the_title_links_the_same_guide_in_the_other_language(lang: str, name: str) -> None:
    path = guide_path(lang, name, REPO_ROOT)
    line = first_line_after_title(read_text(path))
    assert f"]({os.path.join('..', other_language(lang), name).replace(os.sep, '/')})" in line, line


def test_AC_004_every_link_of_a_guide_to_its_twin_resolves() -> None:
    for lang in LANGS:
        for name in GUIDES:
            path = guide_path(lang, name, REPO_ROOT)
            target = path.parent / ".." / other_language(lang) / name
            assert target.resolve() == guide_path(other_language(lang), name, REPO_ROOT).resolve()
            assert target.is_file(), label(path, REPO_ROOT)
    assert_no_problems(twin_link_problems(REPO_ROOT))


# AC-004 on trees under tmp_path: the mistakes, on purpose --------------------------------------------------


def test_AC_004_a_tree_with_the_eight_guides_and_their_links_passes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    assert folder_problems(root) == []
    assert twin_link_problems(root) == []


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", GUIDES)
def test_AC_004_a_guide_missing_in_one_language_fails_and_names_the_file(tmp_path: Path, lang: str, name: str) -> None:
    root = build_tree(tmp_path)
    guide_path(lang, name, root).unlink()

    problems = folder_problems(root)

    assert len(problems) == 1
    assert f"docs/guide/{lang}/{name}" in problems[0]
    assert "missing" in problems[0]


def test_AC_004_a_guide_missing_in_both_languages_fails_in_each(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    for lang in LANGS:
        guide_path(lang, "admin.md", root).unlink()

    problems = folder_problems(root)

    assert len(problems) == 2
    assert {f"docs/guide/{lang}/admin.md" in problem for lang, problem in zip(LANGS, problems, strict=True)} == {True}


def test_AC_004_a_missing_folder_fails_and_names_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    for name in GUIDES:
        guide_path("it", name, root).unlink()
    guide_path("it", "", root).rmdir()

    problems = folder_problems(root)

    assert len(problems) == 1
    assert "docs/guide/it" in problems[0]
    assert "does not exist" in problems[0]


@pytest.mark.parametrize("lang", LANGS)
def test_AC_004_an_extra_file_fails_and_names_it(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    write(guide_path(lang, "faq.md", root), "# FAQ\n")

    problems = folder_problems(root)

    assert len(problems) == 1
    assert f"docs/guide/{lang}/faq.md" in problems[0]
    assert "not a guide" in problems[0]


def test_AC_004_an_extra_folder_fails_and_names_it(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(guide_path("en", "old/user.md", root), "# Old\n")

    problems = folder_problems(root)

    assert len(problems) == 1
    assert "docs/guide/en/old" in problems[0]


def test_AC_004_a_guide_that_is_a_folder_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    guide_path("en", "user.md", root).unlink()
    guide_path("en", "user.md", root).mkdir()

    problems = folder_problems(root)

    assert any("docs/guide/en/user.md" in problem and "folder" in problem for problem in problems)


def test_AC_004_a_dot_file_in_a_folder_is_not_an_extra_file(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(guide_path("en", ".gitkeep", root), "")

    assert folder_problems(root) == []


@pytest.mark.parametrize("lang", LANGS)
def test_AC_004_a_missing_link_to_the_twin_fails_and_names_the_file(tmp_path: Path, lang: str) -> None:
    root = build_tree(tmp_path)
    write(guide_path(lang, "user.md", root), guide_text(lang, "user.md", link="No link here."))

    problems = twin_link_problems(root)

    assert len(problems) == 1
    assert f"docs/guide/{lang}/user.md" in problems[0]
    assert f"../{other_language(lang)}/user.md" in problems[0]


def test_AC_004_a_link_to_another_guide_or_language_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(guide_path("en", "admin.md", root), guide_text("en", "admin.md", link="[it](../it/user.md)"))
    write(guide_path("it", "admin.md", root), guide_text("it", "admin.md", link="[it](../it/admin.md)"))

    problems = twin_link_problems(root)

    assert len(problems) == 2
    assert "docs/guide/en/admin.md" in problems[0]
    assert "docs/guide/it/admin.md" in problems[1]


def test_AC_004_a_link_to_the_twin_that_is_not_in_the_first_line_after_the_title_fails(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    late = "# Guide\n\nThe first line has no link.\n\n[it](../it/installation.md)\n"
    write(guide_path("en", "installation.md", root), late)

    problems = twin_link_problems(root)

    assert len(problems) == 1
    assert "docs/guide/en/installation.md" in problems[0]


def test_AC_004_a_link_inside_a_code_block_is_not_a_link(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(guide_path("it", "architecture.md", root), "# Guida\n\n```\n[en](../en/architecture.md)\n```\n\n## A\n")

    problems = twin_link_problems(root)

    assert len(problems) == 1
    assert "docs/guide/it/architecture.md" in problems[0]


def test_AC_004_a_guide_without_a_title_fails_the_link_check(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(guide_path("en", "user.md", root), "[it](../it/user.md)\n\n## A\n")

    problems = twin_link_problems(root)

    assert len(problems) == 1
    assert "docs/guide/en/user.md" in problems[0]


def test_AC_004_the_link_check_does_not_repeat_a_missing_guide(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    guide_path("it", "user.md", root).unlink()

    assert twin_link_problems(root) == []
    assert len(folder_problems(root)) == 1


def test_AC_004_a_guide_with_windows_line_ends_and_a_bom_is_read_like_any_other(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    windows = "﻿" + guide_text("en", "user.md").replace("\n", "\r\n")
    write(guide_path("en", "user.md", root), windows)

    assert folder_problems(root) == []
    assert twin_link_problems(root) == []
