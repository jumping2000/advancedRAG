# SPEC-2026-11-17-fase-3-chat
"""AC-038 and AC-044 (REQ-019, REQ-021): the chat in a real browser against the full docker compose stack, through Nginx.

The stack is `compose.yaml` plus `compose.fakes.yaml` (the fake Ollama and Docling) and its profile `generation`: a fake
generation provider that streams a fragment every 20 seconds for 90 seconds in all. The admin uploads a text through
the API (the worker, a service of the Compose, makes it READY), signs in through the UI, opens the KB and asks a
question. Every fragment must reach the browser within 2 seconds of the moment the provider sends it (Nginx does not
buffer the stream and the keepalive keeps it open), and the answer ends completed, with a numbered source whose
excerpt opens. Everything is waited for with a condition and a timeout, never with a sleep.

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
PROJECT = "advancedrag-e2e-chatui"
HTTP_PORT = 18680
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
FILES = ["compose.yaml", "backend/tests/e2e/compose.fakes.yaml"]
PROCESSED = 120.0  # s: the uploaded document goes from "Queued" to "Ready" (worker, fake Ollama)

# The provider sends these at 20, 40, 60, 80 and 90 seconds after it gets the request (a number is a pause in seconds)
FRAGMENTS = ["alfa ", "bravo ", "charlie ", "delta ", "echo [1]."]
PAUSES = [20, 20, 20, 20, 10]
SCRIPT = [step for pause, fragment in zip(PAUSES, FRAGMENTS, strict=True) for step in (pause, fragment)]
OFFSETS = [20, 40, 60, 80, 90]  # s after the request reached the provider
TOLERANCE = 2000  # ms: AC-044, each fragment reaches the browser within 2 seconds of the moment it is sent
FIRST_WITHIN = 25000  # ms from the click to the first fragment: 20 s of the provider, retrieval and the network

GUIDE = (
    b"# Guida\n\nQuesta guida spiega come usare il sistema di prova della pagina della chat.\n\n"
    b"## Installazione\n\nSi scarica il pacchetto e si avvia il programma con un solo comando.\n"
)

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
    env_file = tmp_path_factory.mktemp("e2e-chatui") / ".env"
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


@pytest.fixture(scope="module")
def kb_name(stack: str) -> str:
    """A KB with one READY document, made through the API."""
    name = "Guide chat"
    with httpx.Client(base_url=stack, timeout=30) as api:
        assert api.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}).status_code == 200
        created = api.post("/api/kbs", json={"name": name})
        assert created.status_code == 201
        documents = f"/api/kbs/{created.json()['id']}/documents"
        uploaded = api.post(
            documents, files={"file": ("guida.txt", GUIDE, "text/plain")}, headers={"X-Requested-With": "XMLHttpRequest"}
        )
        assert uploaded.status_code == 202
        wait_until(
            lambda: [d["status"] for d in api.get(documents).json()] == ["READY"], "the document to be READY", PROCESSED
        )
    return name


def sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/")
    page.get_by_label("Username", exact=True).fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


# Records, in the page, the moment each fragment is first in the text of the page (performance.now(), ms)
WATCH = """
(fragments) => {
    window.arrivals = {};
    const check = () => {
        for (const fragment of fragments) {
            if (!(fragment in window.arrivals) && document.body.textContent.includes(fragment)) {
                window.arrivals[fragment] = performance.now();
            }
        }
    };
    new MutationObserver(check).observe(document.body, { subtree: true, childList: true, characterData: true });
    window.clicked = null;
}
"""


def test_AC_044_038_each_fragment_reaches_the_browser_within_2_seconds_through_nginx_and_the_answer_ends_completed(
    stack: str, kb_name: str, page: Page
) -> None:
    words = [fragment.split()[0] for fragment in FRAGMENTS]  # "alfa", ..., "echo"
    sign_in(page, stack, ADMIN, ADMIN_PASSWORD)
    page.get_by_role("list", name="All knowledge bases").get_by_role("link", name=kb_name, exact=True).click()
    expect(page.get_by_role("heading", name=kb_name, level=2)).to_be_visible()
    question = page.get_by_label("Question", exact=True)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(question).to_be_enabled()
    page.evaluate(WATCH, words)

    question.fill("Come si installa il programma?")
    page.evaluate("window.clicked = performance.now()")
    send.click()

    # AC-038: the text appears before the end and the field stays disabled until the end
    expect(page.get_by_text("alfa", exact=False)).to_be_visible(timeout=FIRST_WITHIN)
    expect(question).to_be_disabled()
    expect(send).to_be_disabled()
    expect(question).to_be_enabled(timeout=120_000)  # the end of the answer: 90 s after the first request
    expect(page.get_by_text("Source no longer available")).to_have_count(0)

    # AC-044: every fragment arrived when the provider sent it, not all together at the end
    clicked: float = page.evaluate("window.clicked")
    arrivals: dict[str, float] = page.evaluate("window.arrivals")
    assert sorted(arrivals) == sorted(words), f"fragments never shown: {set(words) - set(arrivals)}"
    first = arrivals[words[0]]
    assert 0 < first - clicked <= FIRST_WITHIN, f"the first fragment arrived {first - clicked:.0f} ms after the click"
    for word, offset in zip(words, OFFSETS, strict=True):
        late = (arrivals[word] - first) - (offset - OFFSETS[0]) * 1000
        assert abs(late) <= TOLERANCE, f"'{word}' arrived {late:+.0f} ms from when the provider sent it"

    # AC-038: the numbered source, and the excerpt of the cited chunk when the citation is clicked
    answer = page.get_by_role("list", name="Messages").get_by_role("listitem").last
    expect(answer).to_contain_text("alfa bravo charlie delta echo")
    expect(page.get_by_role("list", name="Sources").get_by_role("listitem")).to_have_count(1)
    expect(page.get_by_role("list", name="Sources")).to_contain_text("guida.txt")
    page.get_by_role("button", name="[1]", exact=True).click()
    expect(page.locator("blockquote")).to_contain_text(re.compile("guida|pacchetto", re.IGNORECASE))
    expect(page.get_by_role("button", name="Hide excerpt")).to_be_visible()
