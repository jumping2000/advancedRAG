# SPEC-2026-11-06-fase-1-fondazioni
"""AC-003, AC-004 (REQ-002): Nginx answers HTTPS when cert.pem and key.pem are in nginx/certs/, otherwise HTTP only.

compose.yaml alone publishes only HTTP_PORT (REQ-001); the override compose.tls.yaml adds HTTPS_PORT. The compose
stack runs with both files, its own project name and host ports, so it never collides with test_stack.py. The
certificate is self-signed and generated at runtime by openssl inside a container, into a temporary folder: no key is
ever written inside the repository and the host needs no openssl. The X-Forwarded-Proto cases (plan, review focus)
and the half-configured folder run the same Nginx image in front of a fake backend that echoes the request headers.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-tls"
HTTP_PORT = 18180
HTTPS_PORT = 18543
NGINX_IMAGE = f"{PROJECT}-nginx"
OPENSSL_IMAGE = "paradedb/paradedb:0.26.4-pg17"  # already used by compose.yaml, ships openssl
COMPOSE_FILES = ("compose.yaml", "compose.tls.yaml")
ECHO_HTTP_PORT = 18181
ECHO_HTTPS_PORT = 18544
ECHO_NETWORK = f"{PROJECT}-echo"
ECHO_BACKEND = f"{PROJECT}-echo-backend"
ECHO_NGINX = f"{PROJECT}-echo-nginx"
ECHO_BACKEND_IMAGE = "python:3.12.16-slim"
ECHO_BACKEND_CODE = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({name.lower(): value for name, value in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("0.0.0.0", 8000), Echo).serve_forever()
"""
FRONTEND_MARKER = '<div id="root"></div>'

pytestmark = pytest.mark.e2e


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # The ports come from the env file only: a value exported in the shell must not change them
    env = {k: v for k, v in os.environ.items() if k not in {"HTTP_PORT", "HTTPS_PORT"}}
    return subprocess.run(list(args), cwd=ROOT, check=check, capture_output=True, text=True, env=env)


def write_env_file(path: Path, *, https_port: int | None) -> Path:
    lines = [
        "POSTGRES_PASSWORD=e2epassword",
        f"HTTP_PORT={HTTP_PORT}",
        "OLLAMA_BASE_URL=http://127.0.0.1:9",
        "DOCLING_BASE_URL=http://127.0.0.1:9",
        "HEALTH_CHECK_TIMEOUT_SECONDS=1",
        "ADMIN_USERNAME=e2eadmin",
        "ADMIN_PASSWORD=e2e-admin-password",
    ]
    if https_port is not None:
        lines.append(f"HTTPS_PORT={https_port}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def generate_certificate(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    # Same owner as the test process where the OS has one, so the files can be copied and removed afterwards
    user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    run(
        "docker",
        "run",
        "--rm",
        *user,
        "-v",
        f"{folder}:/certs",
        "--entrypoint",
        "openssl",
        OPENSSL_IMAGE,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=IP:127.0.0.1,DNS:localhost",
        "-keyout",
        "/certs/key.pem",
        "-out",
        "/certs/cert.pem",
    )
    assert (folder / "cert.pem").is_file() and (folder / "key.pem").is_file(), "certificate generation failed"
    return folder / "cert.pem"


def trust(cert: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cert))


def https_get(url: str, cert: Path, *, follow_redirects: bool = False) -> httpx.Response:
    """GET over HTTPS trusting the test certificate; no TLS answer at all is a failed expectation, not an error."""
    try:
        return httpx.get(url, timeout=10, verify=trust(cert), follow_redirects=follow_redirects)
    except httpx.TransportError as exc:
        raise AssertionError(f"no HTTPS answer from {url}: {exc!r}") from exc


def wait_for(probe: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


class Stack:
    """The compose stack of compose.yaml, with nginx/certs/ replaced by a temporary folder."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.env_file = write_env_file(workdir / ".env", https_port=HTTPS_PORT)
        self.override = workdir / "compose.override.yaml"
        self.empty_certs = workdir / "certs-empty"
        self.empty_certs.mkdir()
        self.cert = generate_certificate(workdir / "certs")
        self.use_certs(False)

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run(
            "docker",
            "compose",
            "-p",
            PROJECT,
            "--env-file",
            str(self.env_file),
            *compose_file_args(*COMPOSE_FILES),
            "-f",
            str(self.override),
            *args,
            check=check,
        )

    def use_certs(self, with_certs: bool) -> None:
        folder = self.cert.parent if with_certs else self.empty_certs
        volume = {"type": "bind", "source": str(folder), "target": "/etc/nginx/certs", "read_only": True}
        # JSON is valid YAML: no quoting problems with Windows paths
        self.override.write_text(json.dumps({"services": {"nginx": {"volumes": [volume]}}}), encoding="utf-8")

    def up(self, *, with_certs: bool) -> None:
        self.use_certs(with_certs)
        result = self.compose("up", "-d", "--build", "--wait", "--wait-timeout", "180", check=False)
        if result.returncode != 0:
            logs = self.compose("logs", "nginx", check=False).stdout
            raise AssertionError(f"docker compose up failed:\n{result.stderr}\nnginx logs:\n{logs}")

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    for name in COMPOSE_FILES:
        assert (ROOT / name).is_file(), f"{name} is missing"
    stack = Stack(tmp_path_factory.mktemp("e2e-tls"))
    try:
        yield stack
    finally:
        stack.down()


def compose_file_args(*names: str) -> list[str]:
    args: list[str] = []
    for name in names:
        assert (ROOT / name).is_file(), f"{name} is missing"
        args += ["-f", str(ROOT / name)]
    return args


def compose_config(env_file: Path, *files: str) -> dict[str, object]:
    output = run(
        "docker",
        "compose",
        "-p",
        PROJECT,
        "--env-file",
        str(env_file),
        *compose_file_args(*files),
        "config",
        "--format",
        "json",
    ).stdout
    config: dict[str, object] = json.loads(output)
    return config


def nginx_service(config: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    services = config["services"]
    assert isinstance(services, dict)
    nginx: dict[str, list[dict[str, object]]] = services["nginx"]
    return nginx


# --- AC-003: the certificate folder in compose.yaml, the HTTPS port in compose.tls.yaml ---


def published_ports(nginx: dict[str, list[dict[str, object]]]) -> set[tuple[str, object]]:
    return {(str(p.get("published")), p.get("target")) for p in nginx.get("ports", [])}


def test_AC_003_compose_mounts_nginx_certs_into_the_nginx_container(tmp_path: Path) -> None:
    nginx = nginx_service(compose_config(write_env_file(tmp_path / ".env", https_port=HTTPS_PORT), "compose.yaml"))
    mounts = {(Path(str(v["source"])), v["target"]) for v in nginx.get("volumes", []) if v.get("type") == "bind"}
    assert (ROOT / "nginx" / "certs", "/etc/nginx/certs") in mounts


def test_AC_004_compose_yaml_alone_publishes_only_the_http_port(tmp_path: Path) -> None:
    nginx = nginx_service(compose_config(write_env_file(tmp_path / ".env", https_port=HTTPS_PORT), "compose.yaml"))
    assert published_ports(nginx) == {(str(HTTP_PORT), 80)}


def test_AC_003_compose_tls_override_publishes_https_port_to_nginx_443(tmp_path: Path) -> None:
    env_file = write_env_file(tmp_path / ".env", https_port=HTTPS_PORT)
    nginx = nginx_service(compose_config(env_file, *COMPOSE_FILES))
    assert published_ports(nginx) == {(str(HTTP_PORT), 80), (str(HTTPS_PORT), 443)}
    environment = nginx.get("environment")
    assert isinstance(environment, dict)
    assert environment.get("HTTPS_PORT") == str(HTTPS_PORT)


def test_AC_003_https_port_defaults_to_443(tmp_path: Path) -> None:
    nginx = nginx_service(compose_config(write_env_file(tmp_path / ".env", https_port=None), *COMPOSE_FILES))
    assert ("443", 443) in published_ports(nginx)


def test_AC_003_certificate_and_key_in_nginx_certs_are_ignored_by_git() -> None:
    assert (ROOT / "nginx" / "certs").is_dir(), "nginx/certs/ must exist in the repository"
    for name in ("cert.pem", "key.pem"):
        result = run("git", "check-ignore", "-q", f"nginx/certs/{name}", check=False)
        assert result.returncode == 0, f"nginx/certs/{name} is not ignored by git"


# --- AC-003 and AC-004 on the running compose stack ---


def test_AC_004_without_certificates_nginx_starts_and_answers_only_http(stack: Stack) -> None:
    stack.up(with_certs=False)

    page = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/", timeout=10, follow_redirects=False)
    assert page.status_code == 200
    assert FRONTEND_MARKER in page.text

    health = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/api/health", timeout=10, follow_redirects=False)
    assert health.status_code == 200
    assert health.json()["checks"]["database"] == "ok"

    with pytest.raises(httpx.TransportError):
        httpx.get(f"https://127.0.0.1:{HTTPS_PORT}/", timeout=5, verify=trust(stack.cert))


def test_AC_003_with_certificates_http_redirects_301_to_https(stack: Stack) -> None:
    stack.up(with_certs=True)

    page = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/kbs?lang=it", timeout=10, follow_redirects=False)
    assert page.status_code == 301
    assert page.headers["location"] == f"https://127.0.0.1:{HTTPS_PORT}/kbs?lang=it"

    api = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/api/health", timeout=10, follow_redirects=False)
    assert api.status_code == 301
    assert api.headers["location"] == f"https://127.0.0.1:{HTTPS_PORT}/api/health"


def test_AC_003_with_certificates_https_serves_frontend_and_api(stack: Stack) -> None:
    stack.up(with_certs=True)

    page = https_get(f"https://127.0.0.1:{HTTPS_PORT}/", stack.cert)
    assert page.status_code == 200
    assert FRONTEND_MARKER in page.text

    followed = https_get(f"http://127.0.0.1:{HTTP_PORT}/", stack.cert, follow_redirects=True)
    assert followed.status_code == 200
    assert str(followed.url) == f"https://127.0.0.1:{HTTPS_PORT}/"
    assert FRONTEND_MARKER in followed.text

    health = https_get(f"https://127.0.0.1:{HTTPS_PORT}/api/health", stack.cert)
    assert health.status_code == 200
    assert health.json()["checks"]["database"] == "ok"


# --- Review focus: X-Forwarded-Proto set by Nginx from the real scheme, never taken from the client ---


@pytest.fixture(scope="module")
def nginx_image(stack: Stack) -> str:
    stack.up(with_certs=False)  # builds the image from the working tree
    run("docker", "image", "inspect", NGINX_IMAGE)
    return NGINX_IMAGE


def remove_echo_setup() -> None:
    run("docker", "rm", "-f", ECHO_NGINX, ECHO_BACKEND, check=False)
    run("docker", "network", "rm", ECHO_NETWORK, check=False)


@contextmanager
def nginx_in_front_of_echo_backend(image: str, certs: Path, https_port: int) -> Iterator[None]:
    """The Nginx image alone, with a fake `backend` that answers with the headers it received."""
    remove_echo_setup()
    try:
        run("docker", "network", "create", ECHO_NETWORK)
        run(
            "docker",
            "run",
            "-d",
            "--name",
            ECHO_BACKEND,
            "--network",
            ECHO_NETWORK,
            "--network-alias",
            "backend",
            ECHO_BACKEND_IMAGE,
            "python",
            "-c",
            ECHO_BACKEND_CODE,
        )
        run(
            "docker",
            "run",
            "-d",
            "--name",
            ECHO_NGINX,
            "--network",
            ECHO_NETWORK,
            "-p",
            f"127.0.0.1:{ECHO_HTTP_PORT}:80",
            "-p",
            f"127.0.0.1:{ECHO_HTTPS_PORT}:443",
            "-e",
            f"HTTPS_PORT={https_port}",
            "-v",
            f"{certs}:/etc/nginx/certs:ro",
            image,
        )
        yield
    finally:
        remove_echo_setup()


def test_AC_004_http_only_forwards_proto_http_even_if_the_client_says_https(nginx_image: str, stack: Stack) -> None:
    with nginx_in_front_of_echo_backend(nginx_image, stack.empty_certs, HTTPS_PORT):
        url = f"http://127.0.0.1:{ECHO_HTTP_PORT}/api/echo"
        wait_for(lambda: httpx.get(url, timeout=5).status_code == 200, "nginx in front of the echo backend")

        echoed = httpx.get(url, headers={"X-Forwarded-Proto": "https"}, timeout=5, follow_redirects=False)
        assert echoed.status_code == 200
        assert echoed.json()["x-forwarded-proto"] == "http"


def test_AC_003_https_forwards_proto_https_even_if_the_client_says_http(nginx_image: str, stack: Stack) -> None:
    context = trust(stack.cert)
    with nginx_in_front_of_echo_backend(nginx_image, stack.cert.parent, HTTPS_PORT):
        url = f"https://127.0.0.1:{ECHO_HTTPS_PORT}/api/echo"
        wait_for(lambda: httpx.get(url, timeout=5, verify=context).status_code == 200, "nginx over HTTPS")

        echoed = httpx.get(url, headers={"X-Forwarded-Proto": "http"}, timeout=5, verify=context)
        assert echoed.status_code == 200
        assert echoed.json()["x-forwarded-proto"] == "https"


def test_AC_003_redirect_to_default_https_port_has_no_port_in_location(nginx_image: str, stack: Stack) -> None:
    context = trust(stack.cert)
    with nginx_in_front_of_echo_backend(nginx_image, stack.cert.parent, 443):
        wait_for(
            lambda: httpx.get(f"https://127.0.0.1:{ECHO_HTTPS_PORT}/", timeout=5, verify=context).status_code == 200,
            "nginx over HTTPS",
        )

        page = httpx.get(f"http://127.0.0.1:{ECHO_HTTP_PORT}/kbs", timeout=5, follow_redirects=False)
        assert page.status_code == 301
        assert page.headers["location"] == "https://127.0.0.1/kbs"


@pytest.mark.parametrize(("present", "missing"), [("cert.pem", "key.pem"), ("key.pem", "cert.pem")])
def test_AC_004_only_one_of_cert_and_key_stays_http_only_and_names_the_missing_file(
    nginx_image: str, stack: Stack, tmp_path: Path, present: str, missing: str
) -> None:
    half = tmp_path / "certs-half"
    half.mkdir()
    shutil.copyfile(stack.cert.parent / present, half / present)
    with nginx_in_front_of_echo_backend(nginx_image, half, HTTPS_PORT):
        url = f"http://127.0.0.1:{ECHO_HTTP_PORT}/api/echo"
        wait_for(lambda: httpx.get(url, timeout=5).status_code == 200, "nginx in front of the echo backend")

        page = httpx.get(f"http://127.0.0.1:{ECHO_HTTP_PORT}/", timeout=5, follow_redirects=False)
        assert page.status_code == 200
        assert FRONTEND_MARKER in page.text
        with pytest.raises(httpx.TransportError):
            httpx.get(f"https://127.0.0.1:{ECHO_HTTPS_PORT}/", timeout=5, verify=trust(stack.cert))

        logs = run("docker", "logs", ECHO_NGINX)
        assert f"TLS disabled: {missing} missing in /etc/nginx/certs" in logs.stdout + logs.stderr
