# BUG docs/bugs/2026-11-21-user-guide-says-admin-cannot-delete-conversations.md
"""The user guide must tell users that administrators can also DELETE any conversation, not only read it.

The administrator reads and deletes every conversation but cannot ask, retry or rename in another user's one
(ConversationsPage.tsx, chat/routes.py, docs/system/chat.md). The guide said "only read it".
"""

from __future__ import annotations

import pytest

from tests.docs_support import LANGS, guide_path, read_text

PARAGRAPH_START = {"en": "Your conversations are yours", "it": "Le tue conversazioni sono tue"}
DELETE_WORD = {"en": "delete", "it": "elimin"}
WRONG = {"en": "only read it", "it": "solo leggerla"}


def admin_paragraph(lang: str) -> str:
    text = read_text(guide_path(lang, "user.md"))
    start = text.index(PARAGRAPH_START[lang])
    return text[start:].split("\n\n", 1)[0]


@pytest.mark.parametrize("lang", LANGS)
def test_the_guide_says_that_administrators_can_delete_conversations(lang: str) -> None:
    paragraph = admin_paragraph(lang).lower()
    assert DELETE_WORD[lang] in paragraph, f"{lang}: the administrators' paragraph does not mention deleting"


@pytest.mark.parametrize("lang", LANGS)
def test_the_guide_does_not_say_that_administrators_only_read(lang: str) -> None:
    assert WRONG[lang] not in admin_paragraph(lang).lower(), f"{lang}: still says the administrators only read"
