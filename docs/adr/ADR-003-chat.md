# ADR-003: Generazione in streaming nel backend e fonti senza chiave esterna (fase 3)

- Status: accepted
- Date: 2026-11-17
- Spec: SPEC-2026-11-17-fase-3-chat

## Context
La fase 3 aggiunge la chat con risposte in streaming e citazioni verificabili (REQ-008, REQ-009, REQ-010, REQ-014,
REQ-018, REQ-021). Una generazione su CPU può durare minuti. La risposta deve arrivare al browser attraverso Nginx mentre
viene prodotta, e deve potersi fermare quando il client chiude la connessione o la conversazione viene cancellata. Le
fonti salvate devono sopravvivere alla cancellazione dei documenti senza copiarne il contenuto. La cancellazione dei
documenti della fase 2 blocca la riga del documento e cancella i chunk in cascata.

## Decision
- La generazione gira nel processo del backend, nella richiesta HTTP della domanda: `StreamingResponse` di Starlette con
  `text/event-stream` e quattro eventi (`conversation`, `delta`, `done`, `error`), più un commento `: keepalive` ogni 15
  secondi.
- Il backend resta un solo processo uvicorn. Il registro delle risposte in corso (`ActiveAnswers`, in `app.state`) è in
  memoria e serve a fermare la generazione quando la conversazione viene cancellata. All'avvio, `app/run.py` segna come
  fallite `interrupted` le risposte rimaste `PENDING`.
- Una sola risposta in corso per conversazione, garantita dal database: indice univoco parziale
  `messages (conversation_id) WHERE status = 'PENDING'`.
- Nginx non cambia. La risposta porta `X-Accel-Buffering: no`, che disattiva il buffering di Nginx per quella risposta, e
  il keepalive resta sotto il `proxy_read_timeout` di 60 secondi.
- `message_sources.chunk_id` non ha chiave esterna verso `chunks`. L'estratto si legge con un `LEFT JOIN` vincolato alla
  KB della conversazione; nessuna riga significa «Fonte non più disponibile». Con la fonte si salvano solo i metadati:
  nome del documento, sezione, pagine, rank e punteggio.
- Nessuna dipendenza nuova. Il client del provider usa `httpx` (ADR-001) e il frontend usa `fetch` con `ReadableStream`.

## Alternatives
- Libreria `sse-starlette`: una dipendenza in più per un formato di quattro eventi che `StreamingResponse` copre.
- Generazione nel worker RQ, con gli eventi passati al backend su Redis pub/sub: più parti mobili, e l'unico worker è
  occupato dall'ingestion (ADR-002), che terrebbe ferma la chat anche per minuti.
- Chiave esterna `chunk_id → chunks ON DELETE SET NULL`: ogni cancellazione di documento scriverebbe sulle fonti dentro
  la transazione che blocca il documento, con rischi di lock come quelli già risolti nella fase 2.
- `EventSource` nel browser: fa solo GET, quindi la domanda finirebbe nell'URL e nei log di accesso di Nginx.

## Consequences
- Più processi uvicorn, o più repliche del backend, richiedono di sostituire `ActiveAnswers` con un meccanismo condiviso
  (Redis) e di rivedere `mark_interrupted` all'avvio: è un nuovo ADR.
- Una risposta in corso occupa una connessione del backend per tutta la generazione. Le altre richieste restano servite
  perché tutto è asincrono (REQ-101, AC-045).
- Una fonte può puntare a un chunk che non esiste più: la UI lo mostra come non disponibile, e i metadati salvati
  bastano a dire quale documento era.
- Se Nginx bufferizzasse comunque lo stream (AC-044), il ripiego è una `location` dedicata con `proxy_buffering off` in
  `nginx/` (area sensibile), da approvare.
