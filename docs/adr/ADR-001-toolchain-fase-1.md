# ADR-001: Toolchain e dipendenze della fase 1

- Status: accepted
- Date: 2026-11-06
- Spec: SPEC-2026-11-06-fase-1-fondazioni

## Context
Il repository parte vuoto. Il documento di architettura fissa FastAPI, React + TypeScript, PostgreSQL (ParadeDB),
Redis + RQ, Nginx e Docker Compose, ma non le librerie e gli strumenti. `docs/constitution.md` chiede di motivare ogni
nuova dipendenza.

## Decision
Backend (Python 3.12, dipendenze gestite con `uv`):
- `fastapi`, `uvicorn`: framework e server ASGI previsti dall'architettura.
- `sqlalchemy[asyncio]` + `asyncpg`: accesso async a PostgreSQL con query esplicite, filtri per KB scritti in SQL.
- `alembic`: migrazioni versionate dello schema; quelle già entrate in `main` non si modificano.
- `pydantic-settings`: configurazione da `.env` validata all'avvio (errori che nominano la variabile).
- `argon2-cffi`: hash Argon2id delle password (REQ-006).
- `redis`: sessioni lato server; lo stesso Redis servirà a RQ nella fase 2.
- `httpx`: health check di Ollama e Docling con timeout; client dei test API.
- Sviluppo: `ruff` (format e lint), `mypy` (tipi), `pytest` + `pytest-asyncio` (test), `pytest-playwright` (e2e UI).

Frontend (Node.js LTS, dipendenze gestite con `npm`):
- `react`, `react-dom`, `react-router-dom`: UI e navigazione tra le pagine.
- `i18next` + `react-i18next`: testi in italiano e inglese, cambio lingua senza ricaricare la pagina (REQ-011).
- Sviluppo: `vite`, `typescript`, `vitest` + `@testing-library/react` + `jsdom` (test), `eslint`, `prettier`.
- Supporto a questi strumenti (autorizzati con T-01, nessun codice in produzione): `@vitejs/plugin-react`,
  `@types/react`, `@types/react-dom`, `typescript-eslint`, `@eslint/js`, `eslint-plugin-react-hooks`, `globals`,
  `@testing-library/jest-dom`.

Infrastruttura: immagini `paradedb/paradedb`, `redis`, `nginx`, `python:3.12-slim`, `node` (solo per la build), con
versione fissata nel Compose e nei Dockerfile.

## Consequences
- Una sola toolchain per lato: `uv` e `npm`, entrambi eseguibili su Windows e Linux.
- I test di backend e gli e2e richiedono Docker (Postgres, Redis e stack completo); `verify --fast` non li esegue.
- Aggiungere una libreria non elencata qui richiede di chiedere prima e motivarla nella PR; un nuovo componente
  infrastrutturale richiede un nuovo ADR.
