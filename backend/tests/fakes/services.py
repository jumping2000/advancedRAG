"""Fake Ollama and Docling servers: real sockets, no external network, standard library only.

In a test, `with fake_ollama() as ollama:` serves on a free local port (`ollama.url`), records every embedding request
(`ollama.requests`) and answers with deterministic vectors: `fake_vector(text, dimensions)`, derived from the hash
of the text, so a test can tell which vector belongs to which text. The failure modes are fields of the object and
may be changed while the server runs.

`with fake_docling() as docling:` does the same for Docling (`POST /v1/convert/file`, see `FakeDocling`): it records the
requests (file name, `X-Api-Key`, form fields, bytes) and builds the DoclingDocument it answers from the uploaded file
itself, so that a test, or a container, chooses what Docling "reads" by choosing the bytes: `fake_document_file(...)`
gives a file that starts like a PDF (`%PDF-1.4`) and carries the JSON of the DoclingDocument after that first line.

`python -m tests.fakes.services` serves both fakes for the end-to-end tests inside a container (Ollama on 11434,
Docling on 5001, the dimensions from EMBEDDING_DIMENSIONS); it needs nothing else from the project.
"""

from __future__ import annotations

import email.parser
import email.policy
import hashlib
import json
import os
import random
import signal
import socket
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OLLAMA_PORT = 11434
DOCLING_PORT = 5001
DEFAULT_DIMENSIONS = 1024


def fake_vector(text: str, dimensions: int) -> list[float]:
    """The vector the fake Ollama returns for a text: `dimensions` numbers in [-1, 1) from the SHA-256 of the text."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
    generator = random.Random(seed)
    return [generator.uniform(-1.0, 1.0) for _ in range(dimensions)]


@dataclass(frozen=True)
class EmbedRequest:
    """One POST /api/embed as the fake received it."""

    model: object
    texts: list[str]


@dataclass
class FakeOllama:
    url: str = ""
    dimensions: int = DEFAULT_DIMENSIONS
    # Every request received, failed ones included, in arrival order.
    requests: list[EmbedRequest] = field(default_factory=list)
    # Failure modes. From the request number `fail_from_request` (1 is the first) on, `fail_status` is answered
    # instead of the vectors.
    fail_status: int | None = None
    fail_from_request: int = 1
    # When set, only the requests with these numbers fail (instead of every request from `fail_from_request` on).
    fail_requests: set[int] | None = None
    # Answer 200 with a body that is not {"embeddings": [[number, ...], ...]}.
    invalid_body: bool = False
    # Answer 200 with exactly this JSON body.
    body_override: Any = None
    # The answer has this many vectors more (or fewer, if negative) than the request has texts.
    vector_count_delta: int = 0
    # The same for one request only: {request number (1 is the first): delta}, added to `vector_count_delta`.
    vector_count_deltas: dict[int, int] = field(default_factory=dict)
    # Seconds to wait before answering, for the timeout tests.
    delay_seconds: float = 0.0
    # Put this number (nan, inf, -inf) in place of the first number of the first vector of every answer.
    bad_number: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def texts_sent(self) -> list[str]:
        return [text for request in self.requests for text in request.texts]


def _json(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], state: Any) -> None:
        super().__init__(address, handler)
        self.state = state


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class _OllamaHandler(_Quiet):
    server: _Server

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/api/version":
            _json(self, 200, {"version": "0.0.0-fake"})
        else:
            _json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        state: FakeOllama = self.server.state
        if self.path != "/api/embed":
            _json(self, 404, {"error": "not found"})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(raw)
            texts = body["input"]
            if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
                raise ValueError("input must be a list of strings")
        except (ValueError, KeyError, TypeError):
            _json(self, 400, {"error": "invalid request"})
            return
        with state.lock:
            state.requests.append(EmbedRequest(model=body.get("model"), texts=list(texts)))
            number = len(state.requests)
        if state.delay_seconds:
            threading.Event().wait(state.delay_seconds)
        fails = number in state.fail_requests if state.fail_requests is not None else number >= state.fail_from_request
        if state.fail_status is not None and fails:
            _json(self, state.fail_status, {"error": "failure on purpose"})
        elif state.body_override is not None:
            _json(self, 200, state.body_override)
        elif state.invalid_body:
            _json(self, 200, {"embeddings": "not a list of vectors"})
        else:
            count = max(0, len(texts) + state.vector_count_delta + state.vector_count_deltas.get(number, 0))
            vectors = [
                fake_vector(texts[index] if index < len(texts) else f"extra {index}", state.dimensions)
                for index in range(count)
            ]
            if state.bad_number is not None and vectors:
                vectors[0][0] = state.bad_number
            _json(self, 200, {"model": body.get("model"), "embeddings": vectors})


DOCLING_FILE_HEAD = b"%PDF-1.4\n"
FakeItem = tuple[Any, ...]


def docling_document(items: Sequence[FakeItem]) -> dict[str, Any]:
    """A DoclingDocument-shaped dict (the parts the project reads) from simple items, in reading order:
    `("title", text, page)`, `("section_header", text, page, level)`, `("text", text, page)` and
    `("table", [[cell, ...], ...], page)`; `page` is a number or None."""
    texts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    children: list[dict[str, str]] = []
    for item in items:
        kind, content, page = item[0], item[1], item[2]
        prov = [] if page is None else [{"page_no": page, "bbox": {}, "charspan": [0, 1]}]
        if kind == "table":
            grid = [[{"text": cell} for cell in row] for row in content]
            tables.append(
                {
                    "self_ref": f"#/tables/{len(tables)}",
                    "parent": {"$ref": "#/body"},
                    "children": [],
                    "label": "table",
                    "prov": prov,
                    "data": {"num_rows": len(grid), "num_cols": len(grid[0]) if grid else 0, "grid": grid},
                }
            )
            children.append({"$ref": f"#/tables/{len(tables) - 1}"})
            continue
        node: dict[str, Any] = {
            "self_ref": f"#/texts/{len(texts)}",
            "parent": {"$ref": "#/body"},
            "children": [],
            "label": kind,
            "prov": prov,
            "orig": content,
            "text": content,
        }
        if kind == "section_header":
            node["level"] = item[3] if len(item) > 3 else 1
        texts.append(node)
        children.append({"$ref": f"#/texts/{len(texts) - 1}"})
    return {
        "schema_name": "DoclingDocument",
        "version": "1.0.0",
        "name": "fake",
        "furniture": {"self_ref": "#/furniture", "children": [], "content_layer": "furniture", "label": "unspecified"},
        "body": {"self_ref": "#/body", "children": children, "content_layer": "body", "label": "unspecified"},
        "groups": [],
        "texts": texts,
        "pictures": [],
        "tables": tables,
        "pages": {},
    }


def fake_document_file(document: dict[str, Any] | Sequence[FakeItem]) -> bytes:
    """The bytes of a file that the fake Docling converts into this DoclingDocument (or the one built from items)."""
    body = document if isinstance(document, dict) else docling_document(document)
    return DOCLING_FILE_HEAD + json.dumps(body).encode("utf-8")


@dataclass(frozen=True)
class DoclingRequest:
    """One POST /v1/convert/file as the fake received it."""

    filename: str | None
    api_key: str | None
    fields: dict[str, str]
    content: bytes


@dataclass
class FakeDocling:
    url: str = ""
    # Every request received, failed ones included, in arrival order.
    requests: list[DoclingRequest] = field(default_factory=list)
    # Failure modes. `fail_status` is answered instead of the conversion, to the first `fail_count` requests (all of
    # them when it is None).
    fail_status: int | None = None
    fail_count: int | None = None
    # Answer 200 with this `status` (e.g. "failure") instead of "success".
    result_status: str = "success"
    # The `errors` of the answer, as text that a client must never copy into a reason (it may hold file content).
    error_text: str | None = None
    # Answer 200 with a body that is not a conversion result.
    invalid_body: bool = False
    # Close the connection without answering.
    drop_connection: bool = False
    # Wait this long before answering, or until `release` is set (a test that must not wait the whole time sets it).
    delay_seconds: float = 0.0
    release: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], str | None, bytes]:
    """The form fields, the file name and the bytes of the field `files` of a multipart body."""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    filename: str | None = None
    content = b""
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if name == "files":
            filename = part.get_filename()
            content = payload
        elif isinstance(name, str):
            fields[name] = payload.decode("utf-8")
    return fields, filename, content


class _DoclingHandler(_Quiet):
    server: _Server

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            _json(self, 200, {"status": "ok"})
        else:
            _json(self, 404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        state: FakeDocling = self.server.state
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path != "/v1/convert/file":
            _json(self, 404, {"detail": "not found"})
            return
        fields, filename, content = _parse_multipart(self.headers.get("Content-Type", ""), body)
        with state.lock:
            state.requests.append(DoclingRequest(filename, self.headers.get("X-Api-Key"), fields, content))
            number = len(state.requests)
        if state.drop_connection:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if state.delay_seconds:
            state.release.wait(state.delay_seconds)
        if state.fail_status is not None and (state.fail_count is None or number <= state.fail_count):
            _json(self, state.fail_status, {"detail": "failure on purpose"})
            return
        if state.invalid_body:
            _json(self, 200, {"document": "not a document"})
            return
        errors = [{"error_message": state.error_text}] if state.error_text else []
        if state.result_status == "failure":
            _json(self, 200, {"document": {}, "status": "failure", "errors": errors})
            return
        try:
            document = json.loads(content.partition(b"\n")[2])
            if not isinstance(document, dict):
                raise ValueError("not a document")
        except ValueError:
            _json(self, 422, {"detail": "the file could not be converted"})
            return
        _json(
            self,
            200,
            {
                "document": {"json_content": document},
                "status": state.result_status,
                "processing_time": 0.01,
                "timings": {},
                "errors": errors,
            },
        )


def _start(server: _Server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@contextmanager
def fake_ollama(dimensions: int = DEFAULT_DIMENSIONS, **modes: Any) -> Iterator[FakeOllama]:
    """A fake Ollama on a free local port; `modes` set the failure fields of FakeOllama."""
    state = FakeOllama(dimensions=dimensions, **modes)
    server = _Server(("127.0.0.1", 0), _OllamaHandler, state)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    _start(server)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def fake_docling(**modes: Any) -> Iterator[FakeDocling]:
    """A fake Docling on a free local port; `modes` set the failure fields of FakeDocling."""
    state = FakeDocling(**modes)
    server = _Server(("127.0.0.1", 0), _DoclingHandler, state)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    _start(server)
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()


def main() -> None:
    dimensions = int(os.environ.get("EMBEDDING_DIMENSIONS") or DEFAULT_DIMENSIONS)
    servers = [
        _Server(("0.0.0.0", OLLAMA_PORT), _OllamaHandler, FakeOllama(dimensions=dimensions)),
        _Server(("0.0.0.0", DOCLING_PORT), _DoclingHandler, FakeDocling()),
    ]
    for server in servers:
        _start(server)
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    print(f"fake Ollama on {OLLAMA_PORT}, fake Docling on {DOCLING_PORT}", flush=True)
    stop.wait()
    for server in servers:
        server.shutdown()


if __name__ == "__main__":
    main()
