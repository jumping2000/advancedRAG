# SPEC-2026-11-20-fase-5-documentazione: Fase 5 – Documentazione IT/EN, README con badge e controllo finale

- Status: approved
- Owner: jumping2000

## Problem and goal
Le fasi 1-4 hanno costruito la piattaforma, ma il repository pubblico non ha un `README`, una licenza o una guida per chi
la installa e la usa. La documentazione esistente (`docs/system/`, spec e ADR) è tecnica, in inglese, e scritta per chi
sviluppa. La fase 5 aggiunge:
- un `README.md` in inglese e un `README.it.md` in italiano, con i badge di GitHub;
- quattro guide, ciascuna in italiano e in inglese: utente, admin, installazione e operatività, architettura e sviluppo;
- le schermate dell'interfaccia nelle due lingue;
- la licenza Apache-2.0;
- un controllo finale delle fasi 1-4, con un rapporto e un bug per ogni difetto trovato.

Il comportamento dell'applicazione non cambia.

Criterio di successo:
- un nuovo admin installa lo stack e configura ogni variabile del `.env` leggendo solo la guida, in una delle due lingue;
- i test di allineamento e `docs_check.py` sono verdi;
- il rapporto del controllo finale elenca l'esito di ogni controllo e ogni difetto ha il suo file in `docs/bugs/`.

## Users / actors
- Visitatore del repository: capisce cosa fa l'applicazione, con quale stack e in che stato è, dal README nella sua lingua.
- Admin che installa: avvia lo stack, configura i servizi esterni e le variabili, verifica la salute.
- Admin che gestisce: utenti, knowledge base, documenti, conversazioni e impostazioni.
- Utente: accede, sceglie una KB, fa domande e legge le fonti.
- Sviluppatore: capisce l'architettura, il flusso SDD, la verifica e come contribuire.

## Scope
- Included:
  - `README.md` (inglese) e `README.it.md` (italiano), con i badge;
  - le guide in `docs/guide/en/` e `docs/guide/it/`, con le schermate in `docs/guide/images/en/` e `docs/guide/images/it/`;
  - un modo ripetibile e facoltativo di catturare le schermate dallo stack di test;
  - il file `LICENSE` con il testo della licenza Apache-2.0;
  - i test di allineamento della documentazione (variabili del `.env.example`, parità tra le due lingue);
  - il rapporto del controllo finale in `docs/audit/` e un file in `docs/bugs/` per ogni difetto trovato.
- **Non-scope (explicit):** restano fuori dalla fase 5 i punti seguenti.
  - qualsiasi modifica al comportamento dell'applicazione: codice in `backend/app/`, `frontend/src/`, migrazioni, Compose,
    Nginx, testi dell'interfaccia;
  - la correzione dei difetti trovati dal controllo finale: ognuno diventa un bug in `docs/bugs/`, corretto dopo con
    `sdd-bugfix`;
  - la traduzione in italiano di `docs/system/`, delle spec e delle ADR, che restano come sono e vengono collegate;
  - sito di documentazione (GitHub Pages, MkDocs) e Wiki di GitHub;
  - badge che richiedono servizi esterni da configurare (copertura dei test, Codecov, analisi della qualità), e badge di
    release o versione (il repository non ha tag);
  - file `NOTICE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` e `SECURITY.md` separati: le regole per contribuire stanno
    nella guida di architettura e sviluppo;
  - schermate o esempi con servizi reali (Ollama, Docling, provider di generazione, riscrittura o reranker);
  - nuove dipendenze.
- **Anti-goals:** la fase 5 fallisce, anche se i documenti esistono, se cambia il comportamento dell'applicazione.

## Current behaviour and impact
- Today:
  - nessun `README` e nessun `LICENSE` nella radice del repository, che è pubblico su GitHub;
  - `docs/system/fondazioni.md`, `ingestion.md` e `chat.md` (in inglese, circa 1600 righe) descrivono il comportamento
    attuale per chi sviluppa; `docs/specs/` ha le quattro spec implementate, `docs/adr/` le ADR 001-004;
  - `.env.example` ha 49 variabili;
  - il workflow `.github/workflows/verify.yml` esegue `scripts/verify.py` su ogni PR e su `main`;
  - `docs/bugs/` è vuota.
- Changes: solo documentazione, la licenza, i test di allineamento e lo strumento facoltativo delle schermate.
- Must not change: il comportamento delle fasi 1-4, protetto dall'intera suite di `scripts/verify.py` (`backend/tests`,
  `frontend/src`, gli e2e in `backend/tests/e2e`).

## Use cases
- UC-001 Conoscere il progetto (visitatore): apre il repository su GitHub, legge il README nella sua lingua, vede lo stato
  della verifica, la licenza e le tecnologie, e trova le guide.
- UC-002 Installare (admin): segue la guida di installazione, prepara il `.env`, avvia lo stack con o senza HTTPS e
  reranker, controlla `/api/health`. Errore: un servizio esterno non risponde; la guida dice come leggerlo dalla salute.
- UC-003 Gestire (admin): segue la guida admin per utenti, KB, documenti, conversazioni e impostazioni.
- UC-004 Usare la chat (utente): segue la guida utente per accedere, scegliere la KB, chiedere, leggere fonti e
  «Ricerca: …», riprovare una risposta fallita.
- UC-005 Contribuire (sviluppatore): segue la guida di architettura e sviluppo per capire i componenti, preparare
  l'ambiente, eseguire la verifica e aprire una PR con il flusso SDD.
- UC-006 Controllo finale (owner): legge il rapporto e decide l'ordine dei bug da correggere.

## Requirements
- REQ-001 (functional): Il repository ha `README.md` in inglese e `README.it.md` in italiano.
  - Ognuno ha le sezioni: panoramica, funzioni principali, architettura in breve, avvio rapido, documentazione, licenza.
  - La prima riga dopo il titolo collega l'altra lingua.
  - L'avvio rapido porta da un clone a una pagina di login che risponde, con i comandi della guida di installazione.
  - La sezione documentazione collega le quattro guide della stessa lingua, `docs/system/`, `docs/specs/` e `docs/adr/`.
- REQ-002 (functional): I due README hanno gli stessi badge, nello stesso ordine e ciascuno con un link, in quattro gruppi.
  - Stato: esito del workflow `verify` su `main` (badge di GitHub Actions), licenza, ultimo commit, issue aperte, PR
    aperte.
  - Tecnologie: Python 3.12, FastAPI, React 19, TypeScript, PostgreSQL (ParadeDB), Redis, Docker Compose, Nginx.
  - Repository: linguaggio principale, dimensione del codice, stelle, fork, contributori.
  - Lingue e metodo: «IT | EN» e «SDD playbook».
  - I badge dinamici puntano a `jumping2000/advancedRAG`; quelli statici sono immagini di `img.shields.io`.
- REQ-003 (functional): Le guide sono `user.md`, `admin.md`, `installation.md` e `architecture.md`, in `docs/guide/en/`
  e in `docs/guide/it/`, con gli stessi nomi di file nelle due lingue. La prima riga dopo il titolo collega la stessa
  guida nell'altra lingua; ogni guida collega le pagine di `docs/system/` che ne descrivono i dettagli tecnici.
- REQ-004 (functional): La guida utente spiega l'accesso e l'uscita, la scelta della lingua, l'elenco delle KB
  assegnate, la chat (domanda, risposta in streaming, fonti numerate con estratto, «Ricerca: …», «Riprova»), le
  conversazioni (nuova, rinomina, elimina) e i messaggi d'errore che l'utente può vedere.
- REQ-005 (functional): La guida admin spiega:
  - gli utenti: creazione, ruoli, disattivazione e riattivazione, reimpostazione della password;
  - le KB: creazione, modifica, assegnazione agli utenti, eliminazione;
  - i documenti: formati e limite di dimensione, stati dell'ingestion, «Riprova», eliminazione;
  - la pagina «Conversazioni», in sola lettura;
  - la pagina «Impostazioni», con i due interruttori e «Non configurato».
- REQ-006 (functional): La guida di installazione e operatività spiega:
  - i prerequisiti, Ollama e Docling come servizi esterni, e il primo avvio;
  - ogni variabile di `.env.example`, con il significato, il valore predefinito e quando è obbligatoria;
  - l'avvio con Docker Compose, l'HTTPS con `compose.tls.yaml`, il reranker su CPU (profilo `rerank`) o GPU NVIDIA
    (`compose.rerank-gpu.yaml`);
  - il controllo `/api/health` e il significato di ogni suo stato;
  - i volumi con i dati e l'aggiornamento a una nuova versione del codice.
- REQ-007 (functional): La guida di architettura e sviluppo ha:
  - un diagramma Mermaid dei componenti, con i servizi esterni;
  - il percorso di un documento (upload → worker → chunk e vettori) e di una domanda (riscrittura → ricerca ibrida →
    reranker → generazione);
  - l'isolamento per KB;
  - la preparazione dell'ambiente, i comandi di verifica e di test;
  - il flusso SDD (spec, piano, task, PR, revisione) e le regole per contribuire;
  - i link a `docs/system/`, `docs/specs/`, `docs/adr/`, `AGENTS.md` e `docs/constitution.md`.
- REQ-008 (functional): Le guide utente e admin mostrano le schermate dell'interfaccia nella loro lingua.
  - Le schermate sono almeno 8 per lingua: login, elenco delle KB, chat con fonti e «Ricerca: …», «Utenti»,
    «Knowledge Base», «Documenti», «Conversazioni», «Impostazioni».
  - Stanno in `docs/guide/images/en/` e `docs/guide/images/it/`, con gli stessi nomi di file nelle due cartelle.
  - Sono catturate con Playwright dallo stack Compose di test, con i servizi finti di `compose.fakes.yaml` e dati inventati.
  - Un comando facoltativo, escluso da `scripts/verify.py` e dalla CI, le ricattura tutte; la guida di architettura e
    sviluppo lo descrive.
- REQ-009 (functional): Il file `LICENSE` nella radice contiene il testo della licenza Apache-2.0 senza modifiche, e
  GitHub la riconosce come `Apache-2.0`.
- REQ-010 (functional): Un test automatico fallisce se una variabile definita in `.env.example` non compare come
  `` `NOME` `` in entrambe le guide di installazione, o se una guida cita come variabile del `.env` un nome che non è in
  `.env.example`.
- REQ-011 (functional): Un test automatico fallisce se:
  - una guida o un'immagine esiste in una sola lingua;
  - le due versioni di una guida, o i due README, hanno un numero diverso di titoli `##` e `###`;
  - un'immagine citata da una guida non esiste.
- REQ-012 (functional): `python3 scripts/docs_check.py README.md README.it.md docs` passa: link relativi validi e blocchi
  Mermaid validi in README, guide e rapporto.
- REQ-013 (functional): Il controllo finale produce `docs/audit/2026-11-20-controllo-finale.md`, con:
  - l'esito della verifica completa e di `trace_check.py` su ognuna delle quattro spec;
  - il confronto tra le pagine di `docs/system/` e il codice;
  - la revisione delle aree sensibili di `AGENTS.md`;
  - l'elenco dei limiti noti accettati;
  - un elenco dei difetti trovati, ognuno con il link al suo file in `docs/bugs/`, scritto con il modello di
    `sdd-bugfix`, e la sua gravità.
  Un controllo senza difetti lo dice esplicitamente.
- REQ-101 (non-functional): Le PR della fase 5 non modificano file in `backend/app/`, `frontend/src/` (test compresi),
  `backend/migrations/`, `nginx/`, `compose*.yaml` (salvo `backend/tests/e2e/compose.fakes.yaml` se serve alle
  schermate), `.github/workflows/` e `.env.example`.
- REQ-102 (non-functional): Documenti e schermate non contengono segreti, password reali, dati personali né indirizzi
  interni.
  - Gli esempi usano i valori di `.env.example` o segnaposto evidenti; gli utenti e i documenti delle schermate sono
    inventati.
  - `scripts/secrets_check.py` passa.
- REQ-103 (non-functional): Ogni comando delle guide e del README è stato eseguito durante la fase. La PR elenca ogni
  blocco di comandi con il suo esito, oppure il motivo per cui non è stato eseguito: per esempio la GPU NVIDIA, che non è
  disponibile nell'ambiente di sviluppo.

## Acceptance criteria
- AC-001 [REQ-001]: Given il repository, when si leggono `README.md` e `README.it.md`, then ognuno ha le sei sezioni di
  REQ-001, la prima riga dopo il titolo collega l'altro file, e la sezione documentazione collega le quattro guide della
  sua lingua.
- AC-002 [REQ-001]: Given un clone pulito e un `.env` copiato da `.env.example` con i servizi finti, when si eseguono i
  comandi dell'avvio rapido, then la pagina di login risponde 200 sulla porta indicata.
- AC-003 [REQ-002]: Given i due README, when si estraggono i badge, then sono gli stessi 20 nello stesso ordine, ciascuno
  con un link, il badge del workflow punta a `actions/workflows/verify.yml/badge.svg` con `branch=main`, e i badge
  dinamici puntano a `jumping2000/advancedRAG`.
- AC-004 [REQ-003]: Given `docs/guide/en/` e `docs/guide/it/`, when si elencano i file, then entrambe hanno `user.md`,
  `admin.md`, `installation.md` e `architecture.md`, ognuno con il link alla stessa guida nell'altra lingua nella prima
  riga dopo il titolo.
- AC-005 [REQ-004]: Given la guida utente in una lingua, when la si legge, then copre accesso, lingua, KB, chat con fonti,
  «Ricerca: …» e «Riprova», le conversazioni e i messaggi d'errore, con i testi dell'interfaccia in quella lingua.
- AC-006 [REQ-005]: Given la guida admin, when la si legge, then copre utenti, KB e assegnazioni, documenti e stati,
  «Conversazioni» e «Impostazioni», con i testi dell'interfaccia in quella lingua.
- AC-007 [REQ-006]: Given la guida di installazione, when la si segue, then ci sono i prerequisiti, il primo avvio, ogni
  variabile, l'HTTPS, il reranker su CPU e GPU, gli stati di `/api/health`, i volumi e l'aggiornamento.
- AC-008 [REQ-007]: Given la guida di architettura e sviluppo, when la si legge, then ha un diagramma Mermaid dei
  componenti valido per `docs_check.py`, i due percorsi, l'isolamento, i comandi di verifica, il flusso SDD e i link di
  REQ-007.
- AC-009 [REQ-008]: Given `docs/guide/images/en/` e `docs/guide/images/it/`, when si elencano i file, then ci sono almeno
  le 8 schermate di REQ-008 con gli stessi nomi, ognuna citata dalla guida utente o admin della sua lingua, e il comando
  di cattura non fa parte di `scripts/verify.py`.
- AC-010 [REQ-009]: Given il file `LICENSE`, when lo si confronta con il testo ufficiale della licenza Apache-2.0, then è
  uguale; given la pagina del repository su GitHub dopo il merge, then la licenza mostrata è «Apache-2.0».
- AC-011 [REQ-010]: Given una variabile di `.env.example` tolta da una delle due guide di installazione, when si esegue
  il test di allineamento, then fallisce e nomina la variabile e la guida; given un nome di variabile citato in una guida
  e assente da `.env.example`, then fallisce e lo nomina.
- AC-012 [REQ-011]: Given una guida presente in una sola lingua, oppure due versioni con un numero diverso di titoli
  `##`/`###`, oppure un'immagine citata e mancante, when si esegue il test di parità, then fallisce e nomina il file.
- AC-013 [REQ-012]: Given README, guide e rapporto, when si esegue `docs_check.py` su `README.md README.it.md docs`, then
  l'esito è OK.
- AC-014 [REQ-013]: Given il rapporto `docs/audit/2026-11-20-controllo-finale.md`, when lo si legge, then ha l'esito
  della verifica, `trace_check.py` per le quattro spec, il confronto tra documenti e codice, le aree sensibili, i limiti
  noti, e per ogni difetto un link a un file esistente in `docs/bugs/` con la gravità.
- AC-015 [REQ-101]: Given le PR della fase 5, when si confrontano con `main`, then nessun file elencato in REQ-101 è
  cambiato e la verifica completa è verde.
- AC-016 [REQ-102]: Given i documenti e le schermate della fase 5, when si esegue `secrets_check.py` e si guardano le
  schermate, then non ci sono segreti né dati personali o indirizzi interni.
- AC-017 [REQ-103]: Given la PR che aggiunge una guida o un README, when la si legge, then elenca ogni blocco di comandi
  con il suo esito o il motivo per cui non è stato eseguito.

## Constraints and assumptions
- I documenti sono Markdown in git, controllati da `docs_check.py`; i titoli delle guide sono nella lingua della guida.
- Le guide descrivono solo il comportamento attuale delle fasi 1-4, con i testi dell'interfaccia presi da
  `frontend/src/i18n/it.json` e `en.json`.
- I test di allineamento usano solo la libreria standard e pytest, già presenti (ADR-001).
- Le schermate usano Playwright e lo stack di test già presenti (ADR-001, fase 1); nessuna nuova dipendenza.
- [ASSUMPTION] Il file `LICENSE` contiene il solo testo ufficiale Apache-2.0, senza file `NOTICE` e senza intestazioni di
  licenza nei file sorgente.
- [ASSUMPTION] Le versioni nei badge statici sono quelle dei manifest al momento della fase (Python 3.12 da
  `backend/pyproject.toml`, React 19 da `frontend/package.json`).
- [ASSUMPTION] I badge dinamici sono serviti da `img.shields.io` e da GitHub; il repository resta pubblico, altrimenti
  non vengono mostrati.
- Confermato dall'owner (2026-11-20): README in inglese più versione italiana; guide utente, admin, installazione,
  architettura e sviluppo in due lingue; `docs/system/` solo in inglese; i quattro gruppi di badge; schermate dallo
  stack di test; licenza Apache-2.0; test delle variabili e di parità; controllo finale con rapporto e bug da correggere
  dopo; anti-goal: nessun cambio di comportamento.

## Open questions
- none

## Revisions
- none
