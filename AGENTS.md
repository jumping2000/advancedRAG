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
- A signed-in page shows its load errors without `role="alert"`: the header language selector owns the alert.

## Rules
- Do not modify: applied database migrations already merged into `main` (`backend/migrations/versions/`): add a new one
- Sensitive areas: `backend/app/auth/`, `backend/app/documents/` (upload validation, file storage), `backend/migrations/`,
  `compose.yaml`, `nginx/`, `.github/workflows/`
- Dependencies: only those of `docs/adr/ADR-001-toolchain-fase-1.md` and `docs/adr/ADR-002-ingestion.md`; any other is
  asked first.
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
- Automated tests never call the real Ollama, Docling or OpenAI: they use fakes; real services only in opt-in live tests.

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

## Documentation
- Specs in `docs/specs/<id>/`. Read `spec.md` and `plan.md` before writing code.
- Non-trivial bugs: `docs/bugs/`. Current behaviour per area: `docs/system/`.
