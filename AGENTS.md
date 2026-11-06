# AGENTS.md

## Project
advancedRAG: web platform for Retrieval-Augmented Generation over several Knowledge Bases (KB), Italian and English.
Modular monolith: Python + FastAPI backend, RQ worker for ingestion, React + TypeScript frontend, PostgreSQL
(ParadeDB: pgvector + pg_search), Redis, Nginx, deployed with Docker Compose. Ollama and Docling are external services.

## Commands
- Python for the playbook scripts: `python3` (written as `python` in the skills)
- Setup: defined by the first task of phase 1 (backend and frontend toolchains)
- Full verification (definition of "done"): `python3 scripts/verify.py`
- Quick verification (only while working): `python3 scripts/verify.py --fast`
- Single test: defined by the first task of phase 1
- Local run: `docker compose up --build` (from phase 1)

## Conventions
- Architecture source: `docs/specs/` (one spec per phase) and `docs/adr/`.
- Docs in English headings as required by the playbook scripts; spec bodies may be in Italian.
- No shell-specific commands in scripts: they must run on Windows and Linux.

## Rules
- Do not modify: applied database migrations already merged into `main` (`backend/migrations/versions/`): add a new one
- Sensitive areas: `backend/app/auth/`, `backend/migrations/`, `compose.yaml`, `nginx/`, `.github/workflows/`
- Every behaviour change requires a test, written from the acceptance criteria before the code.
- Never declare a task completed without having run `python3 scripts/verify.py` successfully.
- Ambiguity or missing requirements: stop and ask, do not assume.
- Behaviour changes: update the spec or the area page in `docs/system/` in the same PR.
- Choices and the merge question are asked as options to select (`sdd-start/references/interaction.md`).
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
- none yet

## Documentation
- Specs in `docs/specs/<id>/`. Read `spec.md` and `plan.md` before writing code.
- Non-trivial bugs: `docs/bugs/`. Current behaviour per area: `docs/system/`.
