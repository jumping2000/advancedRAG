# ADR-004: Reranker e riscrittura della domanda come passi facoltativi della risposta (fase 4)

- Status: accepted
- Date: 2026-11-20
- Spec: SPEC-2026-11-20-fase-4-rerank-rewrite

## Context
La fase 4 aggiunge due passi alla risposta della chat (REQ-005, REQ-008): la riscrittura della domanda in una domanda
autonoma e il reranking dei candidati della fusione RRF. La constitution chiede provider sostituibili per configurazione,
un'immagine CPU o GPU per il reranker, mai entrambe, e un ADR per ogni nuovo componente di infrastruttura. I due passi
si accendono e spengono senza riavvio da una pagina admin (REQ-003). Il backend è un solo processo (ADR-003).

## Decision
- I due passi girano nel processo del backend, dentro `_answer` di `app/chat/service.py`, tra l'evento `conversation` e
  il primo `delta`; i keepalive di ADR-003 coprono l'attesa. Ognuno ha un timeout proprio e un ripiego (domanda
  originale, ordine RRF) che non fa fallire la risposta.
- Riscrittura: un client OpenAI-compatible senza streaming (`POST /chat/completions`, `stream: false`) con variabili
  proprie `REWRITE_*`, separate da quelle della generazione.
- Reranker: un client con due adattatori scelti da `RERANK_API`: `tei` (Hugging Face text-embeddings-inference,
  `{query, texts, truncate}`) e `cohere` (`{model, query, documents, top_n}`, usato da Cohere, Jina, Infinity e altri).
- Servizio nel Compose: `reranker`, immagine Hugging Face text-embeddings-inference nel profilo `rerank`, modello
  `BAAI/bge-reranker-v2-m3` (`RERANK_COMPOSE_MODEL`) scaricato al primo avvio nel volume `reranker-models`, nessuna porta
  sull'host. `compose.rerank-gpu.yaml`, sullo stesso modello di `compose.tls.yaml`, sostituisce l'immagine con quella GPU
  e riserva una GPU NVIDIA: il servizio è sempre uno solo. I tag esatti delle due immagini sono fissati da T-04 dopo la
  verifica sul registro e annotati qui.
- Interruttori: tabella `app_settings` in PostgreSQL con una sola riga (`id = 1`), creata spenta dalla migrazione 0006;
  ogni risposta legge gli interruttori una volta all'inizio.
- Nessuna dipendenza Python o npm nuova: i client usano `httpx` (ADR-001).

## Alternatives
- Due profili `rerank-cpu` e `rerank-gpu`: con due `--profile` partirebbero entrambe le immagini.
- Modello incluso nell'immagine: funziona senza Internet, ma l'immagine pesa alcuni GB e il build diventa nostro.
- Interruttori in Redis o nel `.env`: Redis non è durevole né migrato; il `.env` richiede un riavvio.
- Riscrittura con il client della generazione: lo stesso provider e lo streaming non servono; un provider separato è
  una scelta dell'owner.
- Reranker nel worker RQ: la risposta aspetterebbe la coda dell'ingestion (ADR-002).

## Consequences
- Nel caso peggiore il primo frammento arriva fino a `REWRITE_TIMEOUT_SECONDS` più `RERANK_TIMEOUT_SECONDS` dopo
  (45 s con i valori predefiniti).
- Senza accesso a Hugging Face al primo avvio il reranker del Compose non diventa sano: la chat ripiega su RRF e la
  salute mostra `rerank: unreachable`.
- La GPU richiede NVIDIA Container Toolkit sull'host; l'immagine GPU predefinita vale per una famiglia di GPU, le altre
  richiedono di cambiare il tag nel file aggiuntivo.
- Più processi del backend restano esclusi (ADR-003); gli interruttori in PostgreSQL non ne sono un ostacolo.
