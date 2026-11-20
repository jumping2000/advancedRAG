# SPEC-2026-11-20-fase-4-rerank-rewrite
"""AC-028 (REQ-012, with REQ-007 and REQ-008): the page «Impostazioni» and then the chat in a real browser against the
full Compose stack, through Nginx.

The stack is `compose.yaml` plus `compose.fakes.yaml` (the fake Ollama and Docling) with three of its profiles:
`generation` (the fake provider, with a fast script that cites the three sources), `rewrite` (a fake OpenAI-compatible
rewriting service that answers every request with `REWRITTEN`) and `rerank` (the fake reranker in the `tei` format in
place of the `reranker` service of the Compose, with the scores of `SCORES`). The admin uploads three one-paragraph
texts through the API (the worker makes them READY), signs in through the UI with an Italian browser, switches on the
rewriting and the reranker from the page «Impostazioni», asks a first question in the chat and then a follow-up one.

The order of the reranker differs from the RRF order on purpose: only `spedizione.txt` holds words of the rewritten
question, so the fusion puts it first (it is in both the vector and the BM25 list), while the fake reranker gives it the
lowest score. The answer of the follow-up question must show «Ricerca: <REWRITTEN>» and its sources in the order of the
fake reranker. Everything is waited for with a condition and a timeout, never with a sleep.

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
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-settingsui"
HTTP_PORT = 18880
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
FILES = ["compose.yaml", "backend/tests/e2e/compose.fakes.yaml"]
PROFILES = ["generation", "rewrite", "rerank"]
PROCESSED = 120.0  # s: the uploaded documents go from "Queued" to "Ready" (worker, fake Ollama)
ANSWERED = 60_000  # ms: an answer of the fast script, with the two steps and the retrieval

SCRIPT = ["Risposta [1] [2] [3]."]  # fast: the provider answers at once and cites the three sources
FIRST_QUESTION = "Quanto costa il prodotto X?"
FOLLOW_UP = "e la spedizione?"
REWRITTEN = "Quanto costa la spedizione del prodotto X?"

# One paragraph each, so one chunk each; only the first holds words of REWRITTEN (BM25 finds it alone)
DOCUMENTS = {
    "spedizione.txt": b"La spedizione del prodotto X costa cinque euro in tutta Italia.\n",
    "garanzia.txt": b"Garanzia di ventiquattro mesi dall'acquisto, con lo scontrino.\n",
    "manuale.txt": b"Manuale: accendere il dispositivo tenendo premuto il pulsante.\n",
}
# The fake reranker scores a text by the first key it contains
SCORES = {"Manuale": 0.9, "Garanzia": 0.6, "spedizione": 0.1}
RERANKED = ["manuale.txt", "garanzia.txt", "spedizione.txt"]

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    return {**browser_type_launch_args, **({"executable_path": executable} if executable else {})}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    # An Italian browser: the UI starts in Italian (REQ-011), so the line under the answer reads «Ricerca: …».
    return {**browser_context_args, "locale": "it-IT"}


def compose_command(env_file: Path, *args: str) -> list[str]:
    command = ["docker", "compose", "-p", PROJECT, "--env-file", str(env_file)]
    for profile in PROFILES:
        command += ["--profile", profile]
    for name in FILES:
        command += ["-f", str(ROOT / name)]
    return command + list(args)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    env_file = tmp_path_factory.mktemp("e2e-settingsui") / ".env"
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
                "E2E_REWRITE_URL=http://rewrite:8080/v1",
                f"E2E_REWRITE_ANSWER='{REWRITTEN}'",
                "E2E_RERANK_URL=http://reranker",
                "E2E_RERANK_API=tei",
                f"E2E_RERANK_SCORES='{json.dumps(SCORES)}'",
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


@pytest.fixture(scope="module")
def kb_name(stack: str) -> str:
    """A KB with the three READY documents, made through the API; both switches are still off."""
    name = "Prodotto X"
    with httpx.Client(base_url=stack, timeout=30) as api:
        assert api.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}).status_code == 200
        settings = api.get("/api/admin/settings").json()
        assert settings == {
            "rerank": {"enabled": False, "configured": True},
            "rewrite": {"enabled": False, "configured": True},
        }
        health = api.get("/api/health").json()["checks"]
        assert (health["rewrite"], health["rerank"]) == ("ok", "ok")  # the fakes answer /v1/models and /health
        created = api.post("/api/kbs", json={"name": name})
        assert created.status_code == 201
        documents = f"/api/kbs/{created.json()['id']}/documents"
        for filename, content in DOCUMENTS.items():
            uploaded = api.post(
                documents,
                files={"file": (filename, content, "text/plain")},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert uploaded.status_code == 202
        wait_until(
            lambda: [d["status"] for d in api.get(documents).json()] == ["READY"] * len(DOCUMENTS),
            "the documents to be READY",
            PROCESSED,
        )
    return name


def sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/")
    page.get_by_label("Nome utente", exact=True).fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Accedi").click()
    expect(page.get_by_role("button", name="Esci")).to_be_visible()


def switch_on(page: Page, name: str) -> None:
    """Click the switch and wait for its save (PUT /api/admin/settings) to be answered 200."""
    switch = page.get_by_role("switch", name=name, exact=True)
    expect(switch).to_be_enabled()
    expect(switch).not_to_be_checked()
    with page.expect_response(
        lambda response: response.url.endswith("/api/admin/settings") and response.request.method == "PUT"
    ) as saved:
        switch.click()
    assert saved.value.status == 200
    expect(switch).to_be_checked()


def ask(page: Page, question: str) -> None:
    field = page.get_by_label("Domanda", exact=True)
    expect(field).to_be_enabled()
    field.fill(question)
    page.get_by_role("button", name="Invia", exact=True).click()
    expect(field).to_be_enabled(timeout=ANSWERED)  # the end of the answer


def test_AC_028_both_steps_switched_on_from_the_page_and_the_follow_up_shows_the_search_and_the_reranked_sources(
    stack: str, kb_name: str, page: Page
) -> None:
    sign_in(page, stack, ADMIN, ADMIN_PASSWORD)

    # The page «Impostazioni», from its menu entry: both services are configured, both switches are off
    page.get_by_role("banner").get_by_role("link", name="Impostazioni", exact=True).click()
    expect(page.get_by_role("heading", name="Impostazioni", level=2)).to_be_visible()
    expect(page.get_by_text("Non configurato", exact=True)).to_have_count(0)
    switch_on(page, "Riscrittura della domanda")
    switch_on(page, "Reranker")

    # Saved: still on after a new load of the page, and in the API
    page.reload()
    expect(page.get_by_role("switch", name="Riscrittura della domanda", exact=True)).to_be_checked()
    expect(page.get_by_role("switch", name="Reranker", exact=True)).to_be_checked()
    with httpx.Client(base_url=stack, timeout=30) as api:
        assert api.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}).status_code == 200
        assert api.get("/api/admin/settings").json() == {
            "rerank": {"enabled": True, "configured": True},
            "rewrite": {"enabled": True, "configured": True},
        }

    # The chat: a first question (never rewritten), then a follow-up one
    page.get_by_role("banner").get_by_role("link", name="Home", exact=True).click()
    page.get_by_role("list", name="Tutte le knowledge base").get_by_role("link", name=kb_name, exact=True).click()
    expect(page.get_by_role("heading", name=kb_name, level=2)).to_be_visible()
    ask(page, FIRST_QUESTION)
    ask(page, FOLLOW_UP)

    answers = page.get_by_role("list", name="Messaggi").locator("> li")
    expect(answers).to_have_count(4)
    first_answer, follow_up_answer = answers.nth(1), answers.nth(3)
    expect(first_answer).to_contain_text("Risposta")
    expect(first_answer).not_to_contain_text("Ricerca:")  # the first question of a conversation is not rewritten
    expect(follow_up_answer).to_contain_text("Risposta")
    expect(follow_up_answer.get_by_text(f"Ricerca: {REWRITTEN}", exact=True)).to_be_visible()

    # The sources of the follow-up answer in the order of the fake reranker, not in the RRF order
    sources = follow_up_answer.get_by_role("list", name="Fonti").get_by_role("listitem")
    expect(sources).to_have_text(
        [re.compile(rf"^\[{number}\] {re.escape(name)}") for number, name in enumerate(RERANKED, start=1)]
    )
    sources.nth(0).get_by_role("button", name="Mostra estratto").click()
    expect(follow_up_answer.locator("blockquote")).to_contain_text("Manuale: accendere il dispositivo")
