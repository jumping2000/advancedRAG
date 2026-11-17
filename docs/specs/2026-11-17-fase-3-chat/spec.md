# SPEC-2026-11-17-fase-3-chat: Fase 3 – Retrieval ibrido e chat con citazioni

- Status: approved
- Owner: jumping2000

## Problem and goal
Dopo la fase 2 le KB contengono chunk con vettori e indice BM25, ma nessuno può interrogarli. La fase 3 aggiunge la chat
su una KB: l'utente fa una domanda, il backend recupera i chunk più pertinenti con un retrieval ibrido (vettoriale e
BM25, fusi con Reciprocal Rank Fusion), li passa al modello di generazione configurato dall'admin e mostra la risposta
mentre viene generata, con citazioni numerate che rimandano ai chunk usati. Le conversazioni sono salvate.

Criterio di successo: su uno stack con Ollama e un provider di generazione raggiungibili e una KB con almeno un documento
pronto, un utente assegnato apre la KB, fa una domanda in italiano o in inglese, vede il testo della risposta comparire
prima della fine della generazione, nella lingua della domanda, con citazioni [n] che aprono l'estratto del chunk
citato; ritrova la conversazione dopo un nuovo login; nessuna risposta, citazione o conversazione espone dati di una KB
non assegnata; `python3 scripts/verify.py` e la CI sono verdi.

## Users / actors
- Utente (ruolo `user`): chatta sulle KB a lui assegnate; vede, rinomina e cancella solo le proprie conversazioni.
- Admin: chatta su qualsiasi KB; legge in sola lettura le conversazioni di tutti e le cancella; configura il provider di
  generazione nel `.env`.
- Provider di generazione: servizio esterno con API OpenAI-compatible (OpenAI, Ollama con `/v1`, altri compatibili).

## Scope
- Included: retrieval ibrido (vettoriale sul profilo attivo, BM25, RRF) limitato alla KB, ai documenti pronti e al
  profilo attivo; costruzione del contesto con i chunk e gli ultimi messaggi; generazione in streaming con un client
  OpenAI-compatible; citazioni numerate con elenco delle fonti ed estratto; conversazioni salvate, rinominabili e
  cancellabili; pagina della KB con la chat e l'elenco delle proprie conversazioni; pagina «Conversazioni» per l'admin in
  sola lettura; errori della generazione con «Riprova»; controllo `generation` in `GET /api/health`; streaming attraverso
  Nginx; testi della UI in italiano e inglese; nuove variabili d'ambiente.
- **Non-scope (explicit):** restano fuori dalla fase 3 i punti seguenti.
  - query rewriting, reranker, valutazione del retrieval e della generazione, pagina impostazioni (fase 4);
  - re-indicizzazione: i documenti pronti con un profilo diverso da quello attivo non sono ricercabili (limite noto);
  - domande su più KB insieme o su un sottoinsieme di documenti scelto dall'utente;
  - rilevamento della lingua con una libreria e analyzer BM25 diversi per lingua;
  - esportazione, condivisione o ricerca nel testo delle conversazioni; paginazione degli elenchi di conversazioni;
  - scadenza automatica delle conversazioni;
  - modifica o cancellazione di un singolo messaggio; feedback sulle risposte (pollice su/giù);
  - provider con API diverse da OpenAI-compatible (Ollama `/api/chat` nativo, Anthropic, altri) e più provider attivi
    insieme;
  - download del documento originale dalla citazione (gli originali non esistono più);
  - modifiche alla pagina Documenti e alle API della fase 2.
- **Anti-goals:** la fase 3 fallisce, anche se funziona, se:
  - una risposta, una citazione, un estratto o una conversazione contiene chunk, nomi o metadati di una KB non assegnata
    al chiamante, o un `user` vede la conversazione di un altro utente;
  - un numero [n] della risposta salvata non corrisponde a un chunk davvero passato al modello, o una fonte appartiene a
    un documento non pronto o a un altro profilo;
  - domande, chunk o risposte arrivano a un servizio che l'admin non ha configurato esplicitamente, o il testo di
    domande, risposte e chunk compare nei log;
  - una generazione lenta o fallita blocca la pagina o rallenta oltre i limiti le altre API del backend.

## Current behaviour and impact
- Today (`docs/system/fondazioni.md`, `docs/system/ingestion.md`): un utente apre una KB assegnata e vede solo nome e
  descrizione (`KnowledgeBasePage`); i chunk dei documenti pronti hanno vettori nel profilo attivo (indice HNSW parziale
  per profilo con l'espressione `embedding::vector(N)`) e l'indice BM25 `chunks_bm25` con `kb_id` come campo filtro;
  `GET /api/health` controlla database, redis, ollama, docling e worker con stati `ok` e `unreachable`; non esiste
  nessuna impostazione per un modello di generazione; Nginx bufferizza le risposte del backend; cancellare una KB
  cancella documenti, chunk e originali.
- Changes: tabelle nuove per conversazioni, messaggi e fonti dei messaggi; API delle conversazioni e della chat in
  streaming; la pagina della KB diventa la pagina della chat; la pagina «Conversazioni» e la sua voce di menu per
  l'admin; il controllo `generation` in `GET /api/health` con il nuovo stato `not_configured`; Nginx non bufferizza la
  rotta di streaming e la attende per tutta la durata della generazione; cancellare una KB cancella anche le sue
  conversazioni; nuove variabili d'ambiente. Questa spec **rivede la fase 1**: REQ-003/AC-005 (elenco e valori dei
  controlli di salute) e UC-005 (aprire una KB mostra la chat); i test `test_health.py` e le due spec (voce in
  `## Revisions`) si aggiornano con la PR di design e con i task.
- Must not change: isolamento per KB, formato degli errori, login e sessioni, HTTPS opzionale, pipeline di ingestion,
  pagina e API dei documenti; protetti da `backend/tests/test_isolation.py`, `test_knowledge_bases.py`, `test_auth.py`,
  `test_users.py`, `test_documents_upload.py`, `test_chunk_storage.py`, `test_document_delete.py` e dai test end-to-end
  `test_stack.py`, `test_tls.py`, `test_admin_flow.py`, `test_ingestion_e2e.py`, `test_documents_ui.py` (tranne la
  revisione di `test_health.py` sopra).

## Use cases
- UC-001 Fare una domanda: l'attore apre una KB, sceglie una conversazione o ne inizia una nuova, scrive la domanda e la
  invia; il testo della risposta compare mentre viene generato, poi le fonti citate. Errori: domanda vuota o troppo
  lunga; KB senza documenti pronti (messaggio fisso); provider non configurato; Ollama o provider in errore, in timeout
  o interrotto (risposta fallita con «Riprova»); una risposta già in corso nella stessa conversazione.
- UC-002 Leggere una citazione: l'attore apre una fonte della risposta e legge documento, sezione, pagina ed estratto;
  se il documento è stato cancellato vede «Fonte non più disponibile».
- UC-003 Gestire le proprie conversazioni: l'attore elenca le sue conversazioni della KB, ne riapre una, la rinomina o
  la cancella. Errore: titolo vuoto o troppo lungo.
- UC-004 Riprovare una risposta: l'attore preme «Riprova» sull'ultima risposta fallita; il backend la rigenera.
- UC-005 Leggere le conversazioni (admin): l'admin apre «Conversazioni», filtra per KB e per utente, apre una
  conversazione in sola lettura e la può cancellare.

## Requirements
- REQ-001 (functional): Il backend legge la configurazione della generazione da `GENERATION_BASE_URL` (URL di base di
  un'API OpenAI-compatible, per esempio `https://api.openai.com/v1` o `http://ollama:11434/v1`), `GENERATION_MODEL`,
  `GENERATION_API_KEY` (facoltativa, inviata come `Authorization: Bearer`) e `GENERATION_TIMEOUT_SECONDS` (default 300,
  maggiore di 0); con `GENERATION_BASE_URL` vuota il backend parte, ogni domanda risponde 503 `generation_not_configured`
  e nessuna richiesta parte verso un provider; con `GENERATION_BASE_URL` impostata e `GENERATION_MODEL` vuota il
  backend non parte e il messaggio nomina la variabile.
- REQ-002 (functional): `GET /api/health` aggiunge il controllo `generation`: `not_configured` con `GENERATION_BASE_URL`
  vuota, `ok` se `GET {GENERATION_BASE_URL}/models` risponde 2xx entro `HEALTH_CHECK_TIMEOUT_SECONDS`, altrimenti
  `unreachable`; lo stato complessivo è `ok` solo se tutti i controlli sono `ok`, altrimenti `degraded`; il codice
  della risposta resta deciso solo da database e Redis.
- REQ-003 (functional): Il retrieval di una domanda calcola il vettore della domanda con Ollama e il modello del profilo
  attivo, poi esegue due ricerche, ciascuna con al massimo `RETRIEVAL_CANDIDATES` risultati (default 50): una
  vettoriale per distanza coseno con l'espressione del profilo attivo e una BM25 sul contenuto dei chunk; entrambe
  restituiscono solo chunk della KB della conversazione, di documenti in stato READY e del profilo attivo, con il
  controllo di accesso alla KB nella stessa istruzione SQL.
- REQ-004 (functional): Il retrieval fonde le due liste con Reciprocal Rank Fusion: punteggio(c) = Σ 1 / (`RRF_K` +
  rank(c)) sulle liste in cui c compare (rank da 1, `RRF_K` default 60); ogni chunk compare una sola volta; a parità di
  punteggio vince il rank vettoriale migliore, poi l'id del chunk; il risultato tiene, per ogni chunk, il rank in
  ciascuna lista (o nessuno) e il punteggio RRF.
- REQ-005 (functional): La ricerca BM25 tratta la domanda come testo: virgolette, due punti, parentesi, asterischi,
  operatori come `AND`, `OR`, `NOT` e altri caratteri speciali non causano errori e non cambiano il significato della
  ricerca; una domanda senza parole indicizzabili dà una lista BM25 vuota e la ricerca prosegue con la sola lista
  vettoriale.
- REQ-006 (functional): Il contesto passato al modello contiene, numerati da 1 nell'ordine RRF, i primi
  `CHAT_CONTEXT_CHUNKS` chunk (default 8), ciascuno con titolo del documento, sezione e pagine quando note, e gli ultimi
  `CHAT_HISTORY_MESSAGES` messaggi completati della conversazione (default 6, risposte fallite escluse) prima della
  domanda; le istruzioni chiedono di rispondere solo in base alle fonti, nella lingua della domanda, di citare le fonti
  con [n] e di dire esplicitamente quando le fonti non contengono la risposta.
- REQ-007 (functional): Se la KB non ha nessun chunk ricercabile (nessun documento READY nel profilo attivo), la domanda
  riceve una risposta fissa salvata nella conversazione con il codice `no_documents` e senza fonti, che la UI mostra
  tradotta («Nessun documento disponibile in questa knowledge base»); né Ollama né il provider di generazione vengono chiamati.
- REQ-008 (functional): `POST` della domanda in una conversazione risponde in streaming (Server-Sent Events): eventi con
  i frammenti di testo man mano che il provider li produce, poi un evento finale con il testo salvato, l'id del messaggio
  e le fonti, oppure un evento di errore con un codice; il provider è chiamato con `stream: true` e la generazione
  termina con errore `generation_timeout` dopo `GENERATION_TIMEOUT_SECONDS`.
- REQ-009 (functional): Le citazioni della risposta salvata sono solo i numeri [n] con n tra 1 e il numero di chunk del
  contesto; un [n] fuori da questo intervallo viene tolto dal testo salvato e dall'evento finale; le fonti mostrate sono
  i chunk del contesto citati almeno una volta, con il loro numero, il nome del documento, la sezione e le pagine quando
  note; per ogni chunk del contesto il messaggio salva numero, rank vettoriale, rank BM25 e punteggio RRF.
- REQ-010 (functional): L'estratto di una fonte è il contenuto del chunk, letto al momento della richiesta e solo da chi
  può leggere la conversazione; se il chunk non esiste più (documento o KB cancellati), la fonte mostra il nome del
  documento salvato con la risposta e «Fonte non più disponibile»; il contenuto dei chunk non viene copiato nelle
  conversazioni.
- REQ-011 (functional): Una conversazione appartiene a un solo utente e a una sola KB; si crea con la prima domanda e il
  suo titolo sono i primi 80 caratteri della domanda (spazi iniziali e finali tolti); l'autore la rinomina con un titolo
  di 1–200 caratteri dopo aver tolto gli spazi iniziali e finali (altrimenti 422 `validation_error`); una domanda vuota
  dopo aver tolto gli spazi o più lunga di `CHAT_MAX_QUESTION_CHARS` (default 4000) riceve 422 `validation_error` e non
  viene salvata.
- REQ-012 (functional): Un `user` elenca, apre, rinomina, cancella e usa solo le proprie conversazioni delle KB a lui
  assegnate, ordinate dall'ultima attività; una conversazione di un altro utente, di una KB non assegnata o con un id
  che non è un UUID riceve 404 `conversation_not_found`; se l'utente viene tolto dalla KB le sue conversazioni restano ma
  non sono raggiungibili finché non viene riassegnato.
- REQ-013 (functional): L'admin chatta su qualsiasi KB con le proprie conversazioni; elenca le conversazioni di tutti
  gli utenti con filtro per KB e per utente, le apre in sola lettura (nessuna domanda, «Riprova» o rinomina sulle
  conversazioni degli altri) e le cancella; cancellare una KB cancella le sue conversazioni con messaggi e fonti; un
  utente disattivato conserva le sue conversazioni.
- REQ-014 (functional): In una conversazione è in corso al massimo una risposta: una domanda o un «Riprova» inviati
  mentre una risposta è in corso ricevono 409 `answer_in_progress`.
- REQ-015 (functional): Se il calcolo del vettore della domanda fallisce (`embedding_error`), se il provider è
  irraggiungibile, risponde con un errore o interrompe lo streaming (`generation_error`), se scade il tempo
  (`generation_timeout`), o se la connessione del client si chiude prima della fine o il backend riparte durante la
  generazione (`interrupted`), la domanda resta salvata, la risposta è salvata come fallita con quel codice e senza testo
  (il testo parziale viene scartato) e la generazione si ferma.
- REQ-016 (functional): «Riprova» è disponibile solo sull'ultima risposta della conversazione quando è fallita; ripete
  retrieval e generazione per la stessa domanda e sostituisce la risposta fallita; una risposta fallita seguita da una
  nuova domanda resta fallita senza «Riprova».
- REQ-017 (functional): All'avvio il backend segna come fallite `interrupted` le risposte rimaste in corso.
- REQ-018 (functional): Cancellare una conversazione, anche con una risposta in corso, ferma la generazione e cancella
  conversazione, messaggi e fonti; una richiesta successiva sulla conversazione riceve 404 `conversation_not_found`.
- REQ-019 (functional): La pagina di una KB mostra a sinistra l'elenco delle proprie conversazioni con «Nuova
  conversazione», «Rinomina» ed «Elimina» (con conferma), e a destra i messaggi con il campo della domanda; il testo
  della risposta compare man mano; le citazioni [n] sono collegamenti alle fonti elencate sotto la risposta, ognuna
  apribile per leggere l'estratto; una risposta fallita mostra il motivo tradotto e, se è l'ultima, «Riprova»; il campo
  e il pulsante di invio sono disabilitati mentre una risposta è in corso; tutti i testi nuovi esistono in italiano e in
  inglese.
- REQ-020 (functional): L'admin ha la voce di menu «Conversazioni» e la pagina `/admin/conversations` con filtri per KB
  e per utente, l'elenco ordinato dall'ultima attività (titolo, utente, KB, data, numero di messaggi), l'apertura in
  sola lettura con le stesse citazioni ed estratti della chat ed «Elimina» con conferma; un `user` non vede la voce e la
  rotta lo porta a `/`.
- REQ-021 (functional): Nginx inoltra la rotta di streaming senza bufferizzare e senza chiuderla prima di
  `GENERATION_TIMEOUT_SECONDS` più 30 secondi.
- REQ-101 (non-functional): Mentre una risposta viene generata, `GET /api/health`, `GET /api/kbs` e le API delle
  conversazioni rispondono entro `HEALTH_CHECK_TIMEOUT_SECONDS` più 1 secondo.
- REQ-102 (non-functional): Né il backend né il worker scrivono nei log il testo di domande, risposte, titoli o chunk,
  né vettori o la chiave del provider; le righe della chat contengono l'id della conversazione e del messaggio, la KB, i
  conteggi, le durate e i codici di errore.
- REQ-103 (non-functional): Domande, chunk e storico partono solo verso `GENERATION_BASE_URL` e il vettore della domanda
  solo verso `OLLAMA_BASE_URL`; nessun'altra destinazione riceve contenuti della chat.

## Acceptance criteria
- AC-001 [REQ-001]: Given `GENERATION_BASE_URL` vuota, when un utente invia una domanda, then la risposta è 503
  `generation_not_configured`, nessuna domanda viene salvata e il provider finto non riceve richieste.
- AC-002 [REQ-001]: Given `GENERATION_BASE_URL` impostata e `GENERATION_MODEL` vuota, when il backend parte, then termina
  con un messaggio che nomina `GENERATION_MODEL`.
- AC-003 [REQ-001]: Given `GENERATION_API_KEY` impostata, when parte una generazione, then il provider finto riceve
  `Authorization: Bearer <chiave>` e il modello `GENERATION_MODEL`; con la chiave vuota non riceve l'intestazione.
- AC-004 [REQ-002]: Given `GENERATION_BASE_URL` vuota, when si chiama `GET /api/health`, then `generation` è
  `not_configured`, lo stato è `degraded` e il codice è 200 con database e Redis attivi.
- AC-005 [REQ-002]: Given un provider finto che risponde 200 a `/models`, e poi un provider irraggiungibile, when si
  chiama `GET /api/health`, then `generation` è prima `ok` e poi `unreachable`, entro `HEALTH_CHECK_TIMEOUT_SECONDS` più
  1 secondo.
- AC-006 [REQ-003]: Given due KB con un chunk dallo stesso testo ciascuna e un utente assegnato solo alla prima, when la
  ricerca vettoriale e quella BM25 vengono chiamate direttamente per l'utente sulla seconda KB e sulla prima, then sulla
  seconda non restituiscono nulla e sulla prima solo il chunk della prima.
- AC-007 [REQ-003]: Given una KB con un documento READY, uno PROCESSING e uno FAILED con chunk inseriti dal test e uno
  READY di un altro profilo, when si fa una domanda con le parole dei tre, then il contesto contiene solo
  chunk del documento READY del profilo attivo.
- AC-008 [REQ-003]: Given una KB con 60 chunk del profilo attivo e 60 di un altro profilo con vettori vicini alla
  domanda, when la ricerca vettoriale chiede 50 risultati, then restituisce 50 chunk, tutti del profilo attivo.
- AC-009 [REQ-004]: Given una lista vettoriale [a, b, c] e una BM25 [c, d, a] con `RRF_K` 60, when si fondono, then
  l'ordine è a, c, b, d con punteggi 1/61+1/63, 1/63+1/61, 1/62, 1/62 e i pari merito risolti dal rank vettoriale (a
  prima di c, b prima di d), ciascun chunk una sola volta con i propri rank.
- AC-010 [REQ-005]: Given le domande `"prezzo": (IVA) AND *`, `NOT`, `C++ / C#?` e `:::`, when si fanno, then nessuna
  riceve un errore e la ricerca BM25 della prima trova un chunk che contiene «prezzo» e «IVA».
- AC-011 [REQ-005]: Given una domanda fatta solo di segni di punteggiatura, when si fa, then la risposta arriva con il
  contesto della sola ricerca vettoriale.
- AC-012 [REQ-006]: Given una conversazione con 4 scambi completati e uno fallito e una KB con 12 chunk pertinenti,
  when si fa una nuova domanda, then la richiesta al provider finto contiene 8 chunk numerati da 1 nell'ordine RRF con
  documento, sezione e pagine, gli ultimi 6 messaggi completati senza la risposta fallita, e la domanda.
- AC-013 [REQ-006]: Given la richiesta inviata al provider finto, when la si legge, then le istruzioni chiedono di
  rispondere solo dalle fonti, nella lingua della domanda, di citare con [n] e di dire quando le fonti non bastano.
- AC-014 [REQ-007]: Given una KB senza documenti READY, when si fa una domanda, then la risposta è salvata con il codice
  `no_documents` e senza fonti, la UI la mostra come «Nessun documento disponibile in questa knowledge base», e né Ollama finto né il provider
  finto ricevono richieste.
- AC-015 [REQ-008]: Given un provider finto che invia tre frammenti a un secondo di distanza, when si fa una domanda,
  then il client riceve il primo frammento prima che il provider invii il terzo, poi l'evento finale con il testo
  completo, l'id del messaggio e le fonti; il messaggio salvato ha lo stesso testo.
- AC-016 [REQ-008]: Given `GENERATION_TIMEOUT_SECONDS` uguale a 2 e un provider finto che non risponde, when si fa una
  domanda, then dopo circa 2 secondi arriva l'evento di errore `generation_timeout` e la risposta salvata è fallita con
  quel codice.
- AC-017 [REQ-009]: Given un provider finto che risponde «Vedi [2] e [9] e [2]» con 8 chunk nel contesto, when la
  risposta finisce, then il testo salvato e l'evento finale sono «Vedi [2] e  e [2]», le fonti sono solo la numero 2 con
  il suo documento, e il messaggio salva i rank e i punteggi degli 8 chunk del contesto.
- AC-018 [REQ-009]: Given una risposta senza nessun [n], when finisce, then non ha fonti mostrate e il messaggio salva
  comunque i chunk del contesto con rank e punteggi.
- AC-019 [REQ-010]: Given una risposta con una fonte, when l'autore apre la fonte, then riceve il contenuto attuale del
  chunk; when un altro `user` o un `user` tolto dalla KB chiede la stessa fonte, then riceve 404
  `conversation_not_found`.
- AC-020 [REQ-010]: Given una risposta con una fonte di un documento poi cancellato, when l'autore riapre la
  conversazione e la fonte, then la fonte mostra il nome del documento e «Fonte non più disponibile», e nel database non
  esiste nessuna copia del contenuto di quel chunk.
- AC-021 [REQ-011]: Given una prima domanda di 120 caratteri con spazi iniziali, when viene inviata, then nasce una
  conversazione con titolo uguale ai primi 80 caratteri della domanda senza gli spazi iniziali.
- AC-022 [REQ-011]: Given una conversazione, when l'autore la rinomina con «  Budget 2026  », con «   » e con 201
  caratteri, then il primo titolo diventa «Budget 2026», gli altri ricevono 422 `validation_error` e il titolo resta.
- AC-023 [REQ-011]: Given una domanda di soli spazi e una di 4001 caratteri, when vengono inviate, then entrambe
  ricevono 422 `validation_error` e nessun messaggio viene salvato.
- AC-024 [REQ-012]: Given due utenti assegnati alla stessa KB con una conversazione ciascuno, when ciascuno elenca le
  conversazioni della KB, then vede solo la propria; when chiede, rinomina, cancella o scrive nella conversazione
  dell'altro, o usa un id che non è un UUID, then riceve 404 `conversation_not_found` e nulla cambia.
- AC-025 [REQ-012]: Given la funzione che legge una conversazione per un utente, when il test la chiama direttamente con
  l'id di una conversazione di un'altra KB o di un altro utente, then non restituisce nulla.
- AC-026 [REQ-012]: Given un utente con una conversazione su una KB, when l'admin lo toglie dalla KB, then l'utente non
  la vede più e riceve 404 aprendola; when l'admin lo riassegna, then la ritrova con tutti i messaggi.
- AC-027 [REQ-013]: Given conversazioni di due utenti su due KB, when l'admin le elenca senza filtri, con il filtro per
  KB e con il filtro per utente, then vede rispettivamente tutte, solo quelle della KB e solo quelle dell'utente.
- AC-028 [REQ-013]: Given la conversazione di un utente, when l'admin la apre, then legge messaggi e fonti; when invia
  una domanda, un «Riprova» o una rinomina su quella conversazione, then riceve 403 `forbidden` e nulla cambia; when la
  cancella, then non esiste più.
- AC-029 [REQ-013]: Given una KB con conversazioni, messaggi e fonti, when l'admin cancella la KB, then non resta nessuna
  conversazione, messaggio o fonte di quella KB.
- AC-030 [REQ-013]: Given un utente con conversazioni, when l'admin lo disattiva e poi lo riattiva, then le conversazioni
  esistono ancora e l'utente le ritrova.
- AC-031 [REQ-014]: Given una risposta in corso in una conversazione, when arriva una seconda domanda o un «Riprova»
  nella stessa conversazione, then ricevono 409 `answer_in_progress`; una domanda in un'altra conversazione procede.
- AC-032 [REQ-015]: Given Ollama finto in errore, poi il provider finto irraggiungibile, poi uno che risponde 500, poi
  uno che chiude lo stream a metà, when si fa una domanda in ciascun caso, then la domanda è salvata, la risposta è
  fallita rispettivamente con `embedding_error`, `generation_error`, `generation_error`, `generation_error`, senza testo,
  e il client riceve l'evento di errore con quel codice.
- AC-033 [REQ-015]: Given un provider finto lento, when il client chiude la connessione dopo il primo frammento, then la
  risposta salvata è fallita `interrupted` senza testo e il provider finto vede chiudere la sua richiesta.
- AC-034 [REQ-016]: Given l'ultima risposta fallita, when l'autore preme «Riprova», then retrieval e generazione
  ripartono per la stessa domanda e la conversazione ha una sola risposta, completata, al posto di quella fallita.
- AC-035 [REQ-016]: Given una risposta fallita seguita da una nuova domanda, when si chiede «Riprova» sulla risposta
  fallita, then la risposta è 409 `not_retryable` e la UI non mostra il pulsante su quella risposta.
- AC-036 [REQ-017]: Given una risposta segnata in corso nel database, when il backend riparte, then la risposta è fallita
  `interrupted`.
- AC-037 [REQ-018]: Given una risposta in corso, when l'autore cancella la conversazione, then la generazione si ferma,
  conversazione, messaggi e fonti non esistono più e una richiesta successiva riceve 404 `conversation_not_found`.
- AC-038 [REQ-019]: Given un utente sulla pagina di una KB, when invia una domanda, then vede il testo comparire prima
  della fine, poi le fonti numerate; cliccando una citazione [n] legge l'estratto; il campo è disabilitato fino alla fine
  della risposta.
- AC-039 [REQ-019]: Given una risposta fallita come ultima risposta, when l'utente guarda la conversazione, then vede il
  motivo tradotto e «Riprova», che lo porta a una risposta completata.
- AC-040 [REQ-019]: Given l'elenco delle conversazioni, when l'utente sceglie «Nuova conversazione», «Rinomina» ed
  «Elimina» (con «Annulla» e poi «Conferma»), then la conversazione nuova compare dopo la prima domanda, il titolo
  cambia, «Annulla» la lascia e «Conferma» la rimuove.
- AC-041 [REQ-019]: Given la UI in inglese e poi in italiano, when si apre la chat con una risposta fallita e una con
  fonti, then ogni testo nuovo della pagina è tradotto e nessuna chiave di traduzione compare a schermo.
- AC-042 [REQ-020]: Given l'admin e conversazioni di due utenti, when apre «Conversazioni», filtra per KB e per utente e
  apre una conversazione, then vede le colonne titolo, utente, KB, data e numero di messaggi, la conversazione in sola
  lettura senza campo della domanda né «Riprova», e «Elimina» con conferma la rimuove.
- AC-043 [REQ-020]: Given un utente `user`, when apre l'interfaccia o la rotta `/admin/conversations`, then non vede la
  voce «Conversazioni» e viene portato a `/`.
- AC-044 [REQ-021]: Given lo stack Compose con un provider finto che invia un frammento ogni 20 secondi per 90 secondi
  in tutto, when un browser fa la domanda attraverso Nginx, then riceve ogni frammento entro 2 secondi da quando il
  provider lo invia e la risposta finisce completata.
- AC-045 [REQ-101]: Given una risposta in corso con un provider finto che risponde dopo 30 secondi, when si chiamano
  `GET /api/health`, `GET /api/kbs` e l'elenco delle conversazioni, then rispondono entro
  `HEALTH_CHECK_TIMEOUT_SECONDS` più 1 secondo.
- AC-046 [REQ-102]: Given una domanda, una risposta, un titolo e un chunk con testi riconoscibili, una risposta
  completata e una fallita, when si raccolgono tutti i log di backend e worker, then nessuna riga contiene quei testi,
  un vettore o `GENERATION_API_KEY`, e le righe della chat contengono l'id della conversazione e il codice d'errore.
- AC-047 [REQ-103]: Given lo stack di test con Ollama finto e provider finto, when si fa una domanda con risposta
  completata, then le uniche richieste in uscita del backend con contenuti della chat vanno all'URL di Ollama
  (`/api/embed`, solo la domanda) e a `GENERATION_BASE_URL` (`/chat/completions`).

## Constraints and assumptions
- Vincoli dal documento di architettura: retrieval ibrido vettoriale e BM25 con RRF, k=60 e 50 candidati per lista;
  stesso modello e profilo per la domanda e per i chunk; filtro obbligatorio sulla KB; nessuna traduzione della domanda;
  reranker e query rewriting fuori da questa fase.
- Vincoli dalla fase 2 (`docs/system/ingestion.md`, note per la fase 3): unire `documents.status = 'READY'`; filtrare
  ogni ricerca vettoriale su `chunks.profile_id` e ordinare per l'espressione del profilo `embedding::vector(N)` con
  `<=>`; eseguire la ricerca con `SET LOCAL hnsw.iterative_scan = relaxed_order` e un `hnsw.max_scan_tuples` adeguato;
  BM25 con l'operatore `@@@` e `kb_id` come campo filtro; un solo worker.
- Il client di generazione usa `POST {GENERATION_BASE_URL}/chat/completions` con `stream: true`; i test automatici usano
  un provider finto e un Ollama finto; la prova con servizi reali è un test `live` facoltativo.
- Confermato dall'owner (2026-11-17): un solo client OpenAI-compatible copre OpenAI, Ollama (`/v1`) e altri endpoint.
- Confermato dall'owner (2026-11-17): conversazioni salvate, private dell'autore, leggibili e cancellabili dall'admin da
  una pagina in sola lettura; nessuna scadenza; titolo dall'inizio della prima domanda, rinominabile.
- Confermato dall'owner (2026-11-17): streaming token per token; citazioni [n] nel testo con elenco delle fonti ed
  estratto; «Fonte non più disponibile» per un documento cancellato, senza copia del contenuto.
- Confermato dall'owner (2026-11-17): 8 chunk nel contesto e ultimi 6 messaggi; lingua della risposta chiesta con le
  istruzioni, senza libreria di rilevamento.
- Confermato dall'owner (2026-11-17): KB senza documenti pronti → messaggio fisso senza chiamate; errori della
  generazione → domanda salvata, risposta fallita, testo parziale scartato, «Riprova»; 4000 caratteri, 300 secondi, una
  risposta in corso per conversazione.
- Confermato dall'owner (2026-11-17): togliere un utente da una KB nasconde le sue conversazioni senza cancellarle;
  documenti di un profilo diverso dal profilo attivo non ricercabili (limite noto).
- Confermato dall'owner (2026-11-17): provider non configurato → 503 `generation_not_configured` e health
  `not_configured`; titolo di 1–200 caratteri; le conversazioni di un utente disattivato restano.
- Confermato dall'owner (2026-11-17): le fonti mostrate sono solo i chunk citati (REQ-009); la chiusura della connessione del client ferma la
  generazione (REQ-015); «Riprova» solo sull'ultima risposta (REQ-016); nessuna paginazione dell'elenco admin (REQ-020);
  Nginx attende `GENERATION_TIMEOUT_SECONDS` più 30 secondi (REQ-021).

## Open questions
- Hardware di destinazione, volumi di conversazioni e utenti, tempi di risposta attesi su CPU (fase 4) — blocking: no
- Dataset e metriche per valutare retrieval, fusione RRF e qualità delle risposte in italiano e inglese (fase 4) — blocking: no

## Revisions
- none
