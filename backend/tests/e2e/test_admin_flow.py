# SPEC-2026-11-06-fase-1-fondazioni
"""AC-030, AC-031 (REQ-012): in a real browser against the full docker compose stack, the admin creates a user and two
knowledge bases from the UI and assigns the user to one; the user, signing in, sees only that KB in the list, opens
it and sees no admin menu.

Chromium comes from `playwright install chromium` (CI step); PLAYWRIGHT_CHROMIUM_EXECUTABLE, when set, points at a
Chromium already installed on the machine instead.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-admin"
HTTP_PORT = 18280
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
USER = "mario"
USER_PASSWORD = "mario-initial-password"

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
    return ["docker", "compose", "-p", PROJECT, "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"), *args]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    env_file = tmp_path_factory.mktemp("e2e-admin") / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=e2epassword",
                f"HTTP_PORT={HTTP_PORT}",
                "OLLAMA_BASE_URL=http://127.0.0.1:9",
                "DOCLING_BASE_URL=http://127.0.0.1:9",
                "HEALTH_CHECK_TIMEOUT_SECONDS=1",
                f"ADMIN_USERNAME={ADMIN}",
                f"ADMIN_PASSWORD={ADMIN_PASSWORD}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            compose_command(env_file, "up", "-d", "--build", "--wait", "--wait-timeout", "180"),
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


def sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/")
    page.get_by_label("Username", exact=True).fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()
    expect(page.get_by_role("banner").get_by_text(username, exact=True)).to_be_visible()


def create_kb(page: Page, name: str, description: str) -> None:
    page.get_by_label("Name", exact=True).fill(name)
    page.get_by_label("Description", exact=True).fill(description)
    page.get_by_role("button", name="Create knowledge base").click()
    expect(page.get_by_role("cell", name=name, exact=True)).to_be_visible()


def test_AC_030_the_admin_creates_a_user_and_a_kb_from_the_ui_and_the_user_sees_it(stack: str, page: Page) -> None:
    sign_in(page, stack, ADMIN, ADMIN_PASSWORD)
    banner = page.get_by_role("banner")

    # The admin creates the user from the Users page.
    banner.get_by_role("link", name="Users", exact=True).click()
    expect(page.get_by_role("heading", name="Users", exact=True)).to_be_visible()
    page.get_by_label("Username", exact=True).fill(USER)
    page.get_by_label("Initial password", exact=True).fill(USER_PASSWORD)
    page.get_by_role("button", name="Create user").click()
    expect(page.get_by_role("status")).to_have_text(f"User {USER} created.")

    # The admin creates two knowledge bases and assigns the user to the first only.
    banner.get_by_role("link", name="Knowledge Base", exact=True).click()
    expect(page.get_by_role("heading", name="Knowledge Base", exact=True)).to_be_visible()
    create_kb(page, "Manuali", "Manuali tecnici")
    create_kb(page, "Ricette", "Ricette della mensa")
    page.get_by_role("row").filter(has=page.get_by_role("cell", name="Manuali", exact=True)).get_by_role(
        "button", name="Assigned users"
    ).click()
    members = page.get_by_role("list", name="Users assigned to Manuali")
    expect(page.get_by_text("No user is assigned.")).to_be_visible()
    page.get_by_label("User to assign").select_option(label=USER)
    page.get_by_role("button", name="Assign", exact=True).click()
    expect(members.get_by_role("listitem")).to_have_count(1)
    expect(members.get_by_role("listitem").first).to_contain_text(USER)

    banner.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("button", name="Sign in")).to_be_visible()

    # The user signs in and finds only the assigned KB in the list, and opens it.
    sign_in(page, stack, USER, USER_PASSWORD)
    kbs = page.get_by_role("list", name="Your knowledge bases")
    expect(kbs.get_by_role("listitem")).to_have_count(1)
    expect(kbs.get_by_role("link", name="Manuali", exact=True)).to_be_visible()
    expect(page.get_by_text("Ricette")).to_have_count(0)

    kbs.get_by_role("link", name="Manuali", exact=True).click()
    expect(page.get_by_role("heading", name="Manuali", level=2)).to_be_visible()
    expect(page.get_by_text("Manuali tecnici")).to_be_visible()


def test_AC_031_the_user_sees_no_admin_menu_and_cannot_open_the_admin_pages(stack: str, page: Page) -> None:
    # Its own user, created through the API: this test does not depend on the AC-030 journey.
    with httpx.Client(base_url=stack, timeout=10) as api:
        assert api.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}).status_code == 200
        created = api.post("/api/admin/users", json={"username": "luigi", "password": USER_PASSWORD, "role": "user"})
        assert created.status_code == 201
    sign_in(page, stack, "luigi", USER_PASSWORD)
    banner = page.get_by_role("banner")

    expect(page.get_by_text("No knowledge base is assigned to you.")).to_be_visible()
    expect(banner.get_by_role("link", name="Users", exact=True)).to_have_count(0)
    expect(banner.get_by_role("link", name="Knowledge Base", exact=True)).to_have_count(0)
    for path in ("/admin/users", "/admin/kbs"):
        page.goto(stack + path)
        expect(page.get_by_role("heading", name="Your knowledge bases")).to_be_visible()
        expect(page).to_have_url(stack + "/")
