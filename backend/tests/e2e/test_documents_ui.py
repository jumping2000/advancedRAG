# SPEC-2026-11-07-fase-2-ingestion
"""AC-040 to AC-044 (REQ-020): the Documents page in a real browser against the full docker compose stack.

The stack is `compose.yaml` plus `compose.fakes.yaml` (the fake Ollama and Docling of `tests/fakes/services.py`), with
MAX_UPLOAD_SIZE_MB=1. The admin signs in through the UI, opens Documents from the menu, chooses a KB and selects files;
the worker, a service of the Compose, processes them. Everything is waited for with the auto-waiting `expect` of
Playwright (a condition and a timeout), never with a sleep.

Chromium comes from `playwright install chromium` (CI step); PLAYWRIGHT_CHROMIUM_EXECUTABLE, when set, points at a
Chromium already installed on the machine instead.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-docsui"
HTTP_PORT = 18580
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
USER = "luigi"
USER_PASSWORD = "luigi-initial-password"
FILES = ["compose.yaml", "backend/tests/e2e/compose.fakes.yaml"]
PROCESSED = 120_000  # ms: a document goes from "Queued" to "Ready" (worker, fake Ollama, a poll every 3 seconds)

pytestmark = pytest.mark.e2e

GUIDE = (
    b"# Guida\n\nQuesta guida spiega come usare il sistema di prova della pagina Documenti.\n\n"
    b"## Installazione\n\nSi scarica il pacchetto e si avvia il programma con un solo comando.\n"
)
RICETTE = (
    b"# Ricette\n\nQuesto documento raccoglie le ricette della mensa aziendale per la settimana.\n\n"
    b"## Primi\n\nLa pasta si cuoce in acqua salata e si condisce con il sugo del giorno.\n"
)
# Starts like a PDF, so it is accepted; the fake Docling cannot read what follows: the document ends FAILED.
BROKEN_PDF = b"%PDF-1.4\nthis is not a document that docling can read"


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    return {**browser_type_launch_args, **({"executable_path": executable} if executable else {})}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    # An English browser: the UI starts in English (REQ-011).
    return {**browser_context_args, "locale": "en-US"}


def compose_command(env_file: Path, *args: str) -> list[str]:
    command = ["docker", "compose", "-p", PROJECT, "--env-file", str(env_file)]
    for name in FILES:
        command += ["-f", str(ROOT / name)]
    return command + list(args)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    env_file = tmp_path_factory.mktemp("e2e-docsui") / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=e2epassword",
                f"HTTP_PORT={HTTP_PORT}",
                # Overridden by compose.fakes.yaml; compose.yaml only requires them to be set
                "OLLAMA_BASE_URL=http://127.0.0.1:9",
                "DOCLING_BASE_URL=http://127.0.0.1:9",
                "HEALTH_CHECK_TIMEOUT_SECONDS=2",
                "MAX_UPLOAD_SIZE_MB=1",
                f"ADMIN_USERNAME={ADMIN}",
                f"ADMIN_PASSWORD={ADMIN_PASSWORD}",
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


@pytest.fixture(scope="module")
def api(stack: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=stack, timeout=30) as client:
        assert client.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}).status_code == 200
        yield client


def new_kb(api: httpx.Client) -> str:
    """A KB of its own for each test, created through the API: the tests do not depend on each other."""
    name = f"Docs {uuid.uuid4().hex[:8]}"
    assert api.post("/api/kbs", json={"name": name}).status_code == 201
    return name


def sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/")
    page.get_by_label("Username", exact=True).fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def open_documents(page: Page, base_url: str, kb_name: str) -> None:
    """Sign in as the admin, open Documents from the menu and choose the KB."""
    sign_in(page, base_url, ADMIN, ADMIN_PASSWORD)
    page.get_by_role("banner").get_by_role("link", name="Documents", exact=True).click()
    expect(page.get_by_role("heading", name="Documents", exact=True, level=2)).to_be_visible()
    page.get_by_label("Knowledge base", exact=True).select_option(label=kb_name)
    expect(page.get_by_label("Files to upload", exact=True)).to_be_enabled()


def choose_files(page: Page, *files: tuple[str, str, bytes]) -> None:
    page.get_by_label("Files to upload", exact=True).set_input_files(
        [{"name": name, "mimeType": mime, "buffer": content} for name, mime, content in files]
    )


def row_of(page: Page, name: str) -> Locator:
    return page.get_by_role("row").filter(has=page.get_by_role("cell", name=name, exact=True))


def status_of(page: Page, name: str, status: str, timeout: float = PROCESSED) -> None:
    expect(row_of(page, name).get_by_role("cell", name=status, exact=True)).to_be_visible(timeout=timeout)


def upload_item(page: Page, name: str) -> Locator:
    return page.get_by_role("list", name="Uploads").get_by_role("listitem").filter(has_text=name)


def test_AC_040_two_files_become_two_rows_and_reach_ready_without_reloading(
    stack: str, api: httpx.Client, page: Page
) -> None:
    kb = new_kb(api)
    open_documents(page, stack, kb)
    page.evaluate("window.noReload = true")

    choose_files(page, ("guida.txt", "text/plain", GUIDE), ("ricette.txt", "text/plain", RICETTE))

    for name, content in (("guida.txt", GUIDE), ("ricette.txt", RICETTE)):
        row = row_of(page, name)
        expect(row).to_have_count(1)
        expect(row.get_by_role("cell", name=f"{len(content)} B", exact=True)).to_be_visible()
        status_of(page, name, "Ready")
    assert page.evaluate("window.noReload === true"), "the page was reloaded"
    expect(page.get_by_role("row")).to_have_count(3)  # the header and the two documents


def test_AC_041_a_duplicate_a_wrong_type_and_a_file_over_the_limit_each_show_a_message_and_add_no_row(
    stack: str, api: httpx.Client, page: Page
) -> None:
    kb = new_kb(api)
    open_documents(page, stack, kb)
    choose_files(page, ("guida.txt", "text/plain", GUIDE))
    status_of(page, "guida.txt", "Ready")
    rows = page.get_by_role("row").count()

    choose_files(
        page,
        ("copia.txt", "text/plain", GUIDE),  # the same content as guida.txt: a duplicate
        ("programma.exe", "application/octet-stream", b"MZ not a document"),
        # Above the 1 MB limit: refused by the backend (413 file_too_large) ...
        ("grande.txt", "text/plain", b"a" * (3 * 1024 * 1024 // 2)),
        # ... and above the body limit of Nginx (limit + 1 MB): refused by Nginx, with the same error body
        ("enorme.txt", "text/plain", b"a" * (3 * 1024 * 1024)),
    )

    expect(upload_item(page, "copia.txt")).to_contain_text(
        "This file is already in the knowledge base.", timeout=PROCESSED
    )
    expect(upload_item(page, "programma.exe")).to_contain_text(
        "File type not supported. Allowed: PDF, DOCX, PPTX, XLSX, TXT and MD."
    )
    for name in ("grande.txt", "enorme.txt"):
        expect(upload_item(page, name)).to_contain_text("The file exceeds the maximum allowed size.")
    expect(page.get_by_role("row")).to_have_count(rows)
    for name in ("copia.txt", "programma.exe", "grande.txt", "enorme.txt"):
        expect(row_of(page, name)).to_have_count(0)


def test_AC_042_a_failed_document_shows_its_reason_and_retry_puts_it_back_in_the_queue(
    stack: str, api: httpx.Client, page: Page
) -> None:
    kb = new_kb(api)
    open_documents(page, stack, kb)

    choose_files(page, ("rotto.pdf", "application/pdf", BROKEN_PDF), ("guida.txt", "text/plain", GUIDE))

    status_of(page, "rotto.pdf", "Failed")
    status_of(page, "guida.txt", "Ready")
    failed = row_of(page, "rotto.pdf")
    expect(failed.get_by_text("The file could not be read.")).to_be_visible()
    expect(failed.get_by_role("button", name="Retry")).to_be_visible()
    expect(row_of(page, "guida.txt").get_by_role("button", name="Retry")).to_have_count(0)

    failed.get_by_role("button", name="Retry").click()

    expect(failed.get_by_role("cell", name="Queued", exact=True)).to_be_visible()
    expect(failed.get_by_role("button", name="Retry")).to_have_count(0)
    # The same file fails again, without a page reload: the row follows the document
    status_of(page, "rotto.pdf", "Failed")
    expect(failed.get_by_role("button", name="Retry")).to_be_visible()


def test_AC_043_delete_asks_for_a_confirmation_cancel_keeps_the_row_confirm_removes_it(
    stack: str, api: httpx.Client, page: Page
) -> None:
    kb = new_kb(api)
    open_documents(page, stack, kb)
    choose_files(page, ("guida.txt", "text/plain", GUIDE))
    status_of(page, "guida.txt", "Ready")
    row = row_of(page, "guida.txt")

    row.get_by_role("button", name="Delete").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("guida.txt")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    expect(row).to_have_count(1)

    row.get_by_role("button", name="Delete").click()
    dialog.get_by_role("button", name="Confirm").click()

    expect(dialog).to_have_count(0)
    expect(row_of(page, "guida.txt")).to_have_count(0)
    kb_id = next(item["id"] for item in api.get("/api/kbs").json() if item["name"] == kb)
    assert api.get(f"/api/kbs/{kb_id}/documents").json() == []


def test_AC_044_a_user_sees_no_documents_menu_and_the_route_takes_them_to_the_home_page(
    stack: str, api: httpx.Client, page: Page
) -> None:
    created = api.post("/api/admin/users", json={"username": USER, "password": USER_PASSWORD, "role": "user"})
    assert created.status_code in (201, 409)
    sign_in(page, stack, USER, USER_PASSWORD)

    expect(page.get_by_role("banner").get_by_role("link", name="Documents", exact=True)).to_have_count(0)
    page.goto(stack + "/admin/documents")
    expect(page.get_by_role("heading", name="Your knowledge bases")).to_be_visible()
    expect(page).to_have_url(stack + "/")
    expect(page.get_by_role("heading", name="Documents", exact=True)).to_have_count(0)
