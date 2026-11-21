# BUG-005: `trace_check.py` reports no test for AC-005 and AC-006 of phase 1 and for AC-002 of phase 2

- Status: open
- Severity / impact: low, a traceability gap in the form of the citations only: the tests exist and pass, but the playbook check cannot attribute them, so it exits 1 for two of the four specs.
- Affected spec or area: SPEC-2026-11-06-fase-1-fondazioni (AC-005, AC-006 of REQ-003), SPEC-2026-11-07-fase-2-ingestion (AC-002 of REQ-001), `backend/tests/test_health.py`, `backend/tests/test_worker_health.py`

## Reproduction
From the root of the repository:

```
python3 scripts/trace_check.py docs/specs/2026-11-06-fase-1-fondazioni/spec.md --tests backend/tests frontend/src
python3 scripts/trace_check.py docs/specs/2026-11-07-fase-2-ingestion/spec.md --tests backend/tests frontend/src
```

The first ends with `[ERROR] REQ-003: none of its ACs has tests` and `TRACE: 2 ACs without tests: AC-005, AC-006` (exit 1); the second with `TRACE: 1 ACs without tests: AC-002` (exit 1). Both print a warning: `9 AC citations without a spec in files that do not declare a single spec (e.g. backend/tests/test_health.py:2, ...)` and `12 AC citations ... (e.g. backend/tests/test_worker_health.py:2, ...)`.

## Actual vs expected behaviour
- Actual: `backend/tests/test_health.py` declares `# SPEC-2026-11-06-fase-1-fondazioni` on line 1 and cites `AC-005` and `AC-006` in short form, but its docstring (line 12) and a comment (line 177) also name `SPEC-2026-11-20-fase-4-rerank-rewrite`; a file that names several specs makes a short citation ambiguous (`short_in_file` in `scripts/trace_check.py`), so the citations do not count. The same happens to `backend/tests/test_worker_health.py` (it names phase 2 on line 1 and phase 4 on line 12) and its `AC-002` of phase 2. The tests exercise the ACs: `test_AC_005_*` and `test_AC_006_*` (`test_health.py:57-165`), `test_AC_002_*` (`test_worker_health.py:55-209`).
- Expected (the rule of `sdd-task`/`test-conventions.md`, "Qualified citation, valid in any file"): every AC of a spec that is `implemented` has a citation that `trace_check.py` counts, so that `trace_check.py <spec>` is `TRACE: OK` for the four specs.

## Behaviour that must NOT change
- The tests of `test_health.py` and `test_worker_health.py`: their assertions, their names and their count; AC-019 and AC-020 of phase 4 (cited in qualified form, they are counted).
- The application.

## Root cause
The tests were written when the repository had one spec, where a short citation always counts; later phases revised the same tests (REQ-011 of phase 4, REQ-001 of phase 2) and named their specs in the file, which made the short citations of the older ACs ambiguous. Nothing runs `trace_check.py` on the old specs after a later phase (it is not a step of `verify.py`).

## Fix and tests
- Red test reproducing the bug: not started (the check itself: `python3 scripts/trace_check.py <spec> --tests backend/tests frontend/src` must exit 0 for phases 1 and 2)
- Minimal fix: not started (qualify the citations of AC-005 and AC-006 in `test_health.py` and of AC-002 in `test_worker_health.py`, for example `SPEC-2026-11-06-fase-1-fondazioni/AC-005` in the function names or in a comment; a change to test files of an earlier phase needs the owner's decision)
- Non-regression test on the unchanged behaviour: not started

## Closure
- [ ] Full `verify` green and the bug's test passing
- [ ] Spec/ACs/`docs/system/` updated if the bug revealed a missing requirement
- [ ] Lesson recorded in `AGENTS.md` or in an ADR, if any
