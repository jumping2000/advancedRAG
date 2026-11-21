# Final check of phases 1-4 (2026-11-21)

- Spec: [SPEC-2026-11-20-fase-5-documentazione](../specs/2026-11-20-fase-5-documentazione/spec.md), REQ-013, task T-06
- Code audited: `main` at `e692c9b` (phases 1-4 as of `5b3767a`, the last commit before phase 5; phase 5 changed no application file, see part 1)
- Kind of work: a read-only audit. Nothing was fixed: every defect is a file in `docs/bugs/`, to be fixed later with `sdd-bugfix`, in the order that the owner decides.

Result in one line: 6 defects (1 medium, 5 low), no high, none that exposes data across knowledge bases; the full verification is green; `trace_check.py` is green for phases 3 and 4 and red for phases 1 and 2 because of the form of the citations in two test files (BUG-005), not because tests are missing.

## 1. Verification

The full `python3 scripts/verify.py`, run through the locked helper that serializes Docker and applies a local-only CA patch to the two Dockerfiles (applied and reverted around the run, never committed), on `e692c9b` with `npm ci --prefix frontend` done:

| Step | Result | Time |
|---|---|---|
| `backend-format` (ruff format) | OK | 2.0 s |
| `backend-lint` (ruff check) | OK | 0.1 s |
| `backend-types` (mypy strict) | OK | 8.1 s |
| `frontend-format` (prettier) | OK | 1.1 s |
| `frontend-lint` (eslint) | OK | 2.7 s |
| `frontend-types` (tsc) | OK | 2.5 s |
| `frontend-test` (vitest) | OK: 11 files, 174 tests passed | 5.7 s |
| `test-services` (Postgres and Redis of `compose.test.yaml`) | OK | 10.6 s |
| `backend-test` (pytest, default markers) | OK: 2,453 passed, 59 deselected (`e2e`, `live`, `screenshots`) | 1,094.8 s |
| `e2e` (`pytest -m e2e`, the full Compose stack) | OK: 54 passed | 393.8 s |
| `secrets` (`secrets_check.py --all`) | OK: 262 files checked, only the "possible secret" warnings on test passwords that were already there | 0.3 s |

`VERIFY: GREEN`, exit 0.

The state of phases 1-4 is therefore: format, lint and types clean, 2,453 backend tests, 174 frontend tests, 54 end-to-end tests of the full Compose stack (real Nginx, backend, worker, Redis, ParadeDB, with fakes for Ollama, Docling, the generation provider, the rewriting service and the reranker), and `secrets_check.py --all` clean.

Run on the final tree of this task (the two new test files and the report added): `VERIFY: GREEN`, exit 0: 2,551 backend tests passed (the 2,453 of above and the 98 of `test_docs_audit.py` and `test_docs_guides_complete.py`), 174 frontend tests, 54 end-to-end tests, and every other step OK.

### The phase did not change the application (AC-015, REQ-101)

The command and its output, run at the head of the T-06 branch (the diff of the whole phase 5 against `5b3767a`, the last commit of `main` before it):

```
git diff --stat 5b3767a..HEAD -- backend/app frontend/src backend/migrations nginx compose.yaml compose.tls.yaml compose.rerank-gpu.yaml compose.test.yaml .github/workflows .env.example
(no output)
```

`backend/tests/e2e/compose.fakes.yaml`, the one file that REQ-101 lets a screenshot need, is untouched too:

```
git diff --stat 5b3767a..HEAD -- backend/tests/e2e/compose.fakes.yaml
(no output)
```

What the whole phase did change, from `git diff --stat 5b3767a..HEAD`: `AGENTS.md` (the port 18980 and the guides, selected by the owner at the design PR; and, in this task, the update proposed at the close of the phase and selected by the owner, commit `674671b`), `LICENSE`, the two READMEs, the eight guides, the 16 screenshots, the two spec files, `backend/pyproject.toml` (the `screenshots` marker, left out of the default run) and test files that are new (`docs_support.py`, `test_docs_*.py`, `test_readme.py`, `e2e/test_screenshots.py`). No existing test of phases 1-4 changed. `diff_scope.py` for T-06: `SCOPE: OK` (no file outside the task, no locked test changed, no sensitive area, no manifest; two warnings about how two lines of `AGENTS.md` are written, which were there before).

## 2. Traceability

`python3 scripts/trace_check.py docs/specs/<spec>/spec.md --tests backend/tests frontend/src`, one run per spec (the playbook's `--tests` takes both folders as one flag):

| Spec | ACs | With a test | Result |
|---|---|---|---|
| [2026-11-06-fase-1-fondazioni](../specs/2026-11-06-fase-1-fondazioni/spec.md) | 33 | 31 | exit 1: `AC-005`, `AC-006` (REQ-003) report no test; warning: 9 citations that do not count. See [BUG-005](../bugs/2026-11-21-trace-check-misses-the-health-tests-of-phases-1-and-2.md) |
| [2026-11-07-fase-2-ingestion](../specs/2026-11-07-fase-2-ingestion/spec.md) | 46 | 45 | exit 1: `AC-002` (REQ-001) reports no test; warning: 12 citations that do not count. Same cause, [BUG-005](../bugs/2026-11-21-trace-check-misses-the-health-tests-of-phases-1-and-2.md) |
| [2026-11-17-fase-3-chat](../specs/2026-11-17-fase-3-chat/spec.md) | 48 | 48 | `TRACE: OK`, no warning |
| [2026-11-20-fase-4-rerank-rewrite](../specs/2026-11-20-fase-4-rerank-rewrite/spec.md) | 28 | 28 | `TRACE: OK`; warning: 21 short citations in `test_health.py` that do not count (the ACs of the phase have other, qualified citations) |
| [2026-11-20-fase-5-documentazione](../specs/2026-11-20-fase-5-documentazione/spec.md) | 17 | 17 | `TRACE: OK` on the tree of T-06 (every AC has a test that cites it); before T-06, on `e692c9b`, `AC-004`, `AC-013`, `AC-014` and `AC-015` had none, which is what the Test-Lock commit of this task adds |

Why the two red results are a citation problem and not missing tests: `backend/tests/test_health.py` and `backend/tests/test_worker_health.py` declare their own spec on line 1 but also name a later spec (phase 4) in their text, and `trace_check.py` does not count a short `AC-nnn` in a file that names several specs. The tests exist and pass: `test_AC_005_*` and `test_AC_006_*` in `test_health.py`, `test_AC_002_*` in `test_worker_health.py`. Nothing runs `trace_check.py` on an old spec after a later phase (it is not a step of `verify.py`), which is why nobody saw it.

Not checked by `trace_check.py`, and not checked here: that each test would catch a wrong implementation (that is the review of each task; the owner-annotated gaps of T-02 of phase 4 are [BUG-004](../bugs/2026-11-21-phase-4-t02-tests-do-not-fail-for-three-wrong-implementations.md)).

## 3. Documents against code

The three area pages (`docs/system/fondazioni.md`, `ingestion.md`, `chat.md`: 1,595 lines) were compared with the code in two ways.

**By a script, for every identifier.** A throwaway script (not committed) took every backticked token of the three pages, 866 in all (348, 197 and 321), classified it and looked it up in the tracked files that are not documents: 102 environment variables, 46 endpoints, 124 paths and file names, 342 identifiers (functions, classes, columns, statuses, settings), and 252 that are other text (status codes, SQL fragments, literals) and were not searched. 25 of the 614 searched tokens had no hit; each was read and judged, and none is a defect:

| Tokens without a hit | Why |
|---|---|
| `/admin/users`, `/admin/kbs`, `/admin/documents`, `/admin/conversations`, `/admin/settings`, `/login` (and `/admin/`) | the routes of `frontend/src/app/App.tsx:52-59`, found there as `path="..."` |
| `/data`, `/data/uploads` | `compose.yaml:13, 81, 115` (`UPLOAD_DIR`, the `uploads` volume) |
| `/health`, `/rerank`, `/v1/models` | `/health` of Nginx (`compose.yaml:164`) and of the reranker (`api/health.py:92`), `/rerank` in `chat/rerank.py:143`, `/v1/models` of the fake Ollama of the e2e tests |
| `BAAI/bge-reranker-v2-m3` | `compose.yaml:157`, `.env.example:137` |
| `Expires`, `HttpOnly` | the cookie attributes: `HttpOnly` at `auth/routes.py:45`; `Expires` is named to say that the cookie has none, which is true |
| `application/json`, `multipart/form-data`, `spec.md` | a media type and a file name, not code |
| `en_US.utf8` | the collation of the ParadeDB image, not in the repository; the effect the page states (`Élite` = `élite`) is tested by `test_knowledge_bases.py:232-238` |

Also compared, with a script: every `GET/POST/PUT/PATCH/DELETE /api/...` of the pages with the routes of the routers (`main.py:45-51` and the `@router` decorators: every documented endpoint exists, and no route of the code is missing from the pages); the identifiers of the class diagrams (all exist, except `ChatRoutes` and `SettingsRoutes`, which draw a `routes.py` module and are named as such in the diagram); the tables and columns of the ER diagrams against `backend/migrations/versions/0001`-`0006` and `db/models.py` (all exist); the default of each setting of `ingestion.md` and `chat.md` against `config.py` and `.env.example` (all equal); the 49 variables of `.env.example` and their defaults against the variable table of both installation guides (98 rows, 0 differences; the names are also held by `test_docs_alignment.py`); the reason codes of `RewriteError` and `RerankError` against the pages; `docs_check.py README.md README.it.md docs` (28 files OK before this task).

**By reading, section by section, against the code.** The result:

| Page and section | Compared with | Result |
|---|---|---|
| `fondazioni.md` Current behaviour (stack, reranker service, worker, cleanup, attempts, users, KBs, Settings page) | `compose.yaml`, `compose.tls.yaml`, `compose.rerank-gpu.yaml`, `nginx/`, `worker.py`, `ingestion/cleanup.py`, `docling.py`, `retry.py`, `auth/`, `knowledge_bases/`, `SettingsPage.tsx` | consistent in everything checked: the five services and the optional sixth, the ports, the 413 body, the TLS conditions, the heartbeat (key, 30 s, 10 s), the Docling request fields, the cookie flags, the length limits of names and passwords, Argon2id, `role="switch"` |
| `fondazioni.md` Constraints and Known limits | `knowledge_bases/repository.py`, `tests/test_isolation.py`, migrations | consistent; no migration applied on `main` was edited (see part 4) |
| `ingestion.md` Current behaviour, Interfaces, statuses, settings, worker lifecycle, retention | `documents/routes.py`, `repository.py`, `storage.py`, `formats.py`, `ingestion/`, `config.py` | consistent; the routes, the error codes, the order of the checks and the retention rules match the code |
| `chat.md` Current behaviour (question, retrieval, SSE events, rewriting, reranker, destinations), Settings, Constraints, Known limits | `chat/routes.py`, `service.py`, `retrieval.py`, `repository.py`, `rewrite.py`, `rerank.py`, `app_settings/` | consistent; the numbers (80 and 200 characters of a title, 15 s keepalive, `hnsw.max_scan_tuples` 200000, `ef_search` 40 to 1000, 1 MiB cap, 10 s cancel wait) are the code's |
| the guides: the factual claims that could be checked cheaply (roles, limits, defaults, the states of `/api/health`, the volumes, the delete of a KB) | `chat/routes.py`, `api/health.py`, `config.py`, `compose.yaml`, `ConversationsPage.tsx` | one wrong statement: [BUG-001](../bugs/2026-11-21-user-guide-says-admin-cannot-delete-conversations.md), the user guide says the administrators can only read the conversations |

Two things the pages already say about themselves, and that stay as they are: `spec.md` of phase 3 says `@@@` for the BM25 operator and the code uses `|||` (`chat.md`, Known limits: the page is the source of truth), and the pages are in English only (the spec of phase 5 leaves them untranslated).

What the comparison found in code and comments, not in the pages: [BUG-002](../bugs/2026-11-21-embed-docstring-says-ollama-is-the-only-destination.md) (a docstring that says Ollama is the only destination of the question), [BUG-003](../bugs/2026-11-21-ricerca-line-loses-the-line-breaks-of-the-rewritten-text.md) (the line «Ricerca: …» does not keep a line break, while the answer does) and [BUG-006](../bugs/2026-11-21-upload-failure-logs-the-exception-with-its-message.md) (an upload failure is logged with the message of the exception, where `ingestion.md:72` says only the id and the type of the error are logged).

## 4. Sensitive areas

Each area of the `Sensitive areas` line of `AGENTS.md`, read against the rules of the file. "Read" means the code was read, with `grep` of the whole `backend/app` for the access patterns; "tests" means a test of the repository exists for the rule, found by name (the full run is part 1).

| Area | What was checked | Result |
|---|---|---|
| `backend/app/auth/` | login, logout, sessions, `require_admin`, `require_json`, cookie flags, bootstrap, password hashing | One answer for a wrong password, an unknown user and a deactivated user; a dummy hash against timing; the session is reloaded from the database at every request and ends with `session_epoch`; cookie `HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS; JSON-only bodies (CSRF) with the one upload exception that needs `X-Requested-With`; Argon2id. No defect. |
| `backend/app/documents/` | every route behind `require_admin` at router level and a KB checked with `KnowledgeBaseRepository.get_for` first; the same 404 for a document of another KB, an unknown id and a non-UUID; `UploadStorage` (names are UUIDs, never the uploaded name; `.part` then atomic rename; `"xb"` never overwrites); `sanitize_filename`; retention (`cleanup.py`: expired FAILED, orphans older than 1 hour, finished originals); delete of a document and of a KB removes the originals; logs | The rules of `AGENTS.md` hold: originals only in `UPLOAD_DIR`, named by the document id, deleted at READY or UNSUPPORTED, with the document or its KB, and after `FAILED_ORIGINAL_RETENTION_DAYS`. One deviation in the logs: [BUG-006](../bugs/2026-11-21-upload-failure-logs-the-exception-with-its-message.md). `DocumentRepository` puts `kb_id` in every query but does not check the KB itself: the route does, and only the admin reaches it (the admin skips the join anyway), so no test calls `DocumentRepository` as a non-admin. |
| `backend/app/chat/`: isolation | every statement that reads `conversations`, `messages`, `message_sources` or `chunks` (all `select`/`insert`/`update`/`delete` of the module); `ConversationRepository.get_for/list_for` (`user_id`, `kb_id IN visible_kb_ids(user)`, one statement); `create` as `INSERT ... SELECT ... WHERE kb IN visible`; the retrieval scope `_scope` (`kb_id`, the KB asked inside `visible_kb_ids`, `profile_id`, `documents.status = 'READY'`) in `dense_search`, `bm25_search`, `load_candidates`, `has_searchable_chunks`; `excerpt` looks the chunk up in the KB of the conversation only; the same 404 for another user's conversation, an unassigned KB, an unknown id and a non-UUID; the admin 403 on writes | Holds. `get_for_admin` is reached only after the role check in `own_conversation` and `readable_conversation`, and `list_all` only behind `require_admin`. The admin can read and delete any conversation (specified: `SPEC-2026-11-17-fase-3-chat`, line 20) and writes no question, retry or rename in another user's (403). Tests: `test_conversation_isolation.py` calls the repository and the retrieval directly. No isolation defect. |
| `backend/app/chat/`: lock order | `start_answer`, `complete_answer` and `restart_answer` lock the conversation row (`FOR KEY SHARE`) before any message; `delete` and the cascade of a KB lock the conversation first; `fail_answer` and `mark_interrupted` write messages only and never lock the conversation row, so they cannot close a cycle | Holds. |
| `backend/app/chat/`: `ActiveAnswers`, SSE, vector search | `run.py` starts one `uvicorn.run` with no `workers`; the note is in `service.py`, `chat.md` and ADR-003; `STREAM_HEADERS` has `X-Accel-Buffering: no` (`chat/routes.py:75`); `KEEPALIVE_SECONDS = 15.0` (`service.py:89`) under the 60 s of Nginx; `dense_search` filters `Chunk.profile_id == profile.id` and orders by `embedding_expression(profile)` (`embedding::vector(N)`), as `verify_indexes` does | Holds; the single process is a known limit (part 5). |
| `backend/app/chat/`: logs and destinations | all the `logger` calls of the module and of `ingestion/` and `worker.py` read: each writes ids, counts, durations, codes or `type(error).__name__`, except one (BUG-006); the destinations of the chat content (embedding, generation, rewriting, reranking) | Holds (tests: `test_chat_logging_privacy.py`, `test_chat_steps_privacy.py`). The code sends to those four and no other; the docstring of `_embed` says otherwise: [BUG-002](../bugs/2026-11-21-embed-docstring-says-ollama-is-the-only-destination.md). |
| `backend/app/app_settings/` | `PUT` and `GET /api/admin/settings` behind `require_admin` at router level (before the body); `StrictBool` and `null` refused; 409 `service_not_configured` when a step is switched on and its service is not configured, with nothing saved; switching off always allowed; the single row `id = 1` | Holds. No defect. |
| `backend/migrations/` | the six versions; `git log --diff-filter=M` on each | None was edited after being merged: the only modification, of `0004_chunks.py` (commit `f27b1d4`), is inside the same task and branch (T-03 of phase 2) as its creation (`249e3da`). The ER diagrams of the pages equal the migrations. No defect. |
| `compose.yaml` | secrets, ports, health checks, the least-privilege split of the environment of the worker | Only `nginx` publishes a port; the password comes from `.env` and is required (`:?`); the worker gets only the common part plus its own variables; every variable of `.env.example` reaches the service that reads it; the reranker is behind a profile. No defect. |
| `compose.rerank-gpu.yaml` | image, device reservation | Replaces the image of the same service (one reranker at a time), NVIDIA only, as the page says. No defect. |
| `nginx/` | the two server templates, the entrypoint, the proxy snippet, the upload limit | `X-Forwarded-Proto` always overwritten with the scheme of the server; 1 MB body limit except the upload route (`MAX_UPLOAD_SIZE_MB` + 1) with the JSON `file_too_large` body; TLS 1.2 and 1.3 only when certificate, key and `HTTPS_PORT` are all there; the entrypoint refuses an invalid size. No defect. |
| `.github/workflows/` | `verify.yml` | Runs `scripts/verify.py` on every PR and on `main`, with Chromium for the e2e step, on `ubuntu-24.04`, and uploads the reports. No defect. |

**Observations, for the owner's information: no rule or requirement of phases 1-4 is broken, so none is a defect and none has a bug file.**
- Nginx sends no security headers such as HSTS or `X-Content-Type-Options` (no spec asks for them); `verify.yml` has no `permissions:` block (the default token permissions apply) and its actions are pinned by tag, not by commit; the backend and worker images run as `root`; `GET /api/health` answers without a session and says which service is down (documented as "anyone"); the `require_json` check, being an application-wide dependency, answers 422 for a wrong `Content-Type` before the 401 or 403 of a route (documented in the pages).
- `require_json` (`backend/app/auth/deps.py:71-72`) checks the `Content-Type` only when the header is present: a POST, PUT, PATCH or DELETE with no `Content-Type` passes (the retry routes rely on it, and `ingestion.md` says so). What stops a request of another site that has no `Content-Type` is the `SameSite=Lax` cookie (`auth/routes.py:45`), which a cross-site POST does not carry, and the preflight of the browser for a request that needs custom headers. This audit did not verify that these are the only protection, and did not test a cross-site request.
- Neither `LoginIn.password` (`backend/app/auth/routes.py:19`) nor the body of a new user or of a password reset (`backend/app/users/schemas.py:15, 24`) has a maximum length; the only limit is the 1 MB body limit of Nginx (`nginx/default.conf.template`, `client_max_body_size 1m`), so a password of up to 1 MB reaches Argon2.
- The HTTP to HTTPS redirect takes the host from the request header: `return 301 https://$host${HTTPS_PORT_SUFFIX}$request_uri;` (`nginx/tls.conf.template:7`), with `server_name _`. A request with a forged `Host` is redirected to that host (it is the usual behaviour of this form of redirect; it needs a client that sends a forged header to itself).
- The wording of `AGENTS.md` before this task ("the admin ... writes in none of another user's") was true for questions, retries and renames but not for the delete, which the spec gives to the admin. It is corrected by the update of `AGENTS.md` in this pull request ("reads and deletes every conversation, but asks, retries or renames in none of another user's"). That update was proposed at the close of phase 5, in the pull request, and the owner selected it (with this correction).
- Login attempts are not limited: this is not an observation but a limit that the owner accepted, listed in part 5.

**What was not checked.** The security of the dependencies (no `npm audit` or `pip audit` was run: they are not part of REQ-013 and the network of the environment is limited); the frontend outside `ConversationView.tsx`, `ConversationsPage.tsx`, `SettingsPage.tsx` and `App.tsx` (its tests are in part 1); the behaviour under load; the real Ollama, Docling, generation provider, rewriting service and reranker (the tests use fakes by rule, and the manual run before real use is a known limit); whether every statement of `users/` and `auth/` was read one by one (their routes and roles were read, not each query); the PR descriptions (the comments of PRs #15, #25, #29, #30, #31 and #32 were read: only #30 has any; the others were not).

## 5. Known limits accepted

The limits that the owner accepted in phases 1-4, from the "known limits" sections of the area pages and the comment of PR #30. They are not defects; none is repeated in part 6 unless it is also a finding of this check (BUG-004 is the one that is both).

**`docs/system/fondazioni.md`, section "Known limits of the ingestion (phase 2, T-05 and T-06)"**
- A garbage file for which docling-serve answers 500 ends `docling_error` after three attempts, not `parse_failed`.
- Merged table cells repeat their text in every row they span, and the " | " separators count toward `MIN_EXTRACTED_CHARS`.
- Captions that can only be reached through the children of a picture are not read.
- The shape of Docling's answer rests on its documentation and on fixtures: the live smoke tests are run once by hand before use.
- Three attempts of up to `DOCLING_TIMEOUT_SECONDS` plus the waits can reach the 3600 s job timeout; docling-serve has its own limit for a synchronous conversion.
- One worker (accepted for v1): a second worker would make a document being processed FAILED "Processing was interrupted"; the `worker` service is never scaled.
- The hourly sweep leaves alone any file of `UPLOAD_DIR` that is not named exactly `{uuid}` or `{uuid}.part`.

**`docs/system/ingestion.md`, section "Known limits"**
- One worker (as above).
- `verify_indexes` may be slow on a very large `chunks` table, and a false negative would fail a good document with `verification_failed`.
- `DOCLING_INCLUDE_IMAGES` stays in `.env`: no switch in the Settings page (phase 4 non-scope).
- A Redis failure right after "Riprova" answers 500 and leaves the document QUEUED until the next start of the worker.
- No re-indexing, no OCR, no download or preview of a document, no paging of the list, no document list for the role `user`.
- The attempts and the 3600 s timeout, the garbage-file 500 and the merged cells (as above); the notes for phase 3.

**`docs/system/chat.md`, section "Known limits"**
- A manual run with the real Ollama and a real provider before real use (the tests use fakes only).
- `ActiveAnswers` is single-process (ADR-003): never scale the `backend` service; more processes need a new ADR.
- The residual PENDING window at shutdown (REQ-017), ended as `interrupted` at the next start.
- AC-045 detects only an event-loop block longer than `HEALTH_CHECK_TIMEOUT_SECONDS` + 1 s.
- Documents indexed with an old indexing profile are not searchable: no re-indexing.
- An odd `CHAT_HISTORY_MESSAGES` is accepted (owner's decision); `[n]` inside code in an answer is treated like any other text.
- ChatPage, three low findings of the T-06 review (a stream that closes before the `conversation` event; a question sent while a conversation loads; a `done` event without `message`).
- Admin page «Conversazioni», four low edge cases (a filter that hides the open conversation; a 404 on opening keeps the selection; opening B while the delete of A is in flight; a list request in flight when a delete succeeds).
- `spec.md` of phase 3 says `@@@` for the BM25 operator; the code uses `|||`.
- Non-scope of phases 3 and 4: no evaluation, export, search or paging of conversations, no feedback, no provider other than OpenAI-compatible; no per-KB or per-user switch, no multi-query, HyDE or translation, no rewriting or reranking in the worker, no reranker model inside the image.
- Phase 4: the rewriting is asked when the filtered history is not empty (not on "at least one completed exchange", REQ-005); with `CHAT_HISTORY_MESSAGES` 0 no question is rewritten, with 1 the rewriter gets the last answer alone (owner's decision at the review of T-02, PR #30).
- Phase 4: the low test gaps of T-02 and the 1 MiB cap of `rewrite.py` checked per decoded chunk (owner's comment on PR #30; the test gaps are also [BUG-004](../bugs/2026-11-21-phase-4-t02-tests-do-not-fail-for-three-wrong-implementations.md)).
- Phase 4: the Compose reranker needs Hugging Face at its first start; `MAX_CLIENT_BATCH_SIZE` follows `RERANK_CANDIDATES`, and an external reranker must accept that many texts; AC-025 holds on the worker by argument, not by a test; with the reranker off only `CHAT_CONTEXT_CHUNKS` chunks are loaded; a switch left on whose service was removed from `.env` shows as on and disabled.

**`docs/specs/2026-11-06-fase-1-fondazioni/spec.md`, non-scope and confirmation of the owner (2026-11-06)**
- The limitation of login attempts, the lock of an account and an audit log are out of scope (line 30 of the spec); the owner confirmed that the use is on a company network or behind a firewall and that the limitation of login attempts is deferred (line 169). So there is no rate limit on `POST /api/auth/login`.

**Comment of PR #30** (`SPEC-2026-11-20-fase-4-rerank-rewrite` T-02): the decision on the rewriting condition and the three low test gaps above, both recorded in `chat.md`.

## 6. Defects

Six defects, one file each in `docs/bugs/` (status `open`), in the order of their severity. The severity is also the first word of the `Severity / impact` line of each file. None is high; none shows data of one knowledge base to a user who may not see it.

| ID | Severity | What is wrong | Where | Evidence | Bug file |
|---|---|---|---|---|---|
| BUG-001 | medium | The user guide tells the users that administrators can only read their conversations; they can also delete them | `docs/guide/en/user.md:118`, `docs/guide/it/user.md:118` | `ConversationsPage.tsx:101, 168`; `chat/routes.py:283-300`; `chat.md` Current behaviour; `SPEC-2026-11-17-fase-3-chat` line 20; `admin.md:176-178` says it can delete | [BUG-001](../bugs/2026-11-21-user-guide-says-admin-cannot-delete-conversations.md) |
| BUG-002 | low | The docstring of `_embed` says Ollama is "the only destination of the question"; since phase 4 the question also goes to the rewriting service and, since phase 3, to the generation provider | `backend/app/chat/service.py:466` | `chat.md` "destinations of the chat content"; `_search_text` and `_rerank` in the same file | [BUG-002](../bugs/2026-11-21-embed-docstring-says-ollama-is-the-only-destination.md) |
| BUG-003 | low | The line «Ricerca: …» is a plain paragraph and shows a line break of the rewritten text as a space; the answer keeps it (`pre-wrap`) | `frontend/src/components/ConversationView.tsx:132` (the answer: line 117) | the two lines of code; `rewrite.py` accepts any non-empty text | [BUG-003](../bugs/2026-11-21-ricerca-line-loses-the-line-breaks-of-the-rewritten-text.md) |
| BUG-004 | low | Three tests of T-02 of phase 4 pass with a wrong implementation (owner-accepted gaps, still applicable) | `backend/tests/test_chat_rewrite.py:204` and `:662`; `backend/app/chat/repository.py:462` | comment of PR #30; `chat.md` Known limits; the tests unchanged since | [BUG-004](../bugs/2026-11-21-phase-4-t02-tests-do-not-fail-for-three-wrong-implementations.md) |
| BUG-005 | low | `trace_check.py` reports no test for AC-005 and AC-006 of phase 1 and AC-002 of phase 2: the tests exist, but two test files name two specs, so their short citations do not count | `backend/tests/test_health.py:1-12`, `backend/tests/test_worker_health.py:1-12` | `trace_check.py` exit 1 for both specs (part 2) | [BUG-005](../bugs/2026-11-21-trace-check-misses-the-health-tests-of-phases-1-and-2.md) |
| BUG-006 | low | An unforeseen failure of an upload is logged with `logger.exception` (the message and the traceback), where the pages say only the id and the type of the error are logged | `backend/app/documents/routes.py:127-129` | `ingestion.md:72, 410-411`, `fondazioni.md:115-116`; every other handler logs `type(error).__name__`; `db/session.py:12` sets `hide_parameters=True` (the bound file name is not echoed) but not the `DETAIL` of the database server; no test reads this line | [BUG-006](../bugs/2026-11-21-upload-failure-logs-the-exception-with-its-message.md) |

The checks of parts 1 to 4 that are not named above found no defect: the full verification, `trace_check.py` for phases 3 and 4, the isolation of knowledge bases and conversations, the lock order, the SSE headers, the profile filter of the vector search, the retention of the originals, the switches API, the migrations, Compose, Nginx and the workflow.
