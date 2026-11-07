# SPEC-2026-11-07-fase-2-ingestion
"""Repeating a step after a temporary error: AC-031, AC-032, AC-033 (REQ-016), and the settings that drive it.

`with_retries` repeats only a `TemporaryError`, up to `attempts` attempts in all, waiting `backoff_seconds` and then
double, double again, between them. A `PermanentError` and any other exception end it at once. The wait is injected
(`sleep`): no test sleeps for real.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ingestion.errors import PermanentError, TemporaryError
from app.ingestion.retry import with_retries
from tests.conftest import make_settings


class Steps:
    """An operation that raises the given errors one after the other and then returns "done"; counts its calls."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "done"


class Waits:
    """The injected sleep: records the waits instead of waiting."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def temporary(number: int = 0) -> TemporaryError:
    return TemporaryError("docling_error", f"temporary failure {number}")


async def test_AC_031_a_step_that_works_runs_once_and_never_waits() -> None:
    steps, waits = Steps(), Waits()

    result = await with_retries(steps, attempts=3, backoff_seconds=10, sleep=waits)

    assert result == "done"
    assert steps.calls == 1
    assert waits.seconds == []


async def test_AC_031_two_temporary_errors_and_then_success_make_three_attempts_with_10_then_20_seconds() -> None:
    steps, waits = Steps(temporary(1), temporary(2)), Waits()

    result = await with_retries(steps, attempts=3, backoff_seconds=10, sleep=waits)

    assert result == "done"
    assert steps.calls == 3
    assert waits.seconds == [10, 20]


async def test_AC_032_a_temporary_error_every_time_stops_after_exactly_the_attempts_and_raises_the_last_error() -> None:
    errors = [temporary(1), temporary(2), temporary(3), temporary(4)]
    steps, waits = Steps(*errors), Waits()

    with pytest.raises(TemporaryError) as caught:
        await with_retries(steps, attempts=3, backoff_seconds=10, sleep=waits)

    assert caught.value is errors[2]
    assert steps.calls == 3
    assert waits.seconds == [10, 20]  # no wait after the last attempt


async def test_AC_032_the_wait_doubles_at_every_attempt() -> None:
    steps, waits = Steps(*[temporary(number) for number in range(5)]), Waits()

    with pytest.raises(TemporaryError):
        await with_retries(steps, attempts=5, backoff_seconds=1.5, sleep=waits)

    assert steps.calls == 5
    assert waits.seconds == [1.5, 3.0, 6.0, 12.0]


async def test_AC_032_one_attempt_means_no_repetition() -> None:
    steps, waits = Steps(temporary()), Waits()

    with pytest.raises(TemporaryError):
        await with_retries(steps, attempts=1, backoff_seconds=10, sleep=waits)

    assert steps.calls == 1
    assert waits.seconds == []


async def test_AC_032_a_backoff_of_zero_repeats_without_waiting_time() -> None:
    steps, waits = Steps(temporary(), temporary()), Waits()

    assert await with_retries(steps, attempts=3, backoff_seconds=0, sleep=waits) == "done"
    assert waits.seconds == [0, 0]


async def test_AC_033_a_permanent_error_is_not_repeated() -> None:
    error = PermanentError("parse_failed", "The document could not be parsed")
    steps, waits = Steps(error, temporary()), Waits()

    with pytest.raises(PermanentError) as caught:
        await with_retries(steps, attempts=3, backoff_seconds=10, sleep=waits)

    assert caught.value is error
    assert steps.calls == 1
    assert waits.seconds == []


async def test_AC_033_a_permanent_error_after_a_temporary_one_stops_there() -> None:
    error = PermanentError("embedding_invalid_response", "invalid")
    steps, waits = Steps(temporary(), error, temporary()), Waits()

    with pytest.raises(PermanentError) as caught:
        await with_retries(steps, attempts=5, backoff_seconds=10, sleep=waits)

    assert caught.value is error
    assert steps.calls == 2
    assert waits.seconds == [10]


@pytest.mark.parametrize("error", [ValueError("a bug"), KeyError("k"), RuntimeError("x")])
async def test_AC_033_any_other_exception_is_not_repeated_and_propagates(error: Exception) -> None:
    steps, waits = Steps(error, temporary()), Waits()

    with pytest.raises(type(error)) as caught:
        await with_retries(steps, attempts=3, backoff_seconds=10, sleep=waits)

    assert caught.value is error
    assert steps.calls == 1
    assert waits.seconds == []


# The settings --------------------------------------------------------------------------------------------------------


def test_AC_031_the_defaults_are_3_attempts_a_10_second_backoff_and_600_seconds_for_docling() -> None:
    settings = make_settings()

    assert settings.ingestion_max_attempts == 3
    assert settings.ingestion_retry_backoff_seconds == 10
    assert settings.docling_timeout_seconds == 600
    assert settings.min_extracted_chars == 50
    assert settings.docling_api_key is None


@pytest.mark.parametrize(("name", "value"), [("ingestion_max_attempts", 0), ("ingestion_max_attempts", -1)])
def test_AC_031_fewer_than_one_attempt_is_refused(name: str, value: int) -> None:
    with pytest.raises(ValidationError, match=name):
        make_settings(**{name: value})


def test_AC_031_a_negative_backoff_is_refused_and_zero_is_allowed() -> None:
    with pytest.raises(ValidationError, match="ingestion_retry_backoff_seconds"):
        make_settings(ingestion_retry_backoff_seconds=-1)
    assert make_settings(ingestion_retry_backoff_seconds=0).ingestion_retry_backoff_seconds == 0


@pytest.mark.parametrize("value", [0, -5])
def test_AC_031_a_docling_timeout_that_is_not_positive_is_refused(value: float) -> None:
    with pytest.raises(ValidationError, match="docling_timeout_seconds"):
        make_settings(docling_timeout_seconds=value)


def test_AC_019_a_negative_minimum_of_characters_is_refused_and_zero_is_allowed() -> None:
    with pytest.raises(ValidationError, match="min_extracted_chars"):
        make_settings(min_extracted_chars=-1)
    assert make_settings(min_extracted_chars=0).min_extracted_chars == 0


def test_AC_017_the_docling_api_key_is_a_secret_that_never_shows_in_the_settings_text() -> None:
    settings = make_settings(docling_api_key="s3cret-docling-key")

    assert settings.docling_api_key is not None
    assert settings.docling_api_key.get_secret_value() == "s3cret-docling-key"
    assert "s3cret-docling-key" not in repr(settings)
    assert "s3cret-docling-key" not in str(settings.model_dump())


def test_AC_017_docling_does_not_extract_images_unless_the_setting_says_so() -> None:
    assert make_settings().docling_include_images is False
    assert make_settings(docling_include_images=True).docling_include_images is True
