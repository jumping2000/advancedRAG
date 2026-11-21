# BUG-004: three tests of T-02 of phase 4 would pass with a wrong implementation

- Status: open
- Severity / impact: low, test gaps that the owner accepted at the review of PR #30 (the behaviour is correct today): a later change could break the behaviour without a red test.
- Affected spec or area: SPEC-2026-11-20-fase-4-rerank-rewrite (AC-004 of REQ-003, REQ-006 and REQ-101), `backend/tests/test_chat_rewrite.py`, `backend/tests/test_conversations.py` and `backend/tests/test_chat_retry.py`, `backend/app/chat/service.py`, `backend/app/chat/repository.py`; listed among the known limits of `docs/system/chat.md`

## Reproduction
Still applicable on `main` (the tests have not changed since PR #30; the final check of phase 5 re-read them):

1. AC-004: `test_AC_004_switched_on_and_then_not_configured_sends_nothing_and_the_api_says_so` (`backend/tests/test_chat_rewrite.py:204`). If the answer ignored "configured" (`active_steps`, `backend/app/chat/service.py:345`, uses `settings.rewrite_configured`) and asked the rewriting with an empty `REWRITE_BASE_URL`, the request would be relative and would not be matched by the `outgoing` filter (`startswith(world.rewriter.url)`), and the fake would not see it: the test would still pass.
2. The reset `search_query = None` in `restart_answer` (`backend/app/chat/repository.py:462`) has no failing test of its own: `complete_answer` always overwrites the column, so removing the reset changes nothing a test reads.
3. `(line,) = set(rewrite_lines(logs.lines))` (`backend/tests/test_chat_rewrite.py:662`): a set hides two identical `chat rewrite` lines, so two calls to the rewriting service in one answer would pass.

## Actual vs expected behaviour
- Actual: the three tests do not detect the three wrong implementations above.
- Expected: each of the three behaviours has a test that fails when it breaks: (1) a request with an empty URL, seen in the transport, is an error; (2) after a retry of a failed answer that had a `search_query`, the column is null until `complete_answer` runs (read in between); (3) the list of `chat rewrite` lines of one answer has exactly one distinct line per call (collect the records of one logger, not the duplicated collection of `all_logs`).

## Behaviour that must NOT change
- The behaviour of the chat with the steps on and off (the tests of AC-003 to AC-012 of phase 4) and the log line `chat rewrite <ids> used=… reason=… ms=…`.

## Root cause
The tests were written from the ACs with the fakes available; the three cases need a transport-level probe, a read between two writes, and a log collector that does not duplicate. The owner accepted the gaps in the review comment of PR #30 and `docs/system/chat.md` (Known limits) records them.

## Fix and tests
- Red test reproducing the bug: not started (three new tests; a locked test is not changed)
- Minimal fix: not started (tests only; no change of the application)
- Non-regression test on the unchanged behaviour: not started

## Closure
- [ ] Full `verify` green and the bug's test passing
- [ ] Spec/ACs/`docs/system/` updated if the bug revealed a missing requirement
- [ ] Lesson recorded in `AGENTS.md` or in an ADR, if any
