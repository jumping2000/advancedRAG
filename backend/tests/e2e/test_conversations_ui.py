# SPEC-2026-11-17-fase-3-chat
"""AC-042 and AC-043 (REQ-020): the Conversations page of the admin in a real browser against the full Compose stack,
through Nginx.

The stack is `compose.yaml` plus `compose.fakes.yaml` (the fake Ollama and Docling) and its profile `generation` with a
fast script: the fake provider answers at once. Through the API, the admin creates two KBs (one with a READY document,
made by the worker, one empty) and the user `mario`, assigns him to both, and the admin and `mario` ask their
questions: three conversations of two users on two KBs. The admin then signs in through the UI, filters the list by KB
and by user, opens a conversation read-only (messages, a source whose excerpt opens, no question field, no «Retry»)
and deletes one after a confirmation. `mario`, signing in, has no «Conversations» link and the route
`/admin/conversations` takes him to `/`. Everything is waited for with a condition and a timeout, never with a sleep.

Chromium comes from `playwright install chromium` (CI step); PLAYWRIGHT_CHROMIUM_EXECUTABLE, when set, points at a
Chromium already installed on the machine instead.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-convui"
HTTP_PORT = 18780
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
USER = "mario"
USER_PASSWORD = "mario-initial-password"
FILES = ["compose.yaml", "backend/tests/e2e/compose.fakes.yaml"]
PROCESSED = 120.0  # s: the uploaded document goes from "Queued" to "Ready" (worker, fake Ollama)
SCRIPT = ["Si avvia con un solo comando [1]."]  # fast: the provider answers at once

GUIDE = (
    b"# Guida\n\nQuesta guida spiega come usare il sistema di prova della pagina delle conversazioni.\n\n"
    b"## Installazione\n\nSi scarica il pacchetto e si avvia il programma con un solo comando.\n"
)
KB_WITH_DOCUMENT = "Guide conv"
KB_EMPTY = "Altra KB"
MARIO_GUIDE = "Come si installa il programma?"
ADMIN_GUIDE = "Come si aggiorna il programma?"
MARIO_EMPTY = "Cosa contiene l'altra KB?"

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    return {**browser_type_launch_args, **({"executable_path": executable} if executable else {})}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    # An English browser: the UI starts in English (REQ-011).
    return {**browser_context_args, "locale": "en-US"}


def compose_command(env_file: Path, *args: str) -> list[str]:
    command = ["docker", "compose", "-p", PROJECT, "--profile", "generation", "--env-file", str(env_file)]
    for name in FILES:
        command += ["-f", str(ROOT / name)]
    return command + list(args)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    env_file = tmp_path_factory.mktemp("e2e-convui") / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=e2epassword",
                f"HTTP_PORT={HTTP_PORT}",
                # Overridden by compose.fakes.yaml; compose.yaml only requires them to be set
                "OLLAMA_BASE_URL=http://127.0.0.1:9",
                "DOCLING_BASE_URL=http://127.0.0.1:9",
                "HEALTH_CHECK_TIMEOUT_SECONDS=2",
                f"ADMIN_USERNAME={ADMIN}",
                f"ADMIN_PASSWORD={ADMIN_PASSWORD}",
                "E2E_GENERATION_URL=http://generation:8080/v1",
                f"E2E_GENERATION_SCRIPT='{json.dumps(SCRIPT)}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            compose_command(env_file, "up", "-d", "--build", "--wait", "--wait-timeout", "240"),
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        yield BASE_URL
    finally:
        subprocess.run(
            compose_command(env_file, "down", "-v", "--remove-orphans"), cwd=ROOT, capture_output=True, check=False
        )


def wait_until(condition: Callable[[], bool], what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.5)


def sign_in_api(base_url: str, username: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, timeout=60)
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return client


def ask(client: httpx.Client, path: str, question: str) -> None:
    """Ask a question through the API and read the stream to its end: the answer ends with the event `done`."""
    with client.stream("POST", path, json={"question": question}) as response:
        assert response.status_code == 200
        body = response.read().decode()
    assert "event: done" in body, body


@dataclass(frozen=True)
class Seed:
    kb_with_document: str  # id
    kb_empty: str  # id


@pytest.fixture(scope="module")
def seed(stack: str) -> Iterator[Seed]:
    """Two KBs, mario on both, and three conversations: mario on each KB, the admin on the KB with a document."""
    with sign_in_api(stack, ADMIN, ADMIN_PASSWORD) as admin:
        created = admin.post("/api/admin/users", json={"username": USER, "password": USER_PASSWORD, "role": "user"})
        assert created.status_code == 201
        mario_id = created.json()["id"]
        kb_ids = []
        for name in (KB_WITH_DOCUMENT, KB_EMPTY):
            kb = admin.post("/api/kbs", json={"name": name})
            assert kb.status_code == 201
            kb_ids.append(kb.json()["id"])
            assert admin.put(f"/api/kbs/{kb.json()['id']}/members/{mario_id}").status_code == 204
        documents = f"/api/kbs/{kb_ids[0]}/documents"
        uploaded = admin.post(
            documents,
            files={"file": ("guida.txt", GUIDE, "text/plain")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert uploaded.status_code == 202
        wait_until(
            lambda: [d["status"] for d in admin.get(documents).json()] == ["READY"],
            "the document to be READY",
            PROCESSED,
        )
        with sign_in_api(stack, USER, USER_PASSWORD) as mario:
            ask(mario, f"/api/kbs/{kb_ids[0]}/conversations", MARIO_GUIDE)
            ask(admin, f"/api/kbs/{kb_ids[0]}/conversations", ADMIN_GUIDE)
            ask(mario, f"/api/kbs/{kb_ids[1]}/conversations", MARIO_EMPTY)
    yield Seed(kb_with_document=kb_ids[0], kb_empty=kb_ids[1])


def sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/")
    page.get_by_label("Username", exact=True).fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()
    expect(page.get_by_role("banner").get_by_text(username, exact=True)).to_be_visible()


def open_conversations(page: Page) -> None:
    page.get_by_role("banner").get_by_role("link", name="Conversations", exact=True).click()
    expect(page.get_by_role("heading", name="Conversations", level=2)).to_be_visible()


def row_titles(page: Page, titles: list[str]) -> None:
    """The table lists exactly these conversations, in this order (a first cell per row, the header row excluded)."""
    expect(page.get_by_role("row")).to_have_count(len(titles) + 1)
    for index, title in enumerate(titles):
        expect(page.get_by_role("row").nth(index + 1).get_by_role("cell").first).to_have_text(title)


def test_AC_042_the_admin_lists_filters_and_reads_the_conversations_of_everyone_read_only(
    stack: str, seed: Seed, page: Page
) -> None:
    sign_in(page, stack, ADMIN, ADMIN_PASSWORD)
    open_conversations(page)

    # Columns and rows: the most recently updated first, with user, KB and the number of messages (question + answer)
    headers = page.get_by_role("columnheader")
    expect(headers.nth(0)).to_have_text("Title")
    expect(headers.nth(1)).to_have_text("User")
    expect(headers.nth(2)).to_have_text("Knowledge base")
    expect(headers.nth(3)).to_have_text("Date")
    expect(headers.nth(4)).to_have_text("Messages")
    row_titles(page, [MARIO_EMPTY, ADMIN_GUIDE, MARIO_GUIDE])
    first = page.get_by_role("row").nth(1).get_by_role("cell")
    expect(first.nth(1)).to_have_text(USER)
    expect(first.nth(2)).to_have_text(KB_EMPTY)
    expect(first.nth(3)).to_contain_text(re.compile(r"\d{4}"))
    expect(first.nth(4)).to_have_text("2")
    expect(page.get_by_role("row").nth(2).get_by_role("cell").nth(1)).to_have_text(ADMIN)

    # Filters by KB, by user and by both
    by_kb = page.get_by_label("Knowledge base", exact=True)
    by_user = page.get_by_label("User", exact=True)
    by_kb.select_option(label=KB_EMPTY)
    row_titles(page, [MARIO_EMPTY])
    by_kb.select_option(label=KB_WITH_DOCUMENT)
    row_titles(page, [ADMIN_GUIDE, MARIO_GUIDE])
    by_user.select_option(label=USER)
    row_titles(page, [MARIO_GUIDE])
    by_kb.select_option(index=0)  # all the KBs
    row_titles(page, [MARIO_EMPTY, MARIO_GUIDE])
    by_user.select_option(label=ADMIN)
    row_titles(page, [ADMIN_GUIDE])
    by_user.select_option(index=0)  # all the users
    row_titles(page, [MARIO_EMPTY, ADMIN_GUIDE, MARIO_GUIDE])

    # A conversation of another user opens read-only: messages and the numbered source with its excerpt, but no
    # question field, no send button and no «Retry»
    page.get_by_role("row").nth(3).get_by_role("button", name=MARIO_GUIDE, exact=True).click()
    messages = page.get_by_role("list", name="Messages")
    expect(messages).to_contain_text(MARIO_GUIDE)
    expect(messages).to_contain_text("Si avvia con un solo comando")
    expect(page.get_by_role("list", name="Sources")).to_contain_text("guida.txt")
    page.get_by_role("button", name="[1]", exact=True).click()
    expect(page.locator("blockquote")).to_contain_text(re.compile("guida|pacchetto", re.IGNORECASE))
    expect(page.get_by_label("Question", exact=True)).to_have_count(0)
    expect(page.get_by_role("textbox")).to_have_count(0)
    expect(page.get_by_role("button", name="Send", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)

    # The list is untouched by reading
    row_titles(page, [MARIO_EMPTY, ADMIN_GUIDE, MARIO_GUIDE])


def test_AC_042_the_admin_deletes_a_conversation_after_a_confirmation(stack: str, seed: Seed, page: Page) -> None:
    doomed = "Domanda da eliminare"
    with sign_in_api(stack, ADMIN, ADMIN_PASSWORD) as admin:
        ask(admin, f"/api/kbs/{seed.kb_with_document}/conversations", doomed)
        sign_in(page, stack, ADMIN, ADMIN_PASSWORD)
        open_conversations(page)
        row = page.get_by_role("row").filter(has=page.get_by_role("cell", name=doomed, exact=True))
        expect(row).to_have_count(1)

        # «Cancel» leaves it where it is
        row.get_by_role("button", name="Delete", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text(doomed)
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(row).to_have_count(1)

        # «Confirm» removes it, for good
        row.get_by_role("button", name="Delete", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
        expect(row).to_have_count(0)
        expect(page.get_by_role("cell", name=MARIO_GUIDE, exact=True)).to_be_visible()  # the others stay
        titles = [item["title"] for item in admin.get("/api/admin/conversations").json()]
        assert doomed not in titles
        assert {MARIO_GUIDE, ADMIN_GUIDE, MARIO_EMPTY} <= set(titles)
        page.reload()
        expect(page.get_by_role("cell", name=MARIO_GUIDE, exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name=doomed, exact=True)).to_have_count(0)


def test_AC_043_a_user_has_no_conversations_link_and_the_admin_route_takes_them_to_the_home(
    stack: str, seed: Seed, page: Page
) -> None:
    sign_in(page, stack, USER, USER_PASSWORD)
    banner = page.get_by_role("banner")
    expect(banner.get_by_role("link", name="Conversations", exact=True)).to_have_count(0)

    page.goto(stack + "/admin/conversations")
    expect(page.get_by_role("list", name="Your knowledge bases")).to_be_visible()
    assert page.url.rstrip("/") == stack
    expect(page.get_by_role("heading", name="Conversations", level=2)).to_have_count(0)
    expect(page.get_by_role("cell", name=MARIO_GUIDE, exact=True)).to_have_count(0)

    # The API refuses too: no conversation of anyone else reaches a user
    refused = page.request.get(stack + "/api/admin/conversations")
    assert refused.status == 403
    assert refused.json()["error"]["code"] == "forbidden"
