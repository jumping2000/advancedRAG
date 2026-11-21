# BUG-006: an unforeseen failure of an upload is logged with the message and the traceback of the exception

- Status: open
- Severity / impact: low, a deviation from the documented logging rule on one line; no leak of a name or of content was found (the file name is sanitized, and the failures that reach this line are of the database, Redis or the disk), but the line is the only one of the application that is not limited to the type of the exception.
- Affected spec or area: SPEC-2026-11-07-fase-2-ingestion (REQ-102, AC-046), `backend/app/documents/routes.py`, `docs/system/ingestion.md`, `docs/system/fondazioni.md`, AGENTS.md ("logs never hold document content, names or vectors")

## Reproduction
1. Read `backend/app/documents/routes.py:127-129`:
   ```
   except Exception:
       # Only the id is logged: never the name or the content of the file (REQ-102).
       logger.exception("upload of document %s failed", document_id)
   ```
   `Logger.exception` writes the message of the exception and its whole traceback chain (with `exc_info`).
2. Read the rule in the documents: `docs/system/ingestion.md:72` "only the document id and the error type are logged" (for the 500 of the upload and of the retry), `docs/system/ingestion.md:410-411` and `docs/system/fondazioni.md:115-116` "never content, file names, vectors or exception messages (REQ-102)".
3. Every other log line of the application that handles an exception writes `type(error).__name__` (the retry route at `documents/routes.py:189`, the delete routes, `worker.py:80`, the chat in `chat/service.py:211` and `chat/routes.py:245-249`, whose comments say "the type, never the message").
4. What limits the harm, and what does not: `backend/app/db/session.py:12` creates the engine with `hide_parameters=True`, so a SQLAlchemy error does not echo the bound values (the file name is a parameter of the insert, not part of the statement text). The message that the database server sends is not covered by it: a PostgreSQL error such as a constraint violation carries its own `DETAIL` (for a check violation, "Failing row contains (...)", with the values of the row). That is why the severity stays low and the line is still worth limiting to the type of the error.
5. No test reads this line: `test_logging_privacy.py` (AC-046) covers the lines of the worker and `test_chat_logging_privacy.py` those of the chat, and `grep -rn "upload of document" backend/tests` finds nothing.

## Actual vs expected behaviour
- Actual: a failure after the file is stored (the database, Redis, an OS error) writes the message of the exception and its traceback, in the one place that is not limited to the type.
- Expected (REQ-102, AC-046 and the documents above): the line carries the document id and the type of the error, like the retry route: `logger.error("upload of document %s failed (%s)", document_id, type(error).__name__)`. A message of a database or Redis error can carry data of the request that failed (for instance the detail of a constraint violation shows the failing row), which is why the rule is "the type, never the message".

## Behaviour that must NOT change
- The answer of the upload route (500 `internal_error` with the fixed body), the removal of the row and of the files after the failure, and the 202, 409, 413, 415 and 422 answers.

## Root cause
`logger.exception` was used for its traceback during the development of T-02 of phase 2 and the rule of REQ-102 was not read again for this line; no test covers the log of the upload failure path.

## Fix and tests
- Red test reproducing the bug: not started (a test that makes `queue.enqueue_document` raise an error whose message holds the file name and reads the log)
- Minimal fix: not started (`backend/app/documents/routes.py:129`)
- Non-regression test on the unchanged behaviour: not started (the 500 test of the upload that exists in `test_documents_upload.py`)

## Closure
- [ ] Full `verify` green and the bug's test passing
- [ ] Spec/ACs/`docs/system/` updated if the bug revealed a missing requirement
- [ ] Lesson recorded in `AGENTS.md` or in an ADR, if any
