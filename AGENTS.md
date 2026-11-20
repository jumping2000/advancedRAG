# AGENTS.md

## Project
advancedRAG: Retrieval-Augmented Generation over several Knowledge Bases (KB), Italian and English. Modular monolith:
FastAPI backend, RQ ingestion worker, React + TypeScript frontend, PostgreSQL (ParadeDB: pgvector + pg_search), Redis,
Nginx, Docker Compose; Ollama and Docling are external services.

## Commands
- Setup: `uv sync --project backend` and `npm ci --prefix frontend`; playbook scripts: `python3` (`python` in the skills)
- Test services (Postgres and Redis for backend tests): `docker compose -f compose.test.yaml up -d --wait`
- Full verification (definition of "done"): `python3 scripts/verify.py`; only while working: `--fast`
- Single test: `uv run --project backend pytest <path>::<name>` or `npm --prefix frontend run test -- <file>`
- Local run: `docker compose up --build` (ingestion worker: service `worker`, `python -m app.worker`); HTTPS:
  `-f compose.yaml -f compose.tls.yaml` plus `nginx/certs/cert.pem` and `key.pem`; reranker (ADR-004): `--profile rerank`,
  on a NVIDIA GPU also `-f compose.rerank-gpu.yaml` (one reranker image at a time)
- E2E only (full stack, part of verify): `uv run --directory backend pytest -m e2e`; Chromium: `uv run --frozen --directory
  backend playwright install --with-deps chromium` (CI), or `PLAYWRIGHT_CHROMIUM_EXECUTABLE` set to an installed one

## Conventions
- Architecture source: `docs/specs/` (one spec per phase) and `docs/adr/`. Docs in English headings as required by the
  playbook scripts; spec bodies may be in Italian. No shell-specific commands in scripts (Windows and Linux).
- Committed Dockerfiles stay free of proxy or corporate CA patches (local only); a lock around Docker (e.g. `flock`) is
  needed only on a shared local daemon, never in CI.
- Each e2e test file uses its own Compose project name and host ports and tears down with `down -v`; wait for
  conditions, never sleep. Host ports in use: 18080, 18180, 18181, 18280, 18380, 18480, 18543, 18544, 18580, 18680,
  18780, 18880, 18980; next free: 19080.
- E2E fakes: profiles of `backend/tests/e2e/compose.fakes.yaml`, `generation` (`E2E_GENERATION_SCRIPT`: a JSON list, a
  string is a fragment, a number a pause in s; fast unless the test is about timing), `rewrite`, `rerank` (`E2E_*` URLs); a
  fake of a profiled service of `compose.yaml` overrides it (same name and profile), so the real image never starts.
- The nginx image build runs `tsc` on the test files too: a locked frontend test that needs a new type gets it in a
  skeleton commit first, or every e2e fails at the build.
- API errors are `{"error": {"code", "message"}}`; an id that is not a UUID gets the same 404 as an unknown id
  (`user_not_found`, `kb_not_found`); admin routes put `require_admin` before path ids and body schema.
- Name uniqueness is left to unique indexes on `lower()` with names trimmed first; `IntegrityError` 23505 → 409.
- Backend tests never set an ORM object loaded in the test session to a value the database refuses (autoflush).
- A signed-in page shows its load errors without `role="alert"`: the header language selector owns the alert (except the
  KB-not-found message of the chat page, which keeps the alert of phase 1: `KnowledgeBaseAdminPage.test.tsx`, AC-024).
- Vitest: polling tests call `vi.useFakeTimers({ shouldAdvanceTime: true })` first; only the upload (`FormData`, no
  `Content-Type`) and `api.stream` send `X-Requested-With`: no test expects it on `api.delete`, `api.put`, `api.patch`.
- SSE responses send `X-Accel-Buffering: no` and a keepalive comment at least every 15 s (Nginx reads with a 60 s timeout).
- Lock order of the chat: every writer that takes a conversation row locks it BEFORE any of its messages (`start_answer`,
  `complete_answer`, `restart_answer`, the delete and the cascade of a KB); the opposite order is a deadlock.
- `ActiveAnswers` (`app.state.active_answers`) is in memory, single-process (ADR-003): one backend process, or a new ADR.
- A `user` gets the same 404 for the conversation of another user, of an unassigned KB, an unknown id and an id that is not
  a UUID, never 403; the admin reads every conversation and writes in none of another user's (403).
- `trace_check.py` on a whole spec needs the test folders as ONE flag with two paths: `--tests backend/tests frontend/src`.

## Rules
- Do not modify: applied database migrations already merged into `main` (`backend/migrations/versions/`): add a new one
- Sensitive areas: `backend/app/auth/`, `backend/app/documents/` (upload validation, file storage), `backend/app/chat/`
  (isolation of conversations and sources), `backend/app/app_settings/` (admin switches), `backend/migrations/`,
  `compose.yaml`, `compose.rerank-gpu.yaml`, `nginx/`, `.github/workflows/`
- Dependencies: only those of `docs/adr/ADR-001-toolchain-fase-1.md`, `docs/adr/ADR-002-ingestion.md` and
  `docs/adr/ADR-004-rerank-rewrite.md` (reranker image, no new library); any other is asked first.
- A behaviour change needs a test written from the ACs before the code; no task is done without a green `verify.py`.
- Ambiguity or missing requirements: stop and ask, do not assume.
- Behaviour changes: update the spec or the area page in `docs/system/` and the guides `docs/guide/en|it/` in the same PR.
- Choices and the merge question are asked as options to select (`sdd-start/references/interaction.md`).
- Every access to a KB, its documents or conversations goes through `KnowledgeBaseRepository.list_for`/`get_for` or
  a query joined with `kb_members` in the same SQL statement, with an isolation test that calls it directly.
- Uploaded originals live only in `UPLOAD_DIR`, named by the document id, deleted at READY or UNSUPPORTED, with the
  document or its KB, and `FAILED_ORIGINAL_RETENTION_DAYS` after FAILED; logs never hold document content, names or vectors.
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
- `docs/system/fondazioni.md`: stack (Compose, Nginx/TLS, health, optional reranker), login and sessions, users,
  knowledge bases and per-KB isolation (`KnowledgeBaseRepository`), admin switches and page «Impostazioni», UI and i18n
- `docs/system/ingestion.md`: documents and ingestion (upload API and validation, RQ worker, Docling and Ollama pipeline,
  chunks, indexing profiles and indexes, retention of originals, Documents page), with the notes for phase 3
- `docs/system/chat.md`: chat (vector + BM25 + RRF, optional rewriting and reranker, SSE through Nginx, conversations,
  messages, sources and «Ricerca: …», admin read-only page "Conversazioni", `ActiveAnswers`), with its known limits

## Documentation
- Specs in `docs/specs/<id>/` (read `spec.md` and `plan.md` before code); bugs in `docs/bugs/`; areas in `docs/system/`.
