# SPEC-2026-11-20-fase-4-rerank-rewrite: Fase 4 – Reranker, riscrittura della domanda e impostazioni

- Status: implemented
- Owner: jumping2000

## Problem and goal
Nella fase 3 la ricerca usa la domanda così com'è e il contesto prende i primi chunk nell'ordine della fusione RRF. Una
domanda di seguito come «e il prezzo?» non contiene le parole che la ricerca dovrebbe trovare. L'ordine RRF, inoltre,
non confronta direttamente la domanda con il testo dei chunk. La fase 4 aggiunge due passi facoltativi:
- la riscrittura della domanda in una domanda autonoma, usando la conversazione, e solo per la ricerca;
- un reranker che riordina i primi candidati della fusione prima di scegliere i chunk del contesto.

Entrambi si accendono e spengono da una pagina «Impostazioni» dell'admin. Con entrambi spenti la chat resta quella della
fase 3.

Criterio di successo: su uno stack con un servizio di riscrittura e un reranker raggiungibili e i due interruttori
accesi, una domanda di seguito viene cercata con la sua forma autonoma, mostrata sotto la risposta come «Ricerca: …», e
il contesto segue l'ordine del reranker. Con un servizio irraggiungibile o in timeout la risposta arriva comunque, con
la domanda originale o con l'ordine RRF. Con gli interruttori spenti, o i servizi non configurati, i test della fase 3
restano verdi e nessuna richiesta parte verso quei servizi. `python3 scripts/verify.py` e la CI sono verdi.

## Users / actors
- Utente (ruolo `user`) e admin che chattano: fanno domande di seguito e vedono quale domanda è stata cercata.
- Admin: configura i servizi nel `.env` e accende o spegne riscrittura e reranker dalla pagina «Impostazioni».
- Servizio di riscrittura: API OpenAI-compatible separata da quella della generazione.
- Reranker: servizio con API `/rerank`, nel formato di Hugging Face text-embeddings-inference (`tei`) o nel formato
  stile Cohere (`cohere`); quello nel Compose è facoltativo e gira su CPU o su GPU.

## Scope
- Included:
  - riscrittura della domanda in una domanda autonoma con un provider OpenAI-compatible proprio, con ripiego sulla
    domanda originale, e la «Ricerca: …» salvata con la risposta e mostrata nella chat;
  - reranker con due formati di API e ripiego sull'ordine RRF;
  - servizio reranker facoltativo nel Compose, con immagine CPU o GPU;
  - la pagina admin «Impostazioni» con due interruttori globali salvati nel database;
  - i controlli `rerank` e `rewrite` in `GET /api/health`;
  - le nuove variabili d'ambiente, i testi in italiano e inglese e i test end-to-end con servizi finti.
- **Non-scope (explicit):** restano fuori dalla fase 4 i punti seguenti.
  - valutazione del retrieval e della generazione (dataset, metriche, confronto con e senza i nuovi passi);
  - interruttore delle immagini di Docling, OCR e descrizione delle immagini (`DOCLING_INCLUDE_IMAGES` resta nel `.env`);
  - interruttori per singola KB o per utente, e altre impostazioni nella pagina;
  - più riformulazioni della stessa domanda (multi-query), HyDE e traduzione della domanda;
  - modello del reranker incluso nell'immagine e funzionamento senza accesso a Hugging Face al primo avvio;
  - riscrittura o reranking in streaming, e reranker o riscrittura nel worker;
  - modifiche all'ingestion, alla pagina Documenti e alle API della fase 2;
  - modifiche alle risposte salvate prima di questa fase.
- **Anti-goals:** la fase 4 fallisce, anche se funziona, se:
  - la riscrittura cambia il senso della domanda e l'utente non può vedere quale domanda è stata cercata;
  - domande, cronologia o chunk arrivano a un servizio che l'admin non ha configurato esplicitamente nel `.env` e
    acceso dalla pagina «Impostazioni».

## Current behaviour and impact
- Today (`docs/system/chat.md`): la domanda viene cercata così com'è. Le due ricerche danno fino a 50 candidati
  ciascuna, la fusione RRF li ordina e il contesto prende i primi `CHAT_CONTEXT_CHUNKS`. Solo la generazione e Ollama
  ricevono contenuti della chat. `GET /api/health` ha i controlli database, redis, ollama, docling, worker e
  `generation`, con gli stati `ok`, `unreachable` e `not_configured`. Non esiste nessuna impostazione modificabile
  dall'interfaccia. Il Compose non ha un reranker.
- Changes:
  - due passi facoltativi nella risposta: riscrittura prima della ricerca, reranking dopo la fusione;
  - una colonna per la domanda cercata nei messaggi e una tabella delle impostazioni (nuova migrazione);
  - API e pagina admin «Impostazioni» con la sua voce di menu, e la riga «Ricerca: …» nella chat e nella pagina
    «Conversazioni»;
  - due controlli in più in `GET /api/health`, un servizio facoltativo nel Compose e nuove variabili d'ambiente.
- Questa spec **rivede la fase 3**:
  - il requisito 006 della fase 3: l'ordine del contesto è quello del reranker quando è attivo;
  - il requisito 016 della fase 3: «Riprova» ripete anche la riscrittura e il reranking;
  - il requisito 103 della fase 3: le destinazioni ammesse per i contenuti della chat;
  - il requisito 002 della fase 3: l'elenco dei controlli di salute.
  La spec della fase 3 riceve la voce in `## Revisions` con la PR di design.
- Must not change:
  - con i due passi spenti o non configurati, il comportamento della chat della fase 3: domanda, streaming, citazioni,
    errori, «Riprova», isolamento e log, protetti da `backend/tests/test_chat_ask.py`, `test_chat_streaming.py`,
    `test_chat_errors.py`, `test_chat_retry.py`, `test_retrieval.py`, `test_rrf.py`, `test_prompt.py`,
    `test_conversations.py`, `test_conversation_isolation.py`, `test_chat_sources.py`, `test_chat_logging_privacy.py`
    e dagli end-to-end `test_chat_ui.py` e `test_conversations_ui.py`;
  - login, KB, ingestion e documenti, protetti dai test delle fasi 1 e 2;
  - lo stack Compose avviato senza profili, protetto da `test_stack.py` e `test_tls.py`.

## Use cases
- UC-001 Fare una domanda di seguito: l'attore, in una conversazione con almeno uno scambio completato, fa una domanda
  che dipende dai messaggi precedenti. Con la riscrittura attiva la ricerca usa la domanda autonoma, che compare sotto la
  risposta come «Ricerca: …». Errori: servizio di riscrittura in errore, in timeout o con una risposta inutilizzabile:
  la ricerca usa la domanda originale e la riga non compare.
- UC-002 Ottenere un contesto riordinato: con il reranker attivo i primi candidati della fusione vengono riordinati
  prima di scegliere i chunk del contesto. Errori: reranker in errore, in timeout o con una risposta non valida: resta
  l'ordine RRF.
- UC-003 Accendere e spegnere i passi (admin): l'admin apre «Impostazioni» e accende o spegne riscrittura e reranker; il
  cambio vale dalla domanda successiva. Errori: un servizio non configurato ha l'interruttore disattivato con la scritta
  «Non configurato».
- UC-004 Avviare il reranker nel Compose (admin): l'admin avvia lo stack con il profilo del reranker, su CPU o con il
  file aggiuntivo per la GPU, e imposta l'URL nel `.env`.

## Requirements
- REQ-001 (functional): Il backend legge la riscrittura da `REWRITE_BASE_URL` (URL di base di un'API OpenAI-compatible,
  http o https con un host), `REWRITE_MODEL`, `REWRITE_API_KEY` (facoltativa, inviata come `Authorization: Bearer`) e
  `REWRITE_TIMEOUT_SECONDS` (default 30, un numero finito maggiore di 0). Con `REWRITE_BASE_URL` vuota la riscrittura non è configurata.
  Con `REWRITE_BASE_URL` impostata e `REWRITE_MODEL` vuota, o con un URL non valido, il backend non parte e il
  messaggio nomina la variabile.
- REQ-002 (functional): Il backend legge il reranker da `RERANK_BASE_URL` (URL di base, http o https con un host),
  `RERANK_API` (`tei` o `cohere`, default `tei`), `RERANK_MODEL` (obbligatorio con `cohere`, non usato con `tei`),
  `RERANK_API_KEY` (facoltativa, inviata come `Authorization: Bearer`), `RERANK_CANDIDATES` (default 20, tra 1 e
  `RETRIEVAL_CANDIDATES`) e `RERANK_TIMEOUT_SECONDS` (default 15, un numero finito maggiore di 0). Con
  `RERANK_BASE_URL` vuota il reranker non è configurato e `RERANK_API`, `RERANK_MODEL` e `RERANK_CANDIDATES` non
  vengono controllati, così una configurazione della fase 3 (per esempio `RETRIEVAL_CANDIDATES` minore di 20) parte
  come prima. Con `RERANK_BASE_URL` impostata, un valore non valido oppure `cohere` senza `RERANK_MODEL` impedisce
  l'avvio del backend e il messaggio nomina la variabile; un timeout non valido lo impedisce sempre.
- REQ-003 (functional): Le impostazioni `rerank_enabled` e `rewrite_enabled` sono globali, salvate nel database e
  spente finché l'admin non le accende. Un passo è attivo solo se è acceso e configurato. Un cambio vale per ogni
  domanda e ogni «Riprova» che inizia dopo il salvataggio, senza riavvio; una risposta già in corso finisce con lo stato
  che aveva al suo inizio.
- REQ-004 (functional): `GET /api/admin/settings` restituisce, per `rerank` e `rewrite`, `enabled` e `configured`.
  `PUT /api/admin/settings` con `rerank_enabled` e/o `rewrite_enabled` booleani salva i valori e restituisce lo stato
  nuovo. Accendere un passo non configurato risponde 409 `service_not_configured` e non salva nulla; un corpo senza
  nessuno dei due campi, o con un valore non booleano, risponde 422 `validation_error`. Le due rotte sono solo per
  l'admin: 401 senza sessione, 403 `forbidden` per un `user`.
- REQ-005 (functional): Con la riscrittura attiva e una conversazione che ha almeno uno scambio completato prima della
  domanda, il backend chiede al servizio di riscrittura, senza streaming, una domanda autonoma.
  - La richiesta contiene la cronologia scelta con le regole di REQ-006 della fase 3 e la domanda.
  - Le istruzioni chiedono di restituire solo la domanda, nella sua lingua, senza rispondere, senza aggiungere
    informazioni e senza cambiarne il senso.
  - Il testo ricevuto, senza spazi iniziali e finali, è la domanda cercata: si usa per il vettore, per la BM25 e per il
    reranker.
  - Il modello di generazione riceve sempre la domanda originale.
  - La prima domanda di una conversazione non viene riscritta, e una KB senza chunk ricercabili (REQ-007 della fase 3)
    non chiama il servizio.
- REQ-006 (functional): Se il servizio di riscrittura è irraggiungibile, risponde con un errore, supera
  `REWRITE_TIMEOUT_SECONDS`, oppure restituisce un testo vuoto o più lungo di `CHAT_MAX_QUESTION_CHARS`, la ricerca usa
  la domanda originale e la risposta continua senza errori per l'utente.
- REQ-007 (functional): La risposta salva in `search_query` la domanda cercata quando è stata riscritta e differisce
  dall'originale (confronto senza spazi iniziali e finali e senza distinguere maiuscole e minuscole); altrimenti
  `search_query` è vuota. Il messaggio restituito dalle API e dall'evento `done` contiene `search_query`. La chat e la
  pagina «Conversazioni» mostrano sotto la risposta la riga «Ricerca: <domanda cercata>» quando `search_query` non è
  vuota.
- REQ-008 (functional): Con il reranker attivo, i primi `RERANK_CANDIDATES` chunk della fusione RRF vengono inviati al
  reranker con la domanda cercata.
  - Il formato `tei` è `POST {RERANK_BASE_URL}/rerank` con `{query, texts, truncate: true}` e risposta
    `[{index, score}]`: `truncate` fa accorciare al reranker i testi oltre il limite del modello invece di rifiutarli.
  - Il formato `cohere` è `POST {RERANK_BASE_URL}/rerank` con `{model, query, documents, top_n}` e risposta
    `{results: [{index, relevance_score}]}`.
  - Il contesto prende i primi `CHAT_CONTEXT_CHUNKS` di questo ordine: i candidati inviati, per punteggio del reranker
    decrescente (a parità, l'ordine RRF), seguiti dagli altri chunk della fusione nell'ordine RRF.
  - Con un solo candidato il reranker non viene chiamato.
  - Le fonti salvate seguono l'ordine finale.
- REQ-009 (functional): Se il reranker è irraggiungibile, risponde con un errore, supera `RERANK_TIMEOUT_SECONDS`, oppure
  restituisce una risposta non valida, il contesto segue l'ordine RRF e la risposta continua senza errori per l'utente.
  Una risposta non valida è un indice fuori dai candidati o ripetuto, un candidato mancante, o un punteggio non numerico.
- REQ-010 (functional): «Riprova» ripete la riscrittura e il reranking con le impostazioni attive al momento del nuovo
  tentativo e aggiorna `search_query` della risposta.
- REQ-011 (functional): `GET /api/health` aggiunge i controlli `rewrite` e `rerank`, che dipendono dalla configurazione
  e non dagli interruttori. Ognuno è `not_configured` se il servizio non è configurato, altrimenti `ok` se risponde 2xx
  entro `HEALTH_CHECK_TIMEOUT_SECONDS`, altrimenti `unreachable`. Il controllo usa:
  - `GET {REWRITE_BASE_URL}/models` per la riscrittura;
  - `GET {RERANK_BASE_URL}/health` per il formato `tei`;
  - `GET {RERANK_BASE_URL}/models` per il formato `cohere`.
  Lo stato complessivo è `ok` solo se tutti i controlli sono `ok`, altrimenti `degraded`. Il codice HTTP resta deciso
  solo da database e Redis.
- REQ-012 (functional): L'admin ha la voce di menu «Impostazioni» e la pagina `/admin/settings`.
  - Due interruttori, «Riscrittura della domanda» e «Reranker», mostrano lo stato salvato e lo salvano a ogni cambio.
  - L'interruttore di un servizio non configurato è disattivato, con la scritta «Non configurato».
  - Un salvataggio rifiutato riporta l'interruttore allo stato precedente e mostra il motivo tradotto.
  - Un `user` non vede la voce e la rotta lo porta a `/`; senza sessione la rotta porta a `/login`.
  - I testi sono in italiano e in inglese.
- REQ-013 (functional): Il Compose ha un servizio `reranker` facoltativo, nel profilo `rerank`, con l'immagine CPU di
  Hugging Face text-embeddings-inference e il modello `BAAI/bge-reranker-v2-m3` (variabile `RERANK_COMPOSE_MODEL`),
  scaricato al primo avvio in un volume.
  - Il file aggiuntivo `compose.rerank-gpu.yaml` sostituisce l'immagine con quella GPU e riserva una GPU NVIDIA, così è
    attiva una sola immagine alla volta.
  - Senza il profilo il Compose avvia gli stessi servizi di prima.
  - Il servizio non pubblica porte sull'host.
- REQ-101 (non-functional): Né il backend né il worker scrivono nei log la domanda cercata, la cronologia, i testi
  inviati al reranker, i loro punteggi o le chiavi `REWRITE_API_KEY` e `RERANK_API_KEY`. Le righe dei due passi
  contengono l'id della conversazione e del messaggio, i conteggi, le durate, se il passo è stato usato o se ha fatto da
  ripiego, e il motivo del ripiego (`unreachable`, `http_error`, `timeout`, `invalid_response`).
- REQ-102 (non-functional): I contenuti della chat partono solo verso queste destinazioni:
  - la domanda e la cronologia verso `REWRITE_BASE_URL`, solo con la riscrittura attiva;
  - la domanda cercata e i testi dei candidati verso `RERANK_BASE_URL`, solo con il reranker attivo;
  - verso `OLLAMA_BASE_URL` e `GENERATION_BASE_URL`, come nella fase 3.
  Nessun'altra destinazione riceve contenuti della chat.
- REQ-103 (non-functional): Con la riscrittura e il reranker spenti o non configurati, una domanda produce le stesse
  richieste a Ollama e al provider di generazione, gli stessi eventi e lo stesso messaggio salvato della fase 3, con
  `search_query` vuota.

## Acceptance criteria
- AC-001 [REQ-001]: Given `REWRITE_BASE_URL` impostata e `REWRITE_MODEL` vuota, e poi `REWRITE_BASE_URL` uguale a
  `ftp://x` e a `http://`, when il backend parte, then termina con un messaggio che nomina la variabile; given
  `REWRITE_BASE_URL` vuota, then parte e `rewrite.configured` è false.
- AC-002 [REQ-002]: Given `RERANK_BASE_URL` impostata e `RERANK_API` uguale a `xyz`, poi `cohere` con `RERANK_MODEL`
  vuota, poi `RERANK_CANDIDATES` uguale a 0 e a 51 con `RETRIEVAL_CANDIDATES` 50, poi `RERANK_TIMEOUT_SECONDS` uguale a
  0 e a `inf`, e `REWRITE_TIMEOUT_SECONDS` uguale a `nan`, when il backend parte, then termina con un messaggio che
  nomina la variabile; given `RERANK_BASE_URL` vuota con `RETRIEVAL_CANDIDATES` 10, oppure con `RERANK_API` `cohere` e
  `RERANK_MODEL` vuota, then parte e `rerank.configured` è false.
- AC-003 [REQ-003]: Given un database nuovo, when l'admin legge le impostazioni, then `rerank` e `rewrite` hanno
  `enabled` false; when accende la riscrittura e un utente fa una domanda, then il servizio finto di riscrittura riceve
  la richiesta senza riavvio del backend.
- AC-004 [REQ-003]: Given la riscrittura accesa e poi `REWRITE_BASE_URL` tolta dal `.env` con un riavvio, when si fa una
  domanda di seguito, then nessuna richiesta arriva al servizio e `GET /api/admin/settings` dà `enabled` true e
  `configured` false.
- AC-005 [REQ-003]: Given una risposta in corso iniziata con il reranker acceso, when l'admin lo spegne, then quella
  risposta usa il reranker e la domanda successiva no.
- AC-006 [REQ-004]: Given il reranker non configurato, when l'admin invia `{"rerank_enabled": true}`, then risponde 409
  `service_not_configured` e `rerank.enabled` resta false; when invia `{}` o `{"rewrite_enabled": "yes"}`, then risponde
  422 `validation_error`.
- AC-007 [REQ-004]: Given un `user` e una richiesta senza sessione, when chiamano `GET` e `PUT /api/admin/settings`, then
  ricevono 403 `forbidden` e 401, e nessuna impostazione cambia.
- AC-008 [REQ-005]: Given la riscrittura attiva, una conversazione con uno scambio completato su «Quanto costa il
  prodotto X?» e un servizio finto che risponde «Quanto costa la spedizione del prodotto X?», when l'utente chiede «e la
  spedizione?», then Ollama riceve il testo riscritto, la BM25 cerca il testo riscritto, e il provider di generazione
  riceve «e la spedizione?» come ultima domanda.
- AC-009 [REQ-005]: Given la richiesta ricevuta dal servizio finto di riscrittura, when la si legge, then contiene la
  cronologia secondo REQ-006 della fase 3 e la domanda, e le istruzioni chiedono la sola domanda autonoma nella lingua
  della domanda, senza risposta, senza informazioni nuove e senza cambiarne il senso.
- AC-010 [REQ-005]: Given la riscrittura attiva, when si fa la prima domanda di una conversazione, e poi una domanda di
  seguito su una KB senza documenti READY, then il servizio di riscrittura non riceve nessuna richiesta.
- AC-011 [REQ-006]: Given la riscrittura attiva e un servizio finto irraggiungibile, poi con risposta 500, poi più lento
  di `REWRITE_TIMEOUT_SECONDS` uguale a 1, poi con testo vuoto, poi con 4001 caratteri, when si fa una domanda di
  seguito, then la risposta finisce completata, Ollama riceve la domanda originale e `search_query` è vuota.
- AC-012 [REQ-007]: Given un servizio finto che riscrive «e la spedizione?» come «Quanto costa la spedizione del prodotto
  X?», when la risposta finisce, then l'evento `done` e `GET /api/conversations/{id}` danno quel testo in
  `search_query`; given un servizio che restituisce «  E LA SPEDIZIONE?  », then `search_query` è vuota.
- AC-013 [REQ-007]: Given una risposta con `search_query`, when l'autore apre la conversazione nella chat e l'admin
  nella pagina «Conversazioni», then entrambi vedono sotto la risposta «Ricerca: <testo>» in italiano e «Search: <testo>»
  in inglese; given una risposta con `search_query` vuota, then la riga non compare.
- AC-014 [REQ-008]: Given il reranker attivo con formato `tei`, `RERANK_CANDIDATES` 3, `CHAT_CONTEXT_CHUNKS` 4, una
  fusione RRF [a, b, c, d, e] e un reranker finto che risponde con i punteggi c=0.9, a=0.5, b=0.1, when si fa una
  domanda, then il reranker riceve `{query, texts, truncate: true}` con i testi di a, b, c, il contesto numerato è [c, a, b, d] e le
  fonti salvate seguono quell'ordine.
- AC-015 [REQ-008]: Given il formato `cohere` con `RERANK_MODEL` impostato e `RERANK_API_KEY` impostata, when si fa una
  domanda, then il reranker finto riceve `POST /rerank` con `model`, `query`, `documents`, `top_n` uguale al numero dei
  candidati e l'header `Authorization: Bearer`, e la risposta `{results}` riordina il contesto come in AC-014.
- AC-016 [REQ-008]: Given la riscrittura e il reranker attivi, when si fa una domanda di seguito riscritta, then il
  reranker riceve come `query` la domanda riscritta; given una fusione con un solo chunk, then il reranker non riceve
  richieste.
- AC-017 [REQ-009]: Given il reranker attivo e un reranker finto irraggiungibile, poi con risposta 500, poi più lento di
  `RERANK_TIMEOUT_SECONDS` uguale a 1, poi con un indice fuori dai candidati, un indice ripetuto, un candidato mancante
  e un punteggio non numerico, when si fa una domanda, then la risposta finisce completata con il contesto nell'ordine
  RRF.
- AC-018 [REQ-010]: Given un'ultima risposta fallita con la riscrittura spenta, when l'admin accende la riscrittura e
  l'autore preme «Riprova», then il servizio di riscrittura riceve la richiesta e la risposta salvata ha la nuova
  `search_query`.
- AC-019 [REQ-011]: Given i due servizi non configurati, when si chiama `GET /api/health`, then `rewrite` e `rerank`
  sono `not_configured` e lo stato è `degraded` con codice 200; given due servizi finti che rispondono 2xx (`/models`
  per la riscrittura, `/health` per `tei`), then sono `ok`; given servizi irraggiungibili, then sono `unreachable` e il
  codice resta 200.
- AC-020 [REQ-011]: Given il formato `cohere`, when si chiama `GET /api/health`, then il reranker finto riceve
  `GET /models`; given gli interruttori spenti e i servizi configurati, then i controlli vengono eseguiti comunque.
- AC-021 [REQ-012]: Given l'admin, when apre «Impostazioni» con la riscrittura configurata e il reranker no, then vede
  l'interruttore della riscrittura attivo e quello del reranker disattivato con «Non configurato»; when accende la
  riscrittura, then il valore è salvato e resta acceso dopo un nuovo caricamento della pagina.
- AC-022 [REQ-012]: Given un salvataggio che il backend rifiuta con 409, when l'admin cambia l'interruttore, then torna
  allo stato precedente e la pagina mostra il motivo tradotto; given la UI in inglese, then i testi della pagina sono
  in inglese.
- AC-023 [REQ-012]: Given un `user`, when apre l'interfaccia e poi la rotta `/admin/settings`, then non vede la voce
  «Impostazioni» e viene portato a `/`; given nessuna sessione, then la rotta porta a `/login`.
- AC-024 [REQ-013]: Given il repository, when si legge la configurazione di `docker compose config` senza profili, then
  non contiene il servizio `reranker`; given `--profile rerank`, then lo contiene con l'immagine CPU, il modello di
  `RERANK_COMPOSE_MODEL` (default `BAAI/bge-reranker-v2-m3`), un volume per i modelli e nessuna porta pubblicata; given
  anche `-f compose.rerank-gpu.yaml`, then l'immagine è quella GPU con una riserva di GPU NVIDIA e il servizio è uno
  solo.
- AC-025 [REQ-101]: Given una domanda di seguito, una cronologia, una domanda riscritta e dei chunk con testi
  riconoscibili, chiavi `REWRITE_API_KEY` e `RERANK_API_KEY`, una risposta con i due passi usati e una con i due
  ripieghi, when si raccolgono i log di backend e worker, then nessuna riga contiene quei testi, punteggi del reranker o
  chiavi, e le righe contengono l'id della conversazione, l'uso o il ripiego dei passi e il motivo.
- AC-026 [REQ-102]: Given lo stack di test con i due passi attivi e tutti i servizi finti, when si fa una domanda di
  seguito, then le uniche richieste in uscita con contenuti della chat vanno a `REWRITE_BASE_URL` (`/chat/completions`),
  a `OLLAMA_BASE_URL` (`/api/embed`), a `RERANK_BASE_URL` (`/rerank`) e a `GENERATION_BASE_URL` (`/chat/completions`);
  given i due passi spenti, then nessuna richiesta va a `REWRITE_BASE_URL` o `RERANK_BASE_URL`.
- AC-027 [REQ-103]: Given i due servizi configurati e gli interruttori spenti, when si fa una domanda di seguito, then le
  richieste a Ollama e al provider, gli eventi SSE e il messaggio salvato sono uguali a quelli di una domanda con i
  servizi non configurati, e `search_query` è vuota.
- AC-028 [REQ-012]: Given lo stack completo attraverso Nginx con i servizi finti, when l'admin accende la riscrittura e
  il reranker dalla pagina «Impostazioni» e poi fa una domanda di seguito nella chat, then la risposta mostra
  «Ricerca: …» con il testo riscritto e le fonti nell'ordine del reranker finto.

## Constraints and assumptions
- Dalla constitution: reranker e riscrittura stanno dietro interfacce sostituibili per configurazione; il reranker usa
  un'immagine CPU o GPU scelta da configurazione, mai entrambe; un nuovo componente di infrastruttura richiede un ADR
  (ADR-004, con la PR di design); una nuova dipendenza va chiesta prima. I client usano `httpx` come quello della
  generazione (ADR-001).
- Dalla fase 3 (`docs/system/chat.md`): il retrieval filtra su KB, documenti READY e profilo attivo nella stessa
  istruzione SQL; la cronologia segue REQ-006 della fase 3; i log non contengono testi; un solo processo del backend.
- I test automatici usano un servizio di riscrittura finto e un reranker finto; il servizio `reranker` del Compose non
  viene avviato dai test (il modello reale si scarica da Hugging Face) ed è verificato solo con `docker compose config`;
  la prova con servizi reali è manuale o un test `live` facoltativo.
- [ASSUMPTION] Il testo di un candidato inviato al reranker è il contenuto del chunk, senza titolo del documento.
- [ASSUMPTION] La riscrittura e il reranking avvengono dopo l'evento `conversation` dello streaming e prima del primo
  `delta`, così i keepalive coprono l'attesa (fino a 30 + 15 secondi in più).
- [ASSUMPTION] Il punteggio salvato con una fonte resta il punteggio RRF; il punteggio del reranker non viene salvato.
- [ASSUMPTION] L'immagine CPU e l'immagine GPU di text-embeddings-inference sono fissate con un tag preciso nell'ADR-004;
  la GPU richiede NVIDIA Container Toolkit sull'host.
- [ASSUMPTION] Il codice d'errore per accendere un passo non configurato è `service_not_configured`.
- Confermato dall'owner (2026-11-20): la valutazione esce dalla fase 4 per ridurne l'ampiezza (rimandata, senza spec
  dedicata per ora).
- Confermato dall'owner (2026-11-20): riscrittura con provider separato, domanda autonoma dalla cronologia, ripiego sulla
  domanda originale, riga «Ricerca: …» quando diversa, prima domanda non riscritta, 30 secondi.
- Confermato dall'owner (2026-11-20): reranker con client generico per i formati `tei` e `cohere` e servizio nel
  Compose opzionale, 20 candidati, 15 secondi, ripiego sull'ordine RRF; modello scaricato al primo avvio.
- Confermato dall'owner (2026-11-20): pagina «Impostazioni» con due interruttori globali, solo admin, attivi dalla
  domanda successiva, spenti all'inizio, disattivati con «Non configurato»; controlli `rewrite` e `rerank` non
  bloccanti.

## Open questions
- Hardware di destinazione e tempi di risposta attesi su CPU con reranker e riscrittura accesi — blocking: no
- Valutazione del retrieval e delle risposte (dataset, metriche, confronto con i nuovi passi), rimandata — blocking: no

## Revisions
- 2026-11-20 (review di T-01): REQ-001, REQ-002 e AC-002. `RERANK_API`, `RERANK_MODEL` e `RERANK_CANDIDATES` si
  controllano solo con `RERANK_BASE_URL` impostata, perché con la lettura precedente un `.env` della fase 3 con
  `RETRIEVAL_CANDIDATES` minore di 20 non partiva più, contro REQ-103; i due timeout devono essere numeri finiti
  (`inf` e `nan` rifiutati). Decisione dell'owner alla review di T-01; il piano non cambia (T-01 copre ancora AC-002).
- 2026-11-20 (prima del merge della PR di design): REQ-008 e AC-014, il formato `tei` invia anche `truncate: true`,
  perché text-embeddings-inference rifiuta i testi oltre il limite del modello (decisione dell'owner al piano).
