# SPEC-2026-11-07-fase-2-ingestion: Fase 2 – Ingestion dei documenti

- Status: implemented
- Owner: jumping2000

## Problem and goal
Dopo la fase 1 (stack, accesso, utenti, KB) la piattaforma non contiene ancora nessun contenuto. La fase 2 porta i
documenti dentro le KB: l'admin carica file PDF, DOCX, PPTX, XLSX, TXT e MD; un worker separato li trasforma in chunk con vettori e
indice lessicale (BM25), pronti per il retrieval ibrido della fase 3. Il file originale non viene conservato oltre il
necessario: restano chunk, vettori e metadati.

Criterio di successo: su uno stack avviato con Docling e Ollama raggiungibili, l'admin carica un documento valido dalla
pagina Documenti e lo vede passare da «In coda» a «Pronto» senza ricaricare la pagina; un documento che non può essere
elaborato mostra lo stato e il motivo; dopo l'esito, o dopo la cancellazione, non resta nessun file originale oltre le
regole di conservazione e nessun chunk fuori dai documenti pronti; `python3 scripts/verify.py` e la CI sono verdi.

## Users / actors
- Admin: carica i documenti in una KB, ne segue lo stato, riprova quelli falliti e li cancella.
- Utente (ruolo `user`): in questa fase non vede e non usa i documenti; le loro citazioni arrivano con la chat (fase 3).
- Worker: processo separato che elabora un documento alla volta (Docling, chunking, embedding, indici).

## Scope
- Included: upload di un file per richiesta in una KB; validazione di dimensione, tipo e formato; coda RQ e servizio
  `worker` nel Compose; parsing (Docling per PDF, DOCX, PPTX e XLSX); normalizzazione; chunking strutturale; embedding con Ollama;
  profilo di indicizzazione salvato su ogni chunk; indici vettoriale e BM25; verifica degli indici; stati, motivi,
  tentativi automatici e «Riprova»; conservazione e pulizia degli originali; cancellazione di documenti e KB; pagina
  Documenti per l'admin in italiano e inglese; controllo `worker` in `GET /api/health`.
- **Non-scope (explicit):** restano fuori dalla fase 2 i punti seguenti.
  - retrieval, ricerca, chat, conversazioni e citazioni (fase 3); query rewriting, reranker e valutazione (fase 4);
  - re-indicizzazione: costruire un profilo nuovo e ricalcolare i vettori dei chunk esistenti (il modello dati lo
    prepara, il job no);
  - regole di chunking dedicate a PPTX e XLSX (una diapositiva o un gruppo di righe di un foglio per chunk): questi
    formati seguono le stesse regole degli altri;
  - OCR: i PDF senza testo estraibile diventano UNSUPPORTED;
  - analyzer BM25 diversi per lingua e rilevamento della lingua dei chunk;
  - anteprima, download o sostituzione di un documento già caricato, e qualsiasi aggiornamento «in place»;
  - elenco dei documenti per gli utenti (`user`);
  - upload di più file in una sola richiesta, cartelle o archivi ZIP;
  - paginazione dell'elenco dei documenti, ricerca o filtri;
  - Ollama e Docling dentro il Compose: restano servizi esterni.
- **Anti-goals:** la fase 2 fallisce, anche se funziona, se:
  - un documento resta in coda, in elaborazione o fallito senza uno stato e un motivo visibili all'admin;
  - esistono chunk parziali di un documento non pronto, o chunk e vettori orfani dopo la cancellazione di un documento
    o di una KB;
  - nello stesso spazio di ricerca finiscono vettori di modelli o dimensioni diverse;
  - un utente non admin ottiene documenti, chunk o metadati di una KB;
  - contenuto dei documenti, nomi dei file o vettori compaiono nei log.

## Current behaviour and impact
- Today (`docs/system/fondazioni.md`, SPEC-2026-11-06-fase-1-fondazioni): quattro servizi Compose (nginx, backend,
  redis, db), KB gestite solo dall'admin con isolamento per utente in `KnowledgeBaseRepository`, `GET /api/health` con
  quattro controlli (database, redis, ollama, docling), guardia globale `require_json` (422 per ogni richiesta non JSON),
  limite di corpo Nginx di 1 MB, nessuna tabella di documenti, nessun worker, nessun volume per i file.
- Changes: tre tabelle nuove (documenti, chunk, profili di indicizzazione) con indici vettoriale e BM25; il servizio
  `worker` e il volume `uploads` nel Compose; le API dei documenti sotto `/api/kbs/{kb_id}/documents`; la rotta di upload
  è l'unica eccezione a `require_json` (multipart); Nginx alza il limite di corpo solo per quella rotta; la cancellazione
  di una KB rimuove anche documenti, chunk e originali; la pagina Documenti per l'admin; nuove variabili d'ambiente.
  Questa spec **rivede la fase 1**: REQ-001/AC-001 (quattro servizi) e REQ-003/AC-005 (elenco dei controlli di salute)
  cambiano per il quinto servizio e il controllo `worker`; i test `test_stack.py` e `test_health.py` e le due spec
  (voce in `## Revisions`) si aggiornano con la PR di design e con i task.
- Must not change: isolamento per KB, formato degli errori `{"error": {"code", "message"}}`, login e sessioni, HTTPS
  opzionale; protetti da `backend/tests/test_isolation.py`, `test_knowledge_bases.py`, `test_auth.py`, `test_users.py` e
  dai test end-to-end `test_stack.py`, `test_tls.py`, `test_admin_flow.py` (tranne le due revisioni sopra).

## Use cases
- UC-001 Caricare documenti: l'admin apre la pagina Documenti, sceglie una KB e uno o più file; ogni file è inviato in
  una richiesta propria e compare nell'elenco «In coda». Errori per file: tipo non ammesso, file vuoto, file troppo
  grande, duplicato nella stessa KB.
- UC-002 Elaborazione: il worker prende il job, interpreta il file, normalizza, divide in chunk, calcola i vettori,
  salva tutto, verifica gli indici e segna il documento «Pronto». Errori: servizio esterno irraggiungibile o in errore
  temporaneo (tentativi automatici), file non interpretabile, nessun testo estraibile, dimensione dei vettori diversa dal
  profilo, verifica degli indici non superata.
- UC-003 Riprovare: l'admin preme «Riprova» su un documento fallito il cui originale esiste ancora.
- UC-004 Cancellare: l'admin cancella un documento (anche in elaborazione) o una KB; non resta nulla del contenuto.
- UC-005 Pulizia: il worker rimuove gli originali dei documenti falliti scaduti e i file senza documento.

## Requirements
- REQ-001 (functional): Il Compose avvia anche un servizio `worker` (stessa immagine del backend, processo RQ sulla coda
  `ingestion`) che condivide con il backend il volume `uploads` (`UPLOAD_DIR`); `GET /api/health` aggiunge il controllo
  `worker` (`ok` se almeno un worker RQ è registrato sulla coda, altrimenti `unreachable`) senza cambiare la regola
  200/503 (503 solo se database o Redis non sono `ok`).
- REQ-002 (functional): `POST /api/kbs/{kb_id}/documents` (solo admin, `multipart/form-data`, un solo file nel campo
  `file`, con l'intestazione `X-Requested-With: XMLHttpRequest`) salva l'originale in `UPLOAD_DIR` con il nome uguale
  all'id del documento, crea il documento in stato QUEUED, mette in coda il job e risponde 202 con `DocumentOut`; una
  richiesta senza l'intestazione riceve 422 `validation_error` e non crea nulla. Il nome file mostrato è ripulito da
  percorsi e caratteri di controllo (al massimo 255 caratteri) e non è mai usato come percorso sul disco.
- REQ-003 (functional): L'upload accetta file PDF, DOCX, PPTX, XLSX, TXT e MD fino a `MAX_UPLOAD_SIZE_MB` (default 20) e controlla
  che estensione (senza distinguere maiuscole), tipo MIME dichiarato (se presente e diverso da
  `application/octet-stream`) e firma del contenuto indichino lo stesso formato: un PDF inizia con `%PDF-`, un DOCX è
  uno ZIP che contiene `word/document.xml`, un PPTX è uno ZIP che contiene `ppt/presentation.xml`, un XLSX è uno ZIP
  che contiene `xl/workbook.xml`, TXT e MD sono UTF-8 valido senza byte NUL. Altrimenti 415
  `unsupported_type`; un file vuoto riceve 422 `empty_file`; un file più grande del limite riceve 413 `file_too_large`,
  con lo stesso corpo di errore anche quando il blocco avviene in Nginx. In ogni errore non restano documento né file.
- REQ-004 (functional): Un file con lo stesso checksum SHA-256 di un altro documento della stessa KB, in qualsiasi stato,
  riceve 409 `duplicate_document` con l'oggetto `existing` (`id` e `filename` del documento presente) nel corpo
  dell'errore; l'unicità di (KB, checksum) è garantita dal database anche con upload concorrenti. Lo stesso file in
  un'altra KB è ammesso.
- REQ-005 (functional): Tutte le API dei documenti sono riservate all'admin: senza sessione 401; a un `user` 403 prima di
  qualsiasi lettura, id o corpo; una KB sconosciuta o con id non UUID riceve 404 `kb_not_found`; un documento
  sconosciuto, con id non UUID o di un'altra KB riceve lo stesso 404 `document_not_found`.
- REQ-006 (functional): `GET /api/kbs/{kb_id}/documents` (dal più recente) e `GET /api/kbs/{kb_id}/documents/{document_id}`
  restituiscono `DocumentOut` = `{"id", "kb_id", "filename", "size_bytes", "status", "status_reason", "chunk_count",
  "created_at", "updated_at"}` con `status_reason` = `{"code", "message"}` oppure null; nessuna risposta contiene
  checksum, percorso o contenuto.
- REQ-007 (functional): Ogni documento ha uno stato fra QUEUED, PROCESSING, READY, FAILED e UNSUPPORTED; le uniche
  transizioni sono QUEUED→PROCESSING, PROCESSING→READY, PROCESSING→FAILED, PROCESSING→UNSUPPORTED e FAILED→QUEUED
  (riprova). FAILED e UNSUPPORTED hanno sempre `status_reason` con codice e messaggio non vuoti; gli altri stati lo
  hanno null. Un documento QUEUED, FAILED o UNSUPPORTED non ha chunk; un documento READY ha `chunk_count` uguale al
  numero dei suoi chunk, almeno uno.
- REQ-008 (functional): Il worker elabora un job alla volta. PDF, DOCX, PPTX e XLSX sono inviati a Docling (`DOCLING_BASE_URL`,
  intestazione `X-Api-Key` quando `DOCLING_API_KEY` è impostata, timeout `DOCLING_TIMEOUT_SECONDS`) che restituisce
  titoli, paragrafi, tabelle in forma di testo e numeri di pagina; TXT e MD sono letti direttamente come UTF-8 e le
  intestazioni Markdown (`#`) danno i titoli di sezione.
- REQ-009 (functional): Se il testo estratto ha meno di `MIN_EXTRACTED_CHARS` (default 50) caratteri non bianchi, il
  documento diventa UNSUPPORTED con codice `no_text_extracted` e l'originale è cancellato subito; non si fa OCR.
- REQ-010 (functional): Prima del chunking il testo perde i caratteri di controllo (tranne a capo e tabulazione), gli
  spazi e le righe vuote ripetuti e i paragrafi identici consecutivi, ed è normalizzato in Unicode NFC; lettere,
  accenti, cifre, codici e punteggiatura restano invariati.
- REQ-011 (functional): Il chunker è strutturale: un chunk appartiene a una sola sezione; punta a `CHUNK_TARGET_WORDS`
  (default 300) parole e non supera `CHUNK_MAX_WORDS` (default 450); i chunk consecutivi della stessa sezione condividono
  `CHUNK_OVERLAP_PERCENT` (default 15) per cento di `CHUNK_TARGET_WORDS`, cioè 45 parole; un resto sotto
  `CHUNK_MIN_WORDS` (default 50) si unisce al chunk precedente della sezione, e una sezione sotto il minimo dà un chunk
  solo; le righe di una tabella non vengono spezzate (una riga più lunga del massimo è un chunk a sé). Ogni chunk
  registra `position` (da 0, in ordine di lettura), `section_title` (percorso dei titoli, nullo senza titoli),
  `page_start` e `page_end` (nulli se la pagina non è nota), `content` e `word_count`; tolta la sovrapposizione, i chunk
  contengono tutto il testo normalizzato.
- REQ-012 (functional): I chunk sono inviati a Ollama (`POST {OLLAMA_BASE_URL}/api/embed`, modello `EMBEDDING_MODEL`) a
  gruppi di `EMBEDDING_BATCH_SIZE` (default 16) testi, con timeout `EMBEDDING_TIMEOUT_SECONDS`; se il numero dei vettori
  restituiti non è uguale a quello dei testi o la loro dimensione non è `EMBEDDING_DIMENSIONS` (default 1024), il
  documento diventa FAILED con codice `embedding_invalid_response` o `embedding_dimension_mismatch` e non viene salvato
  nessun chunk.
- REQ-013 (functional): Il profilo di indicizzazione riunisce modello di embedding, dimensioni, parser, parametri del
  chunker e configurazione BM25 (`conservative-v1`); all'avvio, se nessun profilo coincide con la configurazione
  corrente, ne viene creato uno; ogni documento e ogni chunk registrano il profilo con cui sono stati creati e non lo
  cambiano; il database rifiuta un chunk il cui vettore ha una dimensione diversa da quella del suo profilo.
- REQ-014 (functional): Tutti i chunk di un documento (testo, vettore, metadati, `kb_id`, `document_id`, profilo) sono
  salvati in una sola transazione; la tabella ha un indice vettoriale (HNSW, distanza coseno) e un indice BM25
  (pg_search) su `content` e `section_title` con `kb_id` e `document_id` come campi di filtro, e una ricerca BM25
  filtrata per `kb_id` restituisce solo chunk di quella KB.
- REQ-015 (functional): Dopo il commit il worker verifica che sia l'indice vettoriale sia l'indice BM25, filtrando per
  `document_id`, restituiscano tutti i chunk del documento; solo allora il documento passa a READY con il suo
  `chunk_count`; se la verifica non riesce, i chunk del documento sono cancellati e il documento diventa FAILED con
  codice `verification_failed`.
- REQ-016 (functional): Gli errori temporanei di Docling o Ollama (connessione rifiutata, timeout, risposta 5xx o 429)
  ripetono il passo fino a `INGESTION_MAX_ATTEMPTS` (default 3) tentativi in tutto, con attesa
  `INGESTION_RETRY_BACKOFF_SECONDS` (default 10) raddoppiata a ogni tentativo; gli altri errori (altri 4xx, file non
  interpretabile, risposta incoerente) non si ripetono. Esauriti i tentativi, o subito per un errore non temporaneo, il
  documento diventa FAILED con uno dei codici `docling_unreachable`, `docling_error`, `parse_failed`,
  `embedding_unreachable`, `embedding_error` o `unexpected_error` e un messaggio.
- REQ-017 (functional): L'originale è cancellato quando il documento diventa READY o UNSUPPORTED e quando il documento
  o la sua KB sono cancellati; per un documento FAILED resta fino a `FAILED_ORIGINAL_RETENTION_DAYS` (default 7) giorni.
  Alla partenza e ogni ora il worker cancella gli originali scaduti e i file di `UPLOAD_DIR` senza documento e più
  vecchi di un'ora; il documento FAILED resta, con il suo motivo.
- REQ-018 (functional): `POST /api/kbs/{kb_id}/documents/{document_id}/retry` (solo admin) riporta a QUEUED un documento
  FAILED il cui originale esiste ancora e risponde 202; per un documento in un altro stato risponde 409
  `not_retryable`, per un documento FAILED con l'originale già cancellato 409 `original_missing`.
- REQ-019 (functional): `DELETE /api/kbs/{kb_id}/documents/{document_id}` (solo admin) risponde 204; documento, chunk e
  originale spariscono anche se il documento è in elaborazione, e un job in corso non li ricrea. La cancellazione di una
  KB rimuove i suoi documenti, i chunk e gli originali.
- REQ-020 (functional): La pagina Documenti (menu e rotta `/admin/documents`, solo admin; un `user` va a `/`) permette
  di scegliere una KB, selezionare uno o più file (ognuno inviato in una richiesta propria), vedere per ogni documento
  nome, dimensione, stato, motivo tradotto e data, riprovare un documento FAILED con l'originale presente e cancellare
  un documento dopo conferma; l'elenco si aggiorna da solo finché c'è un documento QUEUED o PROCESSING; gli errori di
  upload e i motivi sono mostrati per file, tradotti nella lingua dell'interfaccia.
- REQ-101 (non-functional): Mentre il worker elabora un documento, `GET /api/health` e le altre API rispondono entro
  `HEALTH_CHECK_TIMEOUT_SECONDS` più 1 secondo.
- REQ-102 (non-functional): Né il backend né il worker scrivono nei log il contenuto dei documenti, i nomi dei file o i
  vettori; i log di un'elaborazione contengono l'id del documento, lo stato e il codice del motivo.

## Acceptance criteria
- AC-001 [REQ-001]: Given il Compose avviato con un `.env` compilato, when si elencano i servizi, then sono cinque (`nginx`,
  `backend`, `redis`, `db`, `worker`), solo `nginx` pubblica porte sull'host, backend e worker montano lo stesso volume
  `uploads` e `GET /api/health` mostra `worker: ok`.
- AC-002 [REQ-001]: Given database e Redis attivi e nessun worker registrato sulla coda, when si chiama `GET /api/health`,
  then la risposta è 200 con `status: degraded` e `worker: unreachable`.
- AC-003 [REQ-002]: Given l'admin e una KB, when carica un TXT valido di 1 KB, then la risposta è 202 con `DocumentOut` in
  stato QUEUED, `filename` e `size_bytes` corretti, e in `UPLOAD_DIR` esiste un file con nome uguale all'id del documento.
- AC-004 [REQ-002]: Given un file chiamato `../../segreto/passwd.txt` (e uno con caratteri di controllo nel nome), when l'admin
  lo carica, then il nome mostrato è ripulito (`passwd.txt`) e in `UPLOAD_DIR` esiste solo il file con il nome uguale
  all'id.
- AC-005 [REQ-002]: Given una richiesta di upload senza l'intestazione `X-Requested-With`, when arriva con la sessione
  dell'admin, then 422 `validation_error` e non esistono né documento né file.
- AC-006 [REQ-003]: Given un `.pdf` che non inizia con `%PDF-`, un `.docx` che non è uno ZIP con `word/document.xml`, un
  `.pptx` che non è uno ZIP con `ppt/presentation.xml`, un `.xlsx` che non è uno ZIP con `xl/workbook.xml`, un
  `.txt` con byte NUL, un `.md` non UTF-8, un `.exe` e un `.pdf` dichiarato `image/png`, when si caricano, then ognuno
  riceve 415 `unsupported_type` e non esistono né documento né file.
- AC-007 [REQ-003]: Given un file vuoto, when si carica, then 422 `empty_file` e non esistono né documento né file.
- AC-008 [REQ-003]: Given `MAX_UPLOAD_SIZE_MB` uguale a 1, when si carica un file di 1 MB più 1 byte, then 413
  `file_too_large` senza documento né file, e un file di esattamente 1 MB è accettato.
- AC-009 [REQ-003]: Given lo stack completo dietro Nginx, when si invia un corpo più grande del limite, then la risposta è
  413 con il corpo di errore JSON `file_too_large`, e quando si invia un file entro il limite la richiesta arriva al
  backend.
- AC-010 [REQ-004]: Given un documento A in una KB, in ciascuno degli stati QUEUED, READY, FAILED e UNSUPPORTED, when
  l'admin carica nella stessa KB un file con lo stesso contenuto e un altro nome, then 409 `duplicate_document` con
  `existing.id` uguale all'id di A, nessun nuovo documento e nessun nuovo file; nell'altra KB lo stesso file è accettato.
- AC-011 [REQ-004]: Given due upload simultanei dello stesso file nella stessa KB, when arrivano insieme, then uno
  riceve 202, l'altro 409 `duplicate_document`, e esiste un solo documento.
- AC-012 [REQ-005]: Given un utente `user` (anche assegnato alla KB) e una richiesta senza sessione, when chiamano upload,
  elenco, dettaglio, riprova e cancellazione, then il `user` riceve 403 su tutte e senza sessione 401, e non cambia nulla.
- AC-013 [REQ-005]: Given l'admin, when usa un `kb_id` sconosciuto o non UUID, then 404 `kb_not_found`, e per un
  `document_id` sconosciuto, non UUID o di un'altra KB 404 `document_not_found` con lo stesso corpo.
- AC-014 [REQ-006]: Given tre documenti caricati in momenti diversi, when l'admin chiede l'elenco, then arrivano dal più
  recente al più vecchio con esattamente le chiavi di `DocumentOut`, e nessuna risposta dei documenti contiene checksum,
  percorso o contenuto.
- AC-015 [REQ-007]: Given un TXT valido e i server finti di Docling e Ollama, when il worker lo elabora, then lo stato
  passa da QUEUED a PROCESSING a READY, `status_reason` è null e `chunk_count` è uguale al numero dei chunk salvati.
- AC-016 [REQ-007]: Given un errore dell'embedding sull'ultimo gruppo di un documento, when il job termina, then il
  documento è FAILED con `status_reason` non vuoto e nessun suo chunk esiste.
- AC-017 [REQ-008]: Given un PDF, when il worker lo elabora con `DOCLING_API_KEY` impostata, then Docling riceve il file e
  l'intestazione `X-Api-Key` con quel valore; given un DOCX, un PPTX o un XLSX la stessa cosa; given un TXT o un MD,
  Docling non viene chiamato.
- AC-018 [REQ-008]: Given un MD con `# Guida`, `## Installazione` e due paragrafi sotto ciascuna, when viene elaborato,
  then i chunk sotto la seconda intestazione hanno `section_title` uguale a `Guida > Installazione`.
- AC-019 [REQ-009]: Given un PDF per cui Docling restituisce 20 caratteri di testo, when il worker lo elabora, then il
  documento è UNSUPPORTED con codice `no_text_extracted`, l'originale non esiste più e non c'è nessun chunk.
- AC-020 [REQ-010]: Given un testo con caratteri di controllo, spazi ripetuti, tre righe vuote di seguito, un paragrafo
  ripetuto due volte di seguito e lettere accentate in forma NFD, when viene normalizzato, then il risultato non ha
  caratteri di controllo né ripetizioni, le lettere accentate sono in NFC e il resto del testo è identico.
- AC-021 [REQ-011]: Given un documento di tre sezioni di 700, 40 e 320 parole con le impostazioni predefinite, when viene
  diviso, then nessun chunk contiene parole di due sezioni, nessun chunk supera 450 parole, due chunk consecutivi della
  prima sezione condividono esattamente 45 parole, e la sezione da 40 parole dà un solo chunk con tutte le sue parole.
- AC-022 [REQ-011]: Given una tabella di 30 righe di circa 20 parole in forma di testo, when viene divisa, then ogni chunk
  inizia con una riga intera e nessuna riga è spezzata fra due chunk.
- AC-023 [REQ-011]: Given il documento diviso, when si leggono i chunk, then `position` va da 0 a n-1 nell'ordine di
  lettura, `page_start` non supera `page_end` quando sono noti, e unire i chunk togliendo la sovrapposizione dà il testo
  normalizzato completo.
- AC-024 [REQ-012]: Given 40 chunk e `EMBEDDING_BATCH_SIZE` uguale a 16, when il worker calcola gli embedding, then Ollama
  riceve tre richieste di 16, 16 e 8 testi con il modello `EMBEDDING_MODEL`, e ogni vettore salvato è quello del proprio
  chunk.
- AC-025 [REQ-012]: Given Ollama che restituisce vettori di 768 dimensioni con `EMBEDDING_DIMENSIONS` uguale a 1024, e
  un caso con meno vettori che testi, when il worker elabora, then il documento è FAILED con
  `embedding_dimension_mismatch` e con `embedding_invalid_response` e non esiste nessun chunk.
- AC-026 [REQ-013]: Given un database vuoto, when il backend parte due volte con la stessa configurazione, then esiste un
  solo profilo; given un `EMBEDDING_MODEL` o delle dimensioni diversi, then ne esiste un secondo e i documenti e i chunk
  già salvati mantengono il loro profilo.
- AC-027 [REQ-013]: Given un profilo di 4 dimensioni, when si inserisce direttamente nel database un chunk con un vettore di
  3 dimensioni, then il database lo rifiuta.
- AC-028 [REQ-014]: Given due KB con un documento ciascuna che contengono la stessa parola, when si esegue una ricerca BM25
  su quella parola filtrata per `kb_id`, then restituisce solo i chunk di quella KB, e gli indici vettoriale e BM25
  esistono sulla tabella dei chunk con i campi previsti.
- AC-029 [REQ-014]: Given un errore forzato durante il salvataggio del terzo chunk, when la transazione fallisce, then
  nessun chunk del documento è visibile e il documento è FAILED.
- AC-030 [REQ-015]: Given un documento appena salvato, when il worker verifica gli indici, then una ricerca vettoriale e una
  BM25 filtrate per `document_id` restituiscono tutti i suoi chunk e solo dopo il documento è READY; given una verifica
  che trova meno chunk di quelli salvati, then i chunk sono cancellati e il documento è FAILED con `verification_failed`.
- AC-031 [REQ-016]: Given Docling che risponde 503 due volte e poi correttamente, when il worker elabora un PDF, then il
  documento diventa READY dopo tre tentativi, con attese di `INGESTION_RETRY_BACKOFF_SECONDS` e del doppio.
- AC-032 [REQ-016]: Given Docling irraggiungibile in ogni tentativo, when il worker elabora un PDF, then dopo esattamente
  `INGESTION_MAX_ATTEMPTS` tentativi il documento è FAILED con `docling_unreachable` e l'originale esiste ancora; given
  Ollama irraggiungibile, lo stesso con `embedding_unreachable` e nessun chunk.
- AC-033 [REQ-016]: Given Docling che risponde 422 perché non riesce a interpretare il file, when il worker elabora un PDF,
  then il documento è FAILED con `parse_failed` dopo un solo tentativo.
- AC-034 [REQ-017]: Given un documento READY, when l'elaborazione termina, then l'originale non esiste più; given due
  documenti FAILED con originale di 8 e di 6 giorni e `FAILED_ORIGINAL_RETENTION_DAYS` uguale a 7, when la pulizia gira,
  then solo l'originale di 8 giorni è cancellato e entrambi i documenti restano FAILED con il loro motivo.
- AC-035 [REQ-017]: Given un file in `UPLOAD_DIR` senza documento, vecchio di due ore, e uno vecchio di dieci minuti, when
  la pulizia gira, then solo il primo è cancellato.
- AC-036 [REQ-018]: Given un documento FAILED con l'originale presente e Docling di nuovo raggiungibile, when l'admin chiama
  «riprova», then 202, lo stato è QUEUED e poi READY; given un documento READY, 409 `not_retryable`; given un
  documento FAILED con l'originale cancellato, 409 `original_missing`.
- AC-037 [REQ-019]: Given un documento READY con chunk, when l'admin lo cancella, then 204, un `GET` successivo dà 404
  `document_not_found` e non esistono né chunk né originale.
- AC-038 [REQ-019]: Given un documento in stato PROCESSING con Docling lento, when l'admin lo cancella e poi il job
  termina, then non esistono né documento né chunk né originale.
- AC-039 [REQ-019]: Given una KB con un documento READY e uno FAILED con originale, when l'admin cancella la KB, then
  documenti, chunk e originali non esistono più.
- AC-040 [REQ-020]: Given l'admin sulla pagina Documenti con una KB scelta, when seleziona due file validi, then compaiono
  due righe con nome, dimensione e stato, e ciascuna arriva a «Pronto» senza ricaricare la pagina.
- AC-041 [REQ-020]: Given un duplicato, un file di tipo non ammesso e un file oltre il limite, when l'admin li seleziona,
  then ognuno mostra un messaggio tradotto accanto al proprio nome e nessuna riga viene aggiunta.
- AC-042 [REQ-020]: Given un documento FAILED con l'originale presente, when l'admin guarda la riga, then vede il motivo
  tradotto e il pulsante «Riprova», che lo riporta a «In coda»; un documento READY o UNSUPPORTED non ha il pulsante.
- AC-043 [REQ-020]: Given un documento nell'elenco, when l'admin sceglie «Elimina», then compare una conferma: «Annulla»
  lascia la riga, «Conferma» la rimuove.
- AC-044 [REQ-020]: Given un utente `user`, when apre l'interfaccia o la rotta `/admin/documents`, then non vede il menu
  Documenti e viene portato a `/`.
- AC-045 [REQ-101]: Given un job in corso con Docling che risponde dopo 30 secondi, when si chiamano `GET /api/health` e
  `GET /api/kbs`, then entrambe rispondono entro `HEALTH_CHECK_TIMEOUT_SECONDS` più 1 secondo.
- AC-046 [REQ-102]: Given un documento con contenuto e nome riconoscibili elaborato con successo e uno elaborato con un
  errore, when si raccolgono tutti i log di backend e worker, then nessuna riga contiene il contenuto, il nome del file o
  un vettore, e le righe dell'elaborazione contengono l'id del documento e il codice del motivo.

## Constraints and assumptions
- Vincoli dal documento di architettura e dalla fase 1: modular monolith; Docling e Ollama esterni (i test usano server
  finti, mai servizi reali); PostgreSQL con pgvector e pg_search; coda Redis + RQ; esecuzione su CPU; solo chunk e
  metadati conservati; ogni accesso a una KB passa da `KnowledgeBaseRepository` o da una query unita a `kb_members`
  (AGENTS.md); formato degli errori e ordine dei controlli come in `docs/system/fondazioni.md`.
- Decisioni dell'owner per questa fase: formati PDF, DOCX, PPTX, XLSX, TXT e MD (PPTX e XLSX con le stesse regole di
  chunking degli altri, senza regole dedicate); perimetro senza re-indicizzazione; l'originale dei documenti FAILED resta fino a
  7 giorni per poter riprovare; duplicati rifiutati con 409; chunk da circa 300 parole con 15% di sovrapposizione;
  limite di 20 MB per file; documenti visibili solo all'admin; dopo il cambio di modello gli utenti ricaricano i
  documenti (i vettori di profili diversi non si mescolano).
- Nuove variabili d'ambiente, tutte in `.env.example` (il test di AC-033 della fase 1 lo verifica) e i segreti vuoti:
  `UPLOAD_DIR`, `MAX_UPLOAD_SIZE_MB`, `DOCLING_API_KEY`, `DOCLING_TIMEOUT_SECONDS`, `EMBEDDING_MODEL`,
  `EMBEDDING_DIMENSIONS`, `EMBEDDING_BATCH_SIZE`, `EMBEDDING_TIMEOUT_SECONDS`, `INGESTION_MAX_ATTEMPTS`,
  `INGESTION_RETRY_BACKOFF_SECONDS`, `CHUNK_TARGET_WORDS`, `CHUNK_MIN_WORDS`, `CHUNK_MAX_WORDS`,
  `CHUNK_OVERLAP_PERCENT`, `MIN_EXTRACTED_CHARS`, `FAILED_ORIGINAL_RETENTION_DAYS`.
- Le dipendenze nuove (client RQ e un modo di scrivere i vettori) e il resto delle scelte tecniche stanno nel piano e in
  un ADR-002, da approvare prima del codice.
- Confermato dall'owner (2026-11-07): Analyzer BM25 comune e conservativo (`conservative-v1`: minuscole e normalizzazione degli accenti, senza
  stemming), da validare col benchmark italiano/inglese della fase 4.
- Confermato dall'owner (2026-11-07): TXT e MD sono letti direttamente dal worker come UTF-8 e non passano da Docling; PDF e DOCX sì.
- Confermato dall'owner (2026-11-07): 3 tentativi con attesa di 10 secondi raddoppiata; timeout di 600 secondi per Docling e di 120 per Ollama;
  sotto 50 caratteri estratti il documento è UNSUPPORTED; minimo e massimo di chunk di 50 e 450 parole.
- Confermato dall'owner (2026-11-07): Una parola è una sequenza di caratteri non bianchi separata da spazi; le stime in token non servono.
- Confermato dall'owner (2026-11-07): L'elenco dei documenti si aggiorna ogni 3 secondi finché c'è un documento QUEUED o PROCESSING.
- Confermato dall'owner (2026-11-07): La pulizia dei file orfani e scaduti gira all'avvio del worker e ogni ora.

## Open questions
- La tua istanza docling-serve gestisce PPTX e XLSX: accetta anche TXT e MD? Se sì si possono far passare da Docling — blocking: no
- Hardware di destinazione, volumi di documenti e tempi di elaborazione attesi per file (obiettivi di prestazione) — blocking: no
- Dataset e metriche per validare l'analyzer BM25, il chunking e la qualità dei chunk su PPTX e XLSX (fase 4) — blocking: no
- Se il piano supera gli 8 task, la pagina Documenti (REQ-020) passa a una spec separata — blocking: no

## Revisions
- none
