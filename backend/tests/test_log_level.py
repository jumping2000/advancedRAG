# SPEC-2026-11-17-fase-3-chat
"""Logging of the backend (REQ-102, owner's request during the review of T-04): the backend configures logging at
startup, at the level of `LOG_LEVEL`, so that the lines of the chat (ids, counts, durations, codes) reach stderr in
production; what REQ-102 forbids stays out of the output (no text of questions, answers, titles or chunks, no
vectors, no key, no SQL).

`app.run.configure_logging(level)` configures the root logger on stderr, once per process; `main()` calls it after the
migrations (Alembic configures logging itself and must not win) and before the bootstrap, so the line of
`mark_interrupted` shows. uvicorn keeps its own loggers (no duplicated lines). The tests that call `configure_logging`
restore the loggers they touch.
"""

from __future__ import annotations

import logging
import logging.config
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn.config
from pydantic import ValidationError

from app import run
from app.config import get_settings
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings
from tests.fakes.services import fake_vector
from tests.test_chat_ask import (
    API_KEY,
    DIMENSIONS,
    Chat,
    add_conversation,
    ask,
    ask_new,
    chunk_row,
    done_message,
    events_of,
    names_of,
    seed,
)
from tests.test_chat_ask import chat as chat  # noqa: F401  (the fixture)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_errors import backend_env as backend_env  # noqa: F401  (the fixture)
from tests.test_chat_errors import start_backend
from tests.test_chat_retry import retry
from tests.test_env_example import ROOT, env_example

# Loggers that the tests below leave as they found them
TOUCHED = ("", "sqlalchemy.engine", "httpcore", "httpx", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def logging_state() -> Iterator[None]:
    saved = {
        name: (logger.level, logger.propagate, logger.disabled, list(logger.handlers))
        for name in TOUCHED
        for logger in (logging.getLogger(name),)
    }
    yield
    for name, (level, propagate, disabled, handlers) in saved.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


# The setting -------------------------------------------------------------------------------------


def test_the_log_level_defaults_to_info() -> None:
    assert make_settings().log_level == "INFO"


@pytest.mark.parametrize(
    ("value", "level"),
    [
        ("DEBUG", "DEBUG"),
        ("INFO", "INFO"),
        ("WARNING", "WARNING"),
        ("ERROR", "ERROR"),
        ("CRITICAL", "CRITICAL"),
        ("debug", "DEBUG"),
        ("Warning", "WARNING"),
        ("  error ", "ERROR"),
        ("", "INFO"),  # an empty value in .env counts as not set, as for the other optional settings
    ],
)
def test_the_log_level_accepts_the_five_levels_in_any_case(value: str, level: str) -> None:
    assert make_settings(log_level=value).log_level == level


@pytest.mark.parametrize("value", ["LOUD", "TRACE", "WARN", "FATAL", "10", "INFO now", "NOTSET"])
def test_an_invalid_log_level_is_refused_naming_the_variable(value: str) -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        make_settings(log_level=value)


def test_the_log_level_is_in_env_example_and_in_the_backend_service_only() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    backend = compose.split("\n  backend:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  nginx:\n", 1)[0]

    assert env_example()["LOG_LEVEL"] == "INFO"
    assert re.search(r"^\s+LOG_LEVEL: \$\{LOG_LEVEL:-INFO\}", backend, re.MULTILINE)
    assert "LOG_LEVEL" not in worker


# The startup --------------------------------------------------------------------------------------


@pytest.fixture
def entry_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, logging_state: None
) -> Iterator[list[tuple[Any, ...]]]:
    """`run.main()` on the test database with the bootstrap and uvicorn replaced: uvicorn records what the root logger
    looks like at the moment it would start. Yields those records: (level, handlers)."""
    monkeypatch.chdir(tmp_path)  # no .env file is read
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DOCLING_BASE_URL", "http://127.0.0.1:9")
    reached: list[tuple[Any, ...]] = []

    async def bootstrap(_: object) -> None:
        reached.append(("bootstrap", logging.getLogger().level))

    def uvicorn_run(*_: Any, **__: Any) -> None:
        root = logging.getLogger()
        reached.append(("uvicorn", root.level, list(root.handlers)))

    monkeypatch.setattr(run, "bootstrap", bootstrap)
    monkeypatch.setattr(run.uvicorn, "run", uvicorn_run)
    get_settings.cache_clear()
    yield reached
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("value", "level"), [(None, logging.INFO), ("warning", logging.WARNING), ("DEBUG", logging.DEBUG)]
)
def test_the_backend_configures_logging_to_stderr_at_the_log_level_before_it_starts(
    entry_point: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch, value: str | None, level: int
) -> None:
    if value is not None:
        monkeypatch.setenv("LOG_LEVEL", value)

    run.main()

    bootstrap, started = entry_point  # the level is set before the bootstrap, so that its lines can be seen
    assert bootstrap == ("bootstrap", level)
    _, root_level, handlers = started
    assert root_level == level
    # exactly one handler: the one of Alembic (the migrations ran before) is replaced, not added to
    assert [type(handler) for handler in handlers] == [logging.StreamHandler]
    assert handlers[0].stream is sys.stderr  # type: ignore[attr-defined]


def test_the_backend_does_not_start_with_an_invalid_log_level_and_the_message_names_it(
    entry_point: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError) as refused:
        run.main()

    assert "LOG_LEVEL" in str(refused.value)
    assert entry_point == []  # neither the bootstrap nor uvicorn


async def test_the_line_of_the_answers_failed_at_start_reaches_stderr(
    chat: Chat, backend_env: list[int], logging_state: None, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "TESTO-RISERVATO-DELLA-DOMANDA-4417"
    await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", secret), ("assistant", "", "PENDING"), title=secret
    )

    await start_backend()

    err = capsys.readouterr().err
    assert re.search(r"WARNING app\.run: .*interrupted.*: 1\b", err)
    assert secret not in err


# What the output of the backend holds ------------------------------------------------------------


def test_uvicorn_keeps_its_own_loggers_and_no_line_is_written_twice(
    logging_state: None, capsys: pytest.CaptureFixture[str]
) -> None:
    run.configure_logging("INFO")
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)  # what uvicorn.run does when it starts

    logging.getLogger("uvicorn.error").info("UVICORN-LINE-5521")
    logging.getLogger("app.chat.service").info("APP-LINE-8830")

    err = capsys.readouterr().err
    assert err.count("UVICORN-LINE-5521") == 1
    assert err.count("APP-LINE-8830") == 1


async def test_a_full_round_at_info_writes_the_chat_lines_without_text_vectors_key_or_sql(
    chat: Chat, logging_state: None, capsys: pytest.CaptureFixture[str]
) -> None:
    question = "Zorgonauta7741 qual è il valore del parametro?"
    chunk_text = "Il Frobnicatore9902 si regola con la chiave riservata del tecnico"
    answer_text = "Rispondo con Cavolfiore5523 come indicato [1]"
    await seed(chat, [chunk_row(0, chunk_text, question)], filename="segreto-Pippo3318.pdf")
    chat.provider.fragments = [answer_text]
    vector = fake_vector(question, DIMENSIONS)
    # a process where nothing else configured the libraries: what the code pins itself is what counts
    for name in ("sqlalchemy.engine", "httpcore", "httpx"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    run.configure_logging("INFO")

    async with chat.api("mario") as api:
        done = events_of(await ask_new(api, chat.kb_a, question))
        conversation_id = done[0][1]["conversation_id"]
        chat.provider.fail_status = 500
        failed = events_of(await ask(api, conversation_id, "Seconda Baobab6615 domanda?"))
        chat.provider.fail_status = None
        retried = events_of(await retry(api, conversation_id, failed[0][1]["message_id"]))

    assert names_of(done)[-1] == "done"
    assert names_of(failed)[-1] == "error"
    assert done_message(retried)["status"] == "COMPLETED"
    err = capsys.readouterr().err
    for secret in (
        "Zorgonauta7741",
        "Frobnicatore9902",
        "Cavolfiore5523",
        "Pippo3318",
        "Baobab6615",
        API_KEY,
        repr(vector[0])[:9],
        str(vector[1])[:9],
    ):
        assert secret not in err, secret
    assert "sqlalchemy" not in err  # no statement of the database, whatever the level of the root logger
    chat_lines = [line for line in err.splitlines() if "app.chat" in line]
    assert any(f"conversation={conversation_id}" in line and "chat answer completed" in line for line in chat_lines)
    assert any("code=generation_error" in line for line in chat_lines)
