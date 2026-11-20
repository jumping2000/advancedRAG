# SPEC-2026-11-20-fase-5-documentazione
"""Capture of the screenshots of the user and admin guides (REQ-008, AC-009, AC-016). An opt-in tool, not a test of the
application: `uv run --directory backend pytest -m screenshots` (Docker and Chromium needed, like the e2e tests).

It has the marker `screenshots` and NOT `e2e`: the default run of pytest and the e2e step of `scripts/verify.py` never
start it, and the CI does not either. It writes the images of the guides, so it is run by hand when the interface
changes, and the new files are committed with the guides.

The stack is `compose.yaml` plus `compose.fakes.yaml` (the fake Ollama and Docling) with its profiles `generation` (the
fake provider), `rewrite` and `rerank`, as in test_settings_ui.py, in its own Compose project and on its own host port.
Everything it shows is invented: the users `admin`, `mario.rossi` and `jane.doe`, a KB with three one-paragraph
documents, two conversations, one of them with a follow-up question that the fake rewriting service rewrites, so that
the line «Ricerca: …» shows. No password is ever typed in a page that is captured (the sign-in page is captured empty),
and the dates of the tables of the admin pages are covered with a grey box: they would change at every capture.

For each language (the browser starts in Italian, then in English; the UI follows it) it writes to
`docs/guide/images/<lang>/` the files of IMAGES, the same names in both folders. Before each capture it waits, with a
condition, for the text that the page must show, and after it checks the file (PNG, size of 1280x800 at scale 2 or
taller, not empty) and that the page holds no id, date or time outside the covered cells.

Chromium comes from `playwright install chromium`; PLAYWRIGHT_CHROMIUM_EXECUTABLE, when set, points at a Chromium
already installed on the machine instead.
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.docs_support import ui_text

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-screenshots"
HTTP_PORT = 18980
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
FILES = ["compose.yaml", "backend/tests/e2e/compose.fakes.yaml"]
PROFILES = ["generation", "rewrite", "rerank"]
PROCESSED = 120.0  # s: the uploaded documents go from "Queued" to "Ready" (worker, fake Ollama)
ANSWERED = 60_000  # ms: an answer of the fast script, with the retrieval

# Invented accounts. The passwords are placeholders of this tool; no page that is captured shows or holds one.
ADMIN = "admin"
ADMIN_PASSWORD = "example_admin_password"
MARIO = "mario.rossi"
MARIO_PASSWORD = "example_mario_password"
JANE = "jane.doe"
JANE_PASSWORD = "example_jane_password"

VIEWPORT = {"width": 1280, "height": 800}
SCALE = 2
IMAGES = (
    "login.png",
    "kb-list.png",
    "chat.png",
    "admin-users.png",
    "admin-kbs.png",
    "admin-documents.png",
    "admin-conversations.png",
    "admin-settings.png",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MASK_COLOR = "#c8c8c8"
# What no captured page may show outside the covered cells: an id, a date, a time
FORBIDDEN = {
    "an id": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE),
    "a date": re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"),
    "a time": re.compile(r"\b\d{1,2}:\d{2}\b"),
}

pytestmark = pytest.mark.screenshots


@dataclass(frozen=True)
class Content:
    """What one language shows. Only the documents may hold an apostrophe: the other values go through a `.env`."""

    locale: str
    kb: str
    description: str
    documents: dict[str, bytes]
    # The fake reranker scores a text by the first key it contains: the order of the sources of an answer
    first_scores: dict[str, float]
    follow_up_scores: dict[str, float]
    jane_question: str
    jane_answer: str
    first_question: str
    first_answer: str
    follow_up: str
    rewritten: str
    follow_up_answer: str


CONTENT = {
    "it": Content(
        locale="it-IT",
        kb="Manuali prodotto",
        description="Istruzioni, garanzia e spedizione del dispositivo X1.",
        documents={
            "manuale.txt": (
                "Manuale del dispositivo X1.\n"
                "Per accendere il dispositivo tieni premuto il pulsante laterale per tre secondi.\n"
                "Una luce verde indica che il dispositivo è pronto.\n"
                "Per spegnerlo ripeti la stessa operazione.\n"
            ).encode(),
            "garanzia.txt": (
                b"Garanzia del dispositivo X1.\n"
                b"La garanzia dura ventiquattro mesi dalla data di acquisto.\n"
                b"Per usarla spedisci il dispositivo al centro assistenza insieme allo scontrino.\n"
                b"La garanzia non copre i danni causati da cadute.\n"
            ),
            "spedizione.txt": (
                b"Spedizione del dispositivo X1.\n"
                b"La spedizione verso il centro assistenza costa cinque euro in tutta Italia.\n"
                b"Il corriere ritira il pacco all'indirizzo che indichi.\n"
                b"La consegna richiede al massimo tre giorni lavorativi.\n"
            ),
        },
        first_scores={"Manuale": 0.9, "Garanzia": 0.6, "Spedizione": 0.1},
        follow_up_scores={"Garanzia": 0.9, "Spedizione": 0.6, "Manuale": 0.1},
        jane_question="Quanto dura la garanzia?",
        jane_answer="La garanzia dura ventiquattro mesi dalla data di acquisto [1].",
        first_question="Come si accende il dispositivo X1?",
        first_answer="Tieni premuto il pulsante laterale per tre secondi: una luce verde indica che è pronto [1].",
        follow_up="e se si rompe?",
        rewritten="Cosa devo fare se il dispositivo X1 si rompe?",
        follow_up_answer=(
            "Se è in garanzia, spedisci il dispositivo al centro assistenza insieme allo scontrino [1]. "
            "La spedizione costa cinque euro [2]."
        ),
    ),
    "en": Content(
        locale="en-US",
        kb="Product manuals",
        description="Instructions, warranty and shipping for device X1.",
        documents={
            "manual.txt": (
                b"Manual for device X1.\n"
                b"To turn the device on, hold the side button for three seconds.\n"
                b"A green light means the device is ready.\n"
                b"To turn it off, do the same again.\n"
            ),
            "warranty.txt": (
                b"Warranty for device X1.\n"
                b"The warranty lasts twenty-four months from the date of purchase.\n"
                b"To use it, send the device to the service centre together with the receipt.\n"
                b"The warranty does not cover damage caused by drops.\n"
            ),
            "shipping.txt": (
                b"Shipping for device X1.\n"
                b"Sending the device to the service centre costs five euros everywhere in Italy.\n"
                b"The courier collects the parcel at the address you give.\n"
                b"Delivery takes three working days at most.\n"
            ),
        },
        first_scores={"Manual": 0.9, "Warranty": 0.6, "Shipping": 0.1},
        follow_up_scores={"Warranty": 0.9, "Shipping": 0.6, "Manual": 0.1},
        jane_question="How long does the warranty last?",
        jane_answer="The warranty lasts twenty-four months from the date of purchase [1].",
        first_question="How do I turn on device X1?",
        first_answer="Hold the side button for three seconds: a green light means the device is ready [1].",
        follow_up="what if it breaks?",
        rewritten="What should I do if device X1 breaks?",
        follow_up_answer=(
            "If it is under warranty, send the device to the service centre together with the receipt [1]. "
            "Shipping costs five euros [2]."
        ),
    ),
}


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    return {**browser_type_launch_args, **({"executable_path": executable} if executable else {})}


def wait_until(condition: Callable[[], bool], what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.5)


class Stack:
    """The Compose project. `configure` changes what the fake services answer (they read it from the `.env`) and starts
    again only the services whose configuration changed."""

    def __init__(self, env_file: Path) -> None:
        self.env_file = env_file
        self.env: dict[str, str] = {
            "POSTGRES_PASSWORD": "example_database_password",
            "HTTP_PORT": str(HTTP_PORT),
            # Overridden by compose.fakes.yaml; compose.yaml only requires them to be set
            "OLLAMA_BASE_URL": "http://127.0.0.1:9",
            "DOCLING_BASE_URL": "http://127.0.0.1:9",
            "HEALTH_CHECK_TIMEOUT_SECONDS": "2",
            "ADMIN_USERNAME": ADMIN,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "E2E_GENERATION_URL": "http://generation:8080/v1",
            "E2E_REWRITE_URL": "http://rewrite:8080/v1",
            "E2E_RERANK_URL": "http://reranker",
            "E2E_RERANK_API": "tei",
        }

    def command(self, *args: str) -> list[str]:
        command = ["docker", "compose", "-p", PROJECT, "--env-file", str(self.env_file)]
        for profile in PROFILES:
            command += ["--profile", profile]
        for name in FILES:
            command += ["-f", str(ROOT / name)]
        return command + list(args)

    def write_env(self) -> None:
        # Single quotes: the value is taken as it is (JSON with double quotes inside)
        lines = [f"{name}='{value}'" for name, value in self.env.items()]
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.command(*args), cwd=ROOT, check=check, capture_output=True, text=True)

    def start(self) -> None:
        self.write_env()
        self.run("up", "-d", "--build", "--wait", "--wait-timeout", "240")

    def configure(self, **values: str) -> None:
        self.env.update(values)
        self.write_env()
        self.run("up", "-d", "--wait", "--wait-timeout", "120")

    def stop(self) -> None:
        self.run("down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    compose = Stack(tmp_path_factory.mktemp("screenshots") / ".env")
    compose.write_env()
    compose.stop()  # nothing left over from an earlier run of this project
    try:
        compose.start()
        yield compose
    finally:
        compose.stop()


@contextmanager
def signed_in(account: str, password: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        assert client.post("/api/auth/login", json={"username": account, "password": password}).status_code == 200
        yield client


def ask(client: httpx.Client, path: str, question: str) -> None:
    """Ask through the API and read the stream to its end: the answer ends with the event `done`."""
    with client.stream("POST", path, json={"question": question}) as response:
        assert response.status_code == 200
        body = response.read().decode()
    assert "event: done" in body, body


def say(stack: Stack, content: Content, answer: str, scores: dict[str, float]) -> None:
    """The fake provider answers with `answer` from now on; the fake rewriting service and reranker as the language."""
    stack.configure(
        E2E_GENERATION_SCRIPT=json.dumps([answer]),
        E2E_REWRITE_ANSWER=content.rewritten,
        E2E_RERANK_SCORES=json.dumps(scores),
    )


@pytest.fixture(scope="module")
def users(stack: Stack) -> None:
    with signed_in(ADMIN, ADMIN_PASSWORD) as admin:
        for account, password in ((MARIO, MARIO_PASSWORD), (JANE, JANE_PASSWORD)):
            created = admin.post("/api/admin/users", json={"username": account, "password": password, "role": "user"})
            assert created.status_code == 201


@contextmanager
def seeded_kb(stack: Stack, content: Content) -> Iterator[str]:
    """The KB of the language with its three READY documents, both users assigned, both switches of «Impostazioni»
    on and the conversation of `jane.doe`; the KB (and with it its documents and conversations) is deleted at the end,
    so that the other language starts from the same data."""
    with signed_in(ADMIN, ADMIN_PASSWORD) as admin:
        created = admin.post("/api/kbs", json={"name": content.kb, "description": content.description})
        assert created.status_code == 201
        kb_id = created.json()["id"]
        try:
            for account in (MARIO, JANE):
                listed = {user["username"]: user["id"] for user in admin.get("/api/admin/users").json()}
                assert admin.put(f"/api/kbs/{kb_id}/members/{listed[account]}").status_code == 204
            documents = f"/api/kbs/{kb_id}/documents"
            for filename, data in content.documents.items():
                uploaded = admin.post(
                    documents,
                    files={"file": (filename, data, "text/plain")},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                assert uploaded.status_code == 202
            wait_until(
                lambda: sorted(d["status"] for d in admin.get(documents).json()) == ["READY"] * len(content.documents),
                "the documents to be READY",
                PROCESSED,
            )
            for step in ("rewrite_enabled", "rerank_enabled"):
                assert admin.put("/api/admin/settings", json={step: True}).status_code == 200
            say(stack, content, content.jane_answer, content.follow_up_scores)
            with signed_in(JANE, JANE_PASSWORD) as jane:
                ask(jane, f"/api/kbs/{kb_id}/conversations", content.jane_question)
            yield kb_id
        finally:
            admin.delete(f"/api/kbs/{kb_id}")


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from the IHDR chunk; fails if the file is empty or is not a PNG."""
    data = path.read_bytes()
    assert data, f"{path.name} is empty"
    assert data.startswith(PNG_SIGNATURE), f"{path.name} is not a PNG file"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


class Shooter:
    """Captures the pages of one language into its folder, checking each page before and each file after."""

    def __init__(self, page: Page, lang: str) -> None:
        self.page = page
        self.lang = lang
        self.folder = ROOT / "docs" / "guide" / "images" / lang
        self.folder.mkdir(parents=True, exist_ok=True)
        self.written: list[str] = []

    def text(self, key: str) -> str:
        return ui_text(self.lang, key)

    def column(self, header: str) -> Locator:
        """The cells of the column of a table whose header is `header`."""
        titles = [title.strip() for title in self.page.locator("thead th").all_inner_texts()]
        return self.page.locator(f"tbody tr td:nth-child({titles.index(header) + 1})")

    def shoot(self, name: str, shown: list[str], covered: Locator | None = None, full_page: bool = False) -> None:
        page = self.page
        for text in shown:
            expect(page.get_by_text(text, exact=False).first).to_be_visible()
        # A password field of a captured page is empty (nothing typed in the sign-in page, none elsewhere)
        for field_ in page.locator("input[type=password]").all():
            assert field_.input_value() == "", f"{name}: a password field is filled"
        hidden = [covered.nth(i) for i in range(covered.count())] if covered is not None else []
        body: str = page.evaluate("document.body.textContent || ''")
        for cell in hidden:  # what is covered is not read
            body = body.replace(cell.inner_text(), "")
        for what, pattern in FORBIDDEN.items():
            found = pattern.search(body)
            assert found is None, f"{name}: the page shows {what}: {found.group(0) if found else ''}"
        path = self.folder / name
        page.screenshot(path=path, full_page=full_page, mask=hidden, mask_color=MASK_COLOR)
        width, height = png_size(path)
        assert width == VIEWPORT["width"] * SCALE, f"{name}: {width} px wide"
        assert height >= VIEWPORT["height"] * SCALE, f"{name}: {height} px high"
        self.written.append(name)

    def sign_in(self, account: str, password: str) -> None:
        page = self.page
        page.goto(BASE_URL + "/")
        page.get_by_label(self.text("login.username"), exact=True).fill(account)
        page.get_by_label(self.text("login.password"), exact=True).fill(password)
        page.get_by_role("button", name=self.text("login.submit"), exact=True).click()
        expect(page.get_by_role("button", name=self.text("nav.logout"), exact=True)).to_be_visible()
        expect(page.get_by_role("banner").get_by_text(account, exact=True)).to_be_visible()

    def sign_out(self) -> None:
        self.page.get_by_role("button", name=self.text("nav.logout"), exact=True).click()
        expect(self.page.get_by_role("button", name=self.text("login.submit"), exact=True)).to_be_visible()

    def menu(self, key: str) -> None:
        self.page.get_by_role("banner").get_by_role("link", name=self.text(key), exact=True).click()

    def ask(self, question: str) -> None:
        field_ = self.page.get_by_label(self.text("chat.question"), exact=True)
        expect(field_).to_be_enabled()
        field_.fill(question)
        self.page.get_by_role("button", name=self.text("chat.send"), exact=True).click()
        expect(field_).to_be_enabled(timeout=ANSWERED)  # the end of the answer


@pytest.mark.parametrize("lang", ["it", "en"])
def test_the_eight_screenshots_of_the_guides_are_captured_in_the_language(
    lang: str, stack: Stack, users: None, browser: Browser
) -> None:
    content = CONTENT[lang]
    with seeded_kb(stack, content):
        context = browser.new_context(
            locale=content.locale,
            viewport=VIEWPORT,
            device_scale_factor=SCALE,
            color_scheme="light",
            reduced_motion="reduce",
        )
        context.set_default_timeout(30_000)
        try:
            shooter = Shooter(context.new_page(), lang)
            page = shooter.page
            text = shooter.text

            # The sign-in page: empty, nothing typed
            page.goto(BASE_URL + "/")
            shooter.shoot("login.png", [text("login.title"), text("login.username"), text("login.password")])

            # The user: the list of the assigned KBs, then the chat with a question and a follow-up
            shooter.sign_in(MARIO, MARIO_PASSWORD)
            expect(page.get_by_role("heading", name=text("kbs.title"), level=2)).to_be_visible()
            shooter.shoot("kb-list.png", [text("kbs.title"), content.kb, content.description])

            page.get_by_role("list", name=text("kbs.title")).get_by_role("link", name=content.kb, exact=True).click()
            expect(page.get_by_role("heading", name=content.kb, level=2)).to_be_visible()
            say(stack, content, content.first_answer, content.first_scores)
            shooter.ask(content.first_question)
            say(stack, content, content.follow_up_answer, content.follow_up_scores)
            shooter.ask(content.follow_up)
            answers = page.get_by_role("list", name=text("chat.messages")).locator("> li")
            expect(answers).to_have_count(4)
            last = answers.nth(3)
            expect(
                last.get_by_text(ui_text(lang, "chat.searchQuery").replace("{{query}}", content.rewritten))
            ).to_be_visible()
            sources = last.get_by_role("list", name=text("chat.sources")).get_by_role("listitem")
            expect(sources).to_have_count(2)
            sources.nth(0).get_by_role("button", name=text("chat.showExcerpt"), exact=True).click()
            expect(last.locator("blockquote")).to_be_visible()
            shooter.shoot(
                "chat.png",
                [content.first_question, content.follow_up, content.rewritten, text("chat.hideExcerpt")],
                full_page=True,
            )

            # The admin: the five pages of the menu
            shooter.sign_out()
            shooter.sign_in(ADMIN, ADMIN_PASSWORD)

            shooter.menu("users.menu")
            expect(page.get_by_role("heading", name=text("users.title"), level=2)).to_be_visible()
            expect(page.get_by_role("cell", name=JANE, exact=True)).to_be_visible()
            shooter.shoot("admin-users.png", [text("users.title"), ADMIN, MARIO, JANE])

            shooter.menu("kbAdmin.menu")
            expect(page.get_by_role("heading", name=text("kbAdmin.title"), level=2)).to_be_visible()
            row = page.get_by_role("row").filter(has=page.get_by_role("cell", name=content.kb, exact=True))
            row.get_by_role("button", name=text("kbAdmin.members"), exact=True).click()
            expect(
                page.get_by_role("heading", name=ui_text(lang, "kbAdmin.membersTitle").replace("{{name}}", content.kb))
            ).to_be_visible()
            members = page.get_by_role(
                "list", name=ui_text(lang, "kbAdmin.membersTitle").replace("{{name}}", content.kb)
            )
            expect(members.get_by_role("listitem")).to_have_text([re.compile(JANE), re.compile(MARIO)])  # by name
            shooter.shoot("admin-kbs.png", [text("kbAdmin.title"), content.kb])

            shooter.menu("documents.menu")
            expect(page.get_by_role("heading", name=text("documents.title"), level=2)).to_be_visible()
            page.get_by_label(text("documents.kb"), exact=True).select_option(label=content.kb)
            names = list(content.documents)
            expect(page.get_by_role("cell", name=names[0], exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name=text("documents.statuses.READY"), exact=True)).to_have_count(
                len(names)
            )
            shooter.shoot(
                "admin-documents.png",
                [text("documents.title"), *names],
                covered=shooter.column(text("documents.date")),
            )

            shooter.menu("convAdmin.menu")
            expect(page.get_by_role("heading", name=text("convAdmin.title"), level=2)).to_be_visible()
            expect(page.get_by_role("cell", name=content.jane_question, exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name=content.first_question, exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name=MARIO, exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name=JANE, exact=True)).to_be_visible()
            shooter.shoot(
                "admin-conversations.png",
                [text("convAdmin.title"), content.jane_question, content.first_question],
                covered=shooter.column(text("convAdmin.date")),
            )

            shooter.menu("settings.menu")
            expect(page.get_by_role("heading", name=text("settings.title"), level=2)).to_be_visible()
            expect(page.get_by_role("switch", name=text("settings.rewrite"), exact=True)).to_be_checked()
            expect(page.get_by_role("switch", name=text("settings.rerank"), exact=True)).to_be_checked()
            shooter.shoot("admin-settings.png", [text("settings.rewrite"), text("settings.rerank")])
        finally:
            context.close()

    assert shooter.written == list(IMAGES)
    for name in IMAGES:
        assert (shooter.folder / name).stat().st_size > 0
