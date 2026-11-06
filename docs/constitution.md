# Constitution
- Stack and versions: Python 3.12 + FastAPI; React + TypeScript (Node.js LTS); PostgreSQL via ParadeDB (pgvector +
  pg_search); Redis + RQ; Nginx; Docker Compose. Ollama (embeddings) and Docling (parsing) are external services.
- Architecture: modular monolith; embedding, generation, query rewriting, retrieval and reranking providers behind
  interfaces and replaceable by configuration. No Kafka, Kubernetes or service mesh in v1.
- Isolation: every retrieval and every document or conversation access is bound to one KB and checked against the
  caller's authorization in the backend and in the SQL query.
- Data minimization: only chunks, vectors and metadata are kept; original files are deleted after ingestion.
  No content or question leaves the installation unless the admin configured an external provider explicitly.
- Compute: every local component runs on CPU; components that run models (reranker) choose CPU or GPU images by
  configuration, never both at the same time.
- Security: no secrets in the code or in git (`.env` is ignored); passwords stored only as hashes; inputs validated.
- Tests: every acceptance criterion has at least one test that cites its ID; automated tests use fakes for Ollama,
  Docling and OpenAI.
- Dependencies: a new dependency is asked first and justified in the PR; a new infrastructure component needs an ADR.
- Portability: project scripts run on Windows and Linux.
