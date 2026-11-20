# AGENTS.md

## Project
advancedRAG: web platform for Retrieval-Augmented Generation over several Knowledge Bases (KB), Italian and English.
Modular monolith: Python + FastAPI backend, RQ worker for ingestion, React + TypeScript frontend, PostgreSQL
(ParadeDB: pgvector + pg_search), Redis, Nginx, deployed with Docker Compose. Ollama and Docling are external services.

## Commands
- Python for the playbook scripts: `python3` (written as `python` in the skills)
- Setup: `uv sync --project backend` and `npm ci --prefix frontend`
- Test services (Postgres and Redis for backend tests): `docker compose -f compose.test.yaml up -d --wait`
- Full verification (definition of "done"): `python3 scripts/verify.py`
- Quick verification (only while working): `python3 scripts/verify.py --fast`
- Single test: `uv run --project backend pytest <path>::<name>` or `npm --prefix frontend run test -- <file>`
- Local run: `docker compose up --build` (from phase 1); with HTTPS:
  `docker compose -f compose.yaml -f compose.tls.yaml up --build` plus `nginx/certs/cert.pem` and `key.pem`
- With the reranker (ADR-004): `docker compose --profile rerank up --build`; on a NVIDIA GPU add
  `-f compose.rerank-gpu.yaml` (one reranker image at a time)
- Worker of the ingestion queue: Compose service `worker` (`python -m app.worker`)
- End-to-end tests only (full stack, part of verify): `uv run --directory backend pytest -m e2e`
- Chromium for the Playwright e2e: `uv run --frozen --directory backend playwright install --with-deps chromium` (CI);
  where a Chromium is already installed, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to it instead

## Conventions
- Architecture source: `docs/specs/` (one spec per phase) and `docs/adr/`.
- Docs in English headings as required by the playbook scripts; spec bodies may be in Italian.
- No shell-specific commands in scripts: they must run on Windows and Linux.
- Committed Dockerfiles stay free of proxy or corporate CA patches: such a patch is local only, never committed.
  A lock around Docker (e.g. `flock`) is needed only on a shared local daemon, never in CI.
- Each e2e test file uses its own Compose project name and host ports and tears down with `down -v`; wait for
  conditions, never sleep.
- API errors are `{"error": {"code", "message"}}`; an id that is not a UUID gets the same 404 as an unknown id
  (`user_not_found`, `kb_not_found`); admin routes put `require_admin` before path ids and body schema.
- Name uniqueness is left to unique indexes on `lower()` with names trimmed first; `IntegrityError` 23505 → 409.
- Backend tests never set an ORM object loaded in the test session to a value the database refuses (autoflush).
- A signed-in page shows its load errors without `role="alert"`: the header language selector owns the alert (except the
  KB-not-found message of the chat page, which keeps the alert of phase 1: `KnowledgeBaseAdminPage.test.tsx`, AC-024).
- Polling tests in Vitest call `vi.useFakeTimers({ shouldAdvanceTime: true })` before rendering.
- Multipart `fetch` calls set no `Content-Type` (the browser adds the boundary): `FormData` plus `X-Requested-With`.
- SSE responses send `X-Accel-Buffering: no` and a keepalive comment at least every 15 s (Nginx reads with a 60 s timeout).
- Host ports in use by the e2e tests: 18080, 18180, 18181, 18280, 18380, 18480, 18543, 18544, 18580, 18680, 18780;
  next free: 18880.
- Lock order of the chat: every writer that takes a conversation row locks it BEFORE any of its messages (`start_answer`,
  `complete_answer`, `restart_answer`, the delete and the cascade of a KB); the opposite order is a deadlock.
- `ActiveAnswers` (`app.state.active_answers`) is in memory and single-process (ADR-003): never run more than one backend
  process or replica without a new ADR (shared registry, review of `mark_interrupted`).
- A `user` gets the same 404 for the conversation of another user, of an unassigned KB, an unknown id and an id that is not
  a UUID, never 403; the admin reads every conversation and writes in none of another user's (403).
- The e2e fake generation provider is the profile `generation` of `backend/tests/e2e/compose.fakes.yaml`, with
  `E2E_GENERATION_URL` and `E2E_GENERATION_SCRIPT` (a JSON list: a string is a fragment, a number a pause in seconds); use a
  fast script unless the test is about timing.
- Only the multipart upload and `api.stream` send `X-Requested-With`; `api.delete`, `api.put` and `api.patch` do not: a
  frontend test must not expect the header on them.
- `trace_check.py` on a whole spec needs the test folders as ONE flag with two paths: `--tests backend/tests frontend/src`.

## Rules
- Do not modify: applied database migrations already merged into `main` (`backend/migrations/versions/`): add a new one
- Sensitive areas: `backend/app/auth/`, `backend/app/documents/` (upload validation, file storage), `backend/app/chat/`
  (isolation of conversations and sources), `backend/app/app_settings/` (admin switches), `backend/migrations/`,
  `compose.yaml`, `compose.rerank-gpu.yaml`, `nginx/`, `.github/workflows/`
- Dependencies: only those of `docs/adr/ADR-001-toolchain-fase-1.md`, `docs/adr/ADR-002-ingestion.md` and
  `docs/adr/ADR-004-rerank-rewrite.md` (reranker image, no new library); any other is asked first.
- Every behaviour change requires a test, written from the acceptance criteria before the code.
- Never declare a task completed without having run `python3 scripts/verify.py` successfully.
- Ambiguity or missing requirements: stop and ask, do not assume.
- Behaviour changes: update the spec or the area page in `docs/system/` in the same PR.
- Choices and the merge question are asked as options to select (`sdd-start/references/interaction.md`).
- Every access to a KB, its documents or conversations goes through `KnowledgeBaseRepository.list_for`/`get_for` or
  a query joined with `kb_members` in the same SQL statement, with an isolation test that calls it directly.
- Uploaded originals live only in `UPLOAD_DIR`, named by the document id; they are deleted at READY or UNSUPPORTED, when
  the document or its KB is deleted, and after `FAILED_ORIGINAL_RETENTION_DAYS` if FAILED. Logs never contain document
  content, file names or vectors.
- Vectors of different indexing profiles never mix: every vector search filters on `profile_id` and uses the profile's HNSW
  expression `embedding::vector(N)`.
- Automated tests never call the real Ollama, Docling, generation provider, rewriting provider or reranker: they use
  fakes; real services only in opt-in live tests.

## Decision rights
A decision no line names counts as "Asks first". The "Never" list only grows: the plugin's rules apply even if a line is
removed here.

### Decides alone (record each one under "Decisions taken alone" in the PR description)
- Internal names and structure within the conventions and the plan's interfaces
- Library calls, algorithms and data structures the plan leaves open
- Test data and test structure; log messages

### Asks first (stop and ask, with options)
- Anything a user can observe that the spec leaves open: values, limits, defaults, messages with business meaning
- Data model or public interface changes beyond the plan
- A new dependency, or a deviation from an ADR
- A change to a file outside the task's files
- Any call to an external service (OpenAI, Ollama, Docling, other APIs) not already configured by the spec

### Never
- Change spec or plan while implementing a task (a change to them is a new design PR); change `AGENTS.md` outside the
  update proposed at the design PR or at the feature's close, after the user selected it
- Modify or weaken a locked test (`Test-Lock`), or any test, to make it pass
- Commit on `main`/`master`; merge a PR without the user selecting the merge; push to `main`
- Return chunks, sources or metadata of a KB the caller is not authorized for, in any API or SQL query

## Areas
<!-- One line per area page, added when a feature closes: `docs/system/<area>.md`: what it covers -->
- `docs/system/fondazioni.md`: stack (Compose, Nginx/TLS, health), login and sessions, users, knowledge bases and
  per-KB isolation (`KnowledgeBaseRepository`), UI pages and i18n
- `docs/system/ingestion.md`: documents and ingestion (upload API and validation, RQ worker, Docling and Ollama pipeline,
  chunks, indexing profiles and indexes, retention of originals, Documents page), with the notes for phase 3
- `docs/system/chat.md`: chat (hybrid retrieval: vector + BM25 + RRF; answers streamed as SSE through Nginx; conversations,
  messages and sources; admin read-only page "Conversazioni"; `ActiveAnswers`), with its known limits

## Documentation
- Specs in `docs/specs/<id>/`. Read `spec.md` and `plan.md` before writing code.
- Non-trivial bugs: `docs/bugs/`. Current behaviour per area: `docs/system/`.
