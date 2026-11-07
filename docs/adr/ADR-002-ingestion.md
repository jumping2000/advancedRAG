# ADR-002: Dipendenze e infrastruttura della fase 2 (ingestion)

- Status: accepted
- Date: 2026-11-07
- Spec: SPEC-2026-11-07-fase-2-ingestion

## Context
La fase 2 porta i documenti nelle KB con un'elaborazione asincrona. Il documento di architettura fissa Redis + RQ per la
coda, Docling e Ollama come servizi esterni e PostgreSQL con pgvector e pg_search; `AGENTS.md` chiede che ogni dipendenza
oltre quelle di ADR-001 sia approvata e motivata, e che un nuovo componente di infrastruttura abbia un ADR.

## Decision
Dipendenze del backend (runtime):
- `rq>=3.0` (verificata la 3.0.0): coda dei job su Redis, già prevista dall'architettura; si usa il client per accodare
  e `SimpleWorker` per eseguirli nel processo `worker` (ADR-001 già include `redis`).
- `python-multipart>=0.0.33`: FastAPI la richiede per leggere un upload `multipart/form-data`; è l'unica via per `UploadFile`.

Nessuna dipendenza nuova per il frontend (l'upload usa `fetch` e `FormData`) né per i test (i server finti usano la libreria
standard e `httpx`, già presenti).

Infrastruttura:
- Servizio Compose `worker`: stessa immagine del backend, comando `python -m app.worker`, un job alla volta; condivide con
  il backend il volume `uploads` (`UPLOAD_DIR`) e ha un controllo di salute basato sull'heartbeat in Redis.
- Volume `uploads`: ospita gli originali solo tra l'upload e l'esito (e fino a `FAILED_ORIGINAL_RETENTION_DAYS` giorni
  per i documenti FAILED); i file portano come nome l'id del documento.

Scelte che restano nel codice e non richiedono altro ADR: il tipo `vector` come `UserDefinedType` (niente libreria
`pgvector` per Python), l'indice HNSW per profilo con espressione `embedding::vector(N)`, l'indice BM25 `conservative-v1`.

## Consequences
- Il Compose passa da quattro a cinque servizi; `test_stack.py` e `test_health.py` della fase 1 si aggiornano (spec,
  `## Revisions`).
- Aggiungere un'altra libreria o un altro servizio richiede di chiedere prima; cambiare il parser, il modello di embedding
  o il chunker produce un nuovo profilo di indicizzazione, non una modifica dei chunk esistenti.
- Alternative scartate: `fastapi.BackgroundTasks` (nessun isolamento dal traffico interattivo, si perde al riavvio); il
  `Retry` di RQ (rifarebbe l'intero job); la libreria `pgvector` per Python (una dipendenza per un solo tipo).
