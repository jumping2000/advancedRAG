# SPEC-2026-11-06-fase-1-fondazioni: Fase 1 – Fondamenta (stack, autenticazione, utenti, KB)

- Status: implemented
- Owner: jumping2000

## Problem and goal
La piattaforma RAG (documento di architettura del 2026-11-06) parte da un repository vuoto. La fase 1 costruisce le
fondamenta su cui le fasi successive (2 ingestion, 3 retrieval ibrido e chat, 4 reranker, query rewriting e
valutazione) aggiungono funzionalità: lo stack Docker Compose, l'autenticazione con utenti locali, la gestione degli
utenti e delle Knowledge Base (KB) e l'isolamento per KB.

Criterio di successo: su una macchina pulita, con un `.env` compilato, `docker compose up --build` avvia lo stack; l'admin
creato dal `.env` entra dalla UI servita da Nginx, crea un utente e due KB, assegna all'utente una sola KB; l'utente
vede e apre solo quella; `python3 scripts/verify.py` e la CI sono verdi, inclusi i test di isolamento.

## Users / actors
- Admin: installa la piattaforma, gestisce utenti e KB, assegna gli utenti alle KB.
- Utente: accede e vede solo le KB che gli sono assegnate (nelle fasi successive vi farà chat).

## Scope
- Included: Docker Compose (Nginx, backend FastAPI, Redis, ParadeDB); configurazione degli URL di Ollama e Docling
  esterni con health check; creazione del primo admin da variabili d'ambiente; login e logout con sessione; gestione
  utenti; gestione KB e assegnazioni; autorizzazione per KB nelle API e nelle query SQL; UI in italiano e inglese con
  selettore; passi di `verify` e CI per backend e frontend.
- **Non-scope (explicit):** tutto ciò che segue resta fuori dalla fase 1.
  - documenti, upload, ingestion e worker RQ (fase 2); retrieval, chat, conversazioni e citazioni (fase 3); reranker,
    query rewriting, valutazione e meccanismo CPU/GPU (fase 4);
  - Ollama e Docling dentro il Compose: restano servizi esterni;
  - SSO/OIDC, auto-registrazione, recupero password via email, cambio password da parte dell'utente;
  - limitazione dei tentativi di login, blocco account, audit log;
  - ruoli diversi da `admin` e `user`, ruoli per singola KB;
  - interrogazioni su più KB insieme;
  - backup, monitoraggio, certificati gestiti automaticamente (Let's Encrypt).
- **Anti-goals:** la fase 1 fallisce, anche se funziona, se:
  - un utente ottiene dati, nomi o esistenza di una KB a cui non è assegnato;
  - contenuti o credenziali inviati a servizi esterni non configurati esplicitamente dall'admin;
  - segreti (password, chiavi) nel repository, nei log o nelle risposte delle API;
  - un errore di configurazione (variabile mancante, servizio esterno irraggiungibile) senza un messaggio che dica quale.

## Current behaviour and impact
- none (new area)

## Use cases
- UC-001 Primo avvio: l'admin compila `.env`, avvia il Compose; il backend crea l'admin se non ne esiste uno attivo.
  Errore: variabili dell'admin mancanti e nessun admin nel database → il backend non parte e indica le variabili.
- UC-002 Accesso: l'attore inserisce utente e password; riceve una sessione e vede la sua pagina iniziale. Errori:
  credenziali errate o utente disattivato → messaggio generico, nessuna sessione.
- UC-003 Gestione utenti: l'admin crea un utente, lo disattiva o riattiva, ne reimposta la password. Errori: nome già
  usato, password troppo corta, disattivazione di sé stesso.
- UC-004 Gestione KB: l'admin crea, rinomina e cancella KB e assegna o toglie utenti. Errore: nome già usato.
- UC-005 Consultazione: l'utente vede l'elenco delle sue KB e ne apre una; una KB non assegnata non è raggiungibile.
- UC-006 Lingua: la UI parte nella lingua del browser; l'attore la cambia dal selettore e la ritrova al login seguente.

## Requirements
- REQ-001 (functional): Il Compose avvia i servizi `nginx`, `backend`, `redis` e `db` (immagine ParadeDB con le
  estensioni `vector` e `pg_search` installate); solo `nginx` espone una porta sull'host (`HTTP_PORT`, default 80), serve
  il frontend e inoltra al backend le richieste sotto `/api/`.
- REQ-002 (functional): Se nella cartella montata `nginx/certs/` esistono `cert.pem` e `key.pem`, Nginx risponde anche in
  HTTPS (`HTTPS_PORT`, default 443) e reindirizza con 301 le richieste HTTP a HTTPS; se non esistono risponde solo in HTTP.
- REQ-003 (functional): `GET /api/health` restituisce lo stato (`ok` o `unreachable`) di database, Redis, Ollama
  (`OLLAMA_BASE_URL`) e Docling (`DOCLING_BASE_URL`), attendendo al massimo `HEALTH_CHECK_TIMEOUT_SECONDS` per ciascuno;
  risponde 200 se database e Redis sono `ok`, 503 altrimenti; Ollama e Docling irraggiungibili non impediscono l'avvio.
- REQ-004 (functional): All'avvio, se non esiste un admin attivo, il backend crea l'utente `ADMIN_USERNAME` con ruolo
  `admin` e password `ADMIN_PASSWORD`; se un admin attivo esiste, le due variabili vengono ignorate e nessun utente viene
  modificato; se l'admin va creato e una delle due variabili manca, il backend termina con un messaggio che nomina la
  variabile.
- REQ-005 (functional): `POST /api/auth/login` con utente e password corretti di un utente attivo crea una sessione
  lato server e imposta un cookie `HttpOnly`, `SameSite=Lax`, `Secure` quando la richiesta arriva in HTTPS; con
  credenziali errate o utente disattivato risponde 401 con lo stesso messaggio generico; `POST /api/auth/logout` invalida
  la sessione. Una sessione scade dopo `SESSION_IDLE_TIMEOUT_MINUTES` minuti senza richieste (default 480).
- REQ-006 (functional): Le password sono salvate solo come hash Argon2id; una password più corta di 12 caratteri viene
  rifiutata con 422 sia alla creazione sia al reset; nessuna risposta API né riga di log contiene password o hash.
- REQ-007 (functional): L'admin crea utenti (nome univoco senza distinzione maiuscole/minuscole, 3–64 caratteri tra
  lettere, cifre, `.`, `_`, `-`; ruolo `admin` o `user`; password iniziale), li elenca, li disattiva e riattiva e ne
  reimposta la password; disattivare un utente o reimpostarne la password invalida le sue sessioni; l'admin non può
  disattivare sé stesso (409).
- REQ-008 (functional): L'admin crea KB (nome univoco senza distinzione maiuscole/minuscole, 1–100 caratteri;
  descrizione opzionale fino a 500 caratteri), le rinomina e le cancella; la cancellazione rimuove anche le assegnazioni.
- REQ-009 (functional): L'admin assegna e toglie utenti a una KB; l'assegnazione di un utente già assegnato non crea
  duplicati.
- REQ-010 (functional): Un utente con ruolo `user` elenca solo le KB a lui assegnate; una richiesta su una KB non
  assegnata o inesistente risponde 404 con lo stesso corpo; le API di amministrazione rispondono 403 a un `user`; ogni
  API diversa da login e health risponde 401 senza sessione valida. Il filtro per utente è applicato nella query SQL.
- REQ-011 (functional): La UI parte in italiano se la prima lingua del browser inizia con `it`, altrimenti in inglese;
  il selettore cambia la lingua senza ricaricare la pagina e la salva nel profilo dell'utente, che la ritrova al login
  successivo; tutti i testi visibili esistono in entrambe le lingue.
- REQ-012 (functional): La UI offre: pagina di login; elenco delle proprie KB per l'utente; per l'admin le pagine
  Utenti (crea, disattiva/riattiva, reimposta password) e Knowledge Base (crea, rinomina, cancella con conferma,
  assegna e togli utenti); i messaggi d'errore delle API sono mostrati nella lingua della UI.
- REQ-101 (non-functional): `python3 scripts/verify.py` esegue format, lint, controllo dei tipi e test di backend e
  frontend oltre al controllo dei segreti; la CI lo esegue su ogni PR.
- REQ-102 (non-functional): Il repository contiene `.env.example` con tutte le variabili usate e i segreti vuoti; `.env`
  è ignorato da git.

## Acceptance criteria
- AC-001 [REQ-001]: Given un `.env` compilato, when si esegue `docker compose up --build`, then i quattro servizi sono
  attivi, solo `nginx` pubblica una porta sull'host, `GET /` restituisce la pagina del frontend e `GET /api/health`
  risponde dal backend.
- AC-002 [REQ-001]: Given il database avviato, when si interroga `pg_extension`, then risultano installate `vector` e
  `pg_search`.
- AC-003 [REQ-002]: Given `cert.pem` e `key.pem` in `nginx/certs/`, when si richiede `http://<host>/`, then la risposta è
  301 verso `https://`, e la richiesta HTTPS restituisce il frontend.
- AC-004 [REQ-002]: Given nessun certificato in `nginx/certs/`, when si avvia Nginx, then parte e risponde in HTTP.
- AC-005 [REQ-003]: Given database e Redis attivi e Ollama irraggiungibile, when si chiama `GET /api/health`, then la
  risposta è 200 con `ollama: unreachable` entro `HEALTH_CHECK_TIMEOUT_SECONDS` più 1 secondo.
- AC-006 [REQ-003]: Given Redis fermo, when si chiama `GET /api/health`, then la risposta è 503 con `redis: unreachable`.
- AC-007 [REQ-004]: Given un database senza admin e le variabili `ADMIN_USERNAME` e `ADMIN_PASSWORD`, when il backend
  parte due volte, then esiste un solo admin con quel nome e il login con quella password riesce.
- AC-008 [REQ-004]: Given un admin attivo e `ADMIN_PASSWORD` cambiata nel `.env`, when il backend riparte, then la
  password dell'admin resta quella precedente.
- AC-009 [REQ-004]: Given un database senza admin e `ADMIN_PASSWORD` mancante, when il backend parte, then termina con
  codice diverso da 0 e un messaggio che contiene `ADMIN_PASSWORD`.
- AC-010 [REQ-005]: Given un utente attivo, when fa login con la password corretta, then riceve 200 e un cookie di
  sessione `HttpOnly` con `SameSite=Lax`, e `GET /api/auth/me` restituisce nome, ruolo e lingua.
- AC-011 [REQ-005]: Given un utente esistente, when fa login con password errata, e un nome inesistente fa login, then
  entrambe le risposte sono 401 con lo stesso corpo.
- AC-012 [REQ-005]: Given una sessione attiva, when l'utente fa logout, then una richiesta successiva con lo stesso
  cookie riceve 401.
- AC-013 [REQ-005]: Given una sessione senza richieste da più di `SESSION_IDLE_TIMEOUT_MINUTES`, when arriva una
  richiesta con quel cookie, then la risposta è 401.
- AC-014 [REQ-006]: Given la creazione di un utente con password di 11 caratteri, when l'admin la invia, then la risposta
  è 422 e l'utente non esiste.
- AC-015 [REQ-006]: Given un utente creato, when si legge la sua riga nel database e le risposte delle API utenti, then
  la colonna contiene un hash Argon2id (`$argon2id$`) e nessuna risposta contiene la password o l'hash.
- AC-016 [REQ-007]: Given un utente `mario`, when l'admin crea `Mario`, then la risposta è 409.
- AC-017 [REQ-007]: Given un utente con una sessione attiva, when l'admin lo disattiva, then la sua richiesta successiva
  riceve 401 e un nuovo login riceve 401; dopo la riattivazione il login riesce.
- AC-018 [REQ-007]: Given un utente con una sessione attiva, when l'admin gli reimposta la password, then la vecchia
  sessione riceve 401 e il login riesce solo con la nuova password.
- AC-019 [REQ-007]: Given l'admin autenticato, when chiede di disattivare sé stesso, then la risposta è 409 e resta attivo.
- AC-020 [REQ-008]: Given una KB `Manuali`, when l'admin crea o rinomina un'altra KB in `manuali`, then la risposta è 409.
- AC-021 [REQ-008]: Given una KB con due utenti assegnati, when l'admin la cancella, then la KB e le sue assegnazioni non
  esistono più e i due utenti non la vedono nel loro elenco.
- AC-022 [REQ-009]: Given un utente già assegnato a una KB, when l'admin lo assegna di nuovo, then esiste una sola
  assegnazione.
- AC-023 [REQ-010]: Given un utente assegnato alla KB A e non alla KB B, when elenca le KB, then vede solo A.
- AC-024 [REQ-010]: Given lo stesso utente, when chiede la KB B e una KB con id inesistente, then entrambe le risposte
  sono 404 con lo stesso corpo.
- AC-025 [REQ-010]: Given un utente con ruolo `user`, when chiama un'API di amministrazione (utenti, creazione KB,
  assegnazioni), then la risposta è 403.
- AC-026 [REQ-010]: Given nessun cookie di sessione, when si chiama un'API diversa da login e health, then la risposta è
  401.
- AC-027 [REQ-011]: Given un browser con lingua `it-IT` e nessuna preferenza salvata, when si apre la pagina di login,
  then i testi sono in italiano; con `de-DE` sono in inglese.
- AC-028 [REQ-011]: Given un utente che sceglie l'inglese dal selettore, when esce e rientra da un browser in italiano,
  then la UI è in inglese.
- AC-029 [REQ-011]: Given i file di traduzione, when il test confronta le chiavi italiane e inglesi, then gli insiemi
  sono uguali.
- AC-030 [REQ-012]: Given l'admin autenticato, when crea dalla UI un utente e una KB e assegna l'utente, then l'utente,
  autenticandosi, vede la KB nel suo elenco.
- AC-031 [REQ-012]: Given un utente `user` autenticato, when apre la UI, then non vede i menu Utenti e Knowledge Base
  di amministrazione.
- AC-032 [REQ-101]: Given una PR, when la CI parte, then esegue `python3 scripts/verify.py` con i passi di backend e
  frontend e lo stato è visibile sulla PR.
- AC-033 [REQ-102]: Given il repository, when si confrontano le variabili lette dal backend e dal Compose con
  `.env.example`, then ognuna è presente in `.env.example` e i segreti hanno valore vuoto.

## Constraints and assumptions
- Vincoli dal documento di architettura: modular monolith FastAPI; React + TypeScript; ParadeDB; Redis; Docker Compose;
  nessun Kafka, Kubernetes o service mesh.
- Vincoli decisi per la v1 (usati nelle fasi successive): gli originali sono eliminati dopo l'ingestion e un cambio di
  parser o chunker richiede di ricaricare i documenti; ogni conversazione appartiene a una sola KB; il query rewriting
  usa un provider configurabile come la generazione; la scelta CPU o GPU vale per il reranker (fase 4), con un solo
  tipo di immagine attivo alla volta.
- Ollama e Docling sono esterni: i test automatici usano server finti; la prova con i servizi reali è manuale.
- Confermato dall'owner (2026-11-06): Sessione lato server salvata in Redis, durata d'inattività 480 minuti.
- Confermato dall'owner (2026-11-06): Password minima di 12 caratteri, senza altre regole di composizione.
- Confermato dall'owner (2026-11-06): Porte di default 80 e 443, modificabili con `HTTP_PORT` e `HTTPS_PORT`.
- Confermato dall'owner (2026-11-06): Uso su rete aziendale o dietro firewall: la limitazione dei tentativi di login è rimandata.

## Open questions
- Dimensione e sovrapposizione dei chunk, gestione dei duplicati, limiti dei file (fase 2) — blocking: no
- Conversazioni salvate o no, risposta in streaming, formato delle citazioni (fase 3) — blocking: no
- Hardware di destinazione, volumi di documenti e utenti, tempi di risposta attesi (fasi 3–4) — blocking: no
- Dataset e metriche di valutazione del retrieval e della generazione (fase 4) — blocking: no

## Revisions
- none
