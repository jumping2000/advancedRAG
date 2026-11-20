# Guida all'installazione e all'operatività

[English](../en/installation.md)

Questa guida accompagna l'admin da un clone del repository a uno stack in funzione, e lo mantiene in funzione: i prerequisiti, i servizi esterni, ogni variabile del file `.env`, HTTPS, il reranker facoltativo, il controllo di salute, i dati da salvare, l'aggiornamento a una nuova versione e i guasti più comuni. I testi dell'interfaccia che cita sono quelli dell'interfaccia italiana. La guida utente, la guida admin e la guida di architettura sono guide separate.

I dettagli tecnici stanno nelle pagine d'area: [fondazioni](../../system/fondazioni.md) (stack, Nginx, accesso, salute, knowledge base), [ingestione](../../system/ingestion.md) (documenti, worker, Docling e Ollama) e [chat](../../system/chat.md) (ricerca, riscrittura, reranker, generazione). Sono scritte in inglese per chi sviluppa.

## Prerequisiti

### Sul server

- Docker Engine con il plugin Compose (il comando `docker compose`, Compose v2). I comandi di primo avvio, HTTPS, controllo di salute, copia di sicurezza, ripristino, aggiornamento e rimozione sono stati provati con Docker 29.9 e Compose 5.6. Non eseguiti: `ollama pull`, l'avvio del reranker su CPU e su GPU, e `git pull`; le sezioni sul reranker vengono da `compose.yaml`, ADR-004 e `.env.example` (la loro configurazione di `docker compose` è stata controllata con `config`).
- Git, per scaricare il codice. Per avviare lo stack non servono Python, `uv` né Node.js: le immagini costruiscono tutto.
- Accesso, dal server, ai registri che la build e l'avvio usano: le immagini Docker (`paradedb/paradedb`, `redis`, `nginx`, `python`, `node`, e `ghcr.io/huggingface/text-embeddings-inference` per il reranker facoltativo), npm e PyPI per la build, e Hugging Face per il modello del reranker facoltativo.
- Una porta libera sull'host per Nginx, l'unico servizio che ne pubblica una: `HTTP_PORT` (80 di default) e `HTTPS_PORT` (443 di default) se usi HTTPS.
- Il progetto non dà indicazioni di dimensionamento. Le parti pesanti sono i modelli dei servizi che fornisci tu e, se lo usi, il reranker.

### Servizi che fornisci tu

Compose avvia cinque servizi (Nginx, backend, worker, Redis, database) e, su richiesta, il reranker. I servizi qui sotto girano fuori da Compose: li fornisci tu, a indirizzi che i container possono raggiungere:

| Servizio | A cosa serve | Variabili |
|---|---|---|
| Ollama | i vettori dei documenti e delle domande: senza, nessun documento viene indicizzato e nessuna domanda cercata | `OLLAMA_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` |
| Docling (docling-serve) | leggere i file PDF, DOCX, PPTX e XLSX; i file TXT e MD si leggono direttamente | `DOCLING_BASE_URL`, `DOCLING_API_KEY` |
| Un provider di generazione | le risposte della chat: una qualsiasi API compatibile con OpenAI; senza, ogni domanda riceve un errore | `GENERATION_BASE_URL`, `GENERATION_MODEL`, `GENERATION_API_KEY` |
| Un servizio di riscrittura (facoltativo) | riscrivere una domanda di seguito come domanda autonoma | `REWRITE_BASE_URL`, `REWRITE_MODEL`, `REWRITE_API_KEY` |
| Un reranker (facoltativo) | riordinare i passaggi trovati: quello di Compose, oppure un altro servizio | `RERANK_BASE_URL`, `RERANK_API`, `RERANK_MODEL`, `RERANK_API_KEY` |

## Ollama, Docling e gli altri servizi esterni

### Ollama

Ollama fornisce gli embedding, nel profilo di indicizzazione (`EMBEDDING_MODEL`, di default `bge-m3`, con vettori di `EMBEDDING_DIMENSIONS`, di default 1024). Scarica il modello in Ollama prima del primo documento:

```bash
ollama pull bge-m3
```

Di default Ollama ascolta sull'indirizzo locale; fallo ascoltare su un indirizzo che i container raggiungono (l'impostazione `OLLAMA_HOST` di Ollama) e scrivi quell'indirizzo in `OLLAMA_BASE_URL`. Dentro un container `localhost` è il container stesso, mai l'host. Il controllo di salute chiama `GET /api/version`.

Ollama può essere anche il provider di generazione: la sua API compatibile con OpenAI sta su `/v1`, per esempio `http://ollama.example.internal:11434/v1`.

### Docling

docling-serve legge i file PDF, DOCX, PPTX e XLSX (`POST /v1/convert/file`); il controllo di salute chiama `GET /health`. Se il tuo docling-serve richiede una chiave, scrivila in `DOCLING_API_KEY`; altrimenti lasciala vuota. Due cose da sapere:

- non c'è OCR: una scansione senza testo diventa Non supportato (il motivo dice che non è stato estratto abbastanza testo);
- docling-serve ha un limite proprio per una conversione sincrona e può rispondere 504 prima di `DOCLING_TIMEOUT_SECONDS`: alzalo sul server insieme alla variabile.

### Provider di generazione, servizio di riscrittura e reranker

Il provider di generazione serve perché la chat risponda: imposta `GENERATION_BASE_URL` e `GENERATION_MODEL`. Con `GENERATION_BASE_URL` vuota il backend parte lo stesso, ogni domanda riceve l'errore «Il provider di generazione non è configurato: contatta l'amministratore.» e il controllo di salute mostra `generation` come `not_configured`.

Il servizio di riscrittura e il reranker sono facoltativi e indipendenti. Un passo gira solo quando sono vere due cose: il suo URL base è impostato in `.env` e l'admin lo ha attivato nella pagina «Impostazioni» (gli interruttori «Riscrittura della domanda» e «Reranker»). Finché l'URL base è vuoto l'interruttore è disattivato e dice «Non configurato». Il reranker di Compose è descritto più avanti.

## Primo avvio

### Scarica il codice e crea il file .env

```bash
git clone https://github.com/jumping2000/advancedRAG.git
cd advancedRAG
cp .env.example .env
```

`.env` è ignorato da git: contiene i tuoi segreti e non si committa mai.

### Imposta le variabili obbligatorie

Apri `.env` e imposta almeno queste cinque (i segnaposto qui sotto sono solo esempi: usa i tuoi valori e password robuste):

```bash
POSTGRES_PASSWORD=change-me
OLLAMA_BASE_URL=http://ollama.example.internal:11434
DOCLING_BASE_URL=http://docling.example.internal:5001
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me-at-least-12-characters
```

Compose si rifiuta di partire se `POSTGRES_PASSWORD`, `OLLAMA_BASE_URL` o `DOCLING_BASE_URL` sono vuote. `ADMIN_USERNAME` e `ADMIN_PASSWORD` creano il primo admin e servono solo al primo avvio. Perché la chat risponda, imposta anche `GENERATION_BASE_URL` e `GENERATION_MODEL`. Ogni altra variabile ha un valore predefinito; la sezione seguente le elenca tutte.

### Avvia lo stack e accedi

```bash
docker compose up --build
```

Il primo avvio costruisce le immagini del frontend e del backend, e richiede qualche minuto. Aggiungi `-d` per eseguirlo in background. All'avvio il backend applica le migrazioni del database, crea il primo admin e il profilo di indicizzazione, poi partono il worker e Nginx; `docker compose ps` mostra ogni servizio come `healthy` quando sono pronti.

Apri `http://localhost` (o `http://localhost:<HTTP_PORT>` se hai cambiato la porta). La pagina «Accedi» chiede il nome utente e la password del primo admin. L'interfaccia segue la lingua del browser, poi quella salvata nel profilo. Da qui in poi la guida admin spiega come creare utenti e knowledge base e come caricare i documenti.

Uno stack nuovo risponde `degraded` su `/api/health` finché non configuri i servizi facoltativi: vedi la sezione sul controllo di salute.

## Le variabili del file .env

Il file `.env` sta nella radice del repository ed è letto da Docker Compose. Una riga è `NOME=valore`; una variabile vuota o assente prende il valore predefinito della tabella. Dopo una modifica, esegui di nuovo `docker compose up -d` (con le stesse opzioni `-f` e `--profile` dell'avvio) e Compose ricrea i servizi le cui variabili sono cambiate.

- **Predefinito** è il valore usato quando la variabile è assente o vuota. Dove c'è scritto *vuoto*, vuoto significa che la funzione è spenta o che il valore non è impostato. Dove c'è scritto *nessuno*, non c'è un valore predefinito: Compose si rifiuta di partire se la variabile è assente o vuota (l'esempio, quando c'è, è il valore di `.env.example`).
- **Obbligatoria** dice quando lo stack non parte senza la variabile.
- Backend e worker ricevono insiemi diversi. Entrambi ricevono gli URL di database, Redis, Ollama e Docling, `UPLOAD_DIR` e le variabili `EMBEDDING_*` e `CHUNK_*`. Solo il worker riceve `DOCLING_API_KEY`, `DOCLING_TIMEOUT_SECONDS`, `DOCLING_INCLUDE_IMAGES`, `INGESTION_*`, `MIN_EXTRACTED_CHARS` e `FAILED_ORIGINAL_RETENTION_DAYS`. Le altre variabili vanno solo al backend, tranne le porte e `RERANK_COMPOSE_MODEL`, che legge Compose stesso (`MAX_UPLOAD_SIZE_MB` va anche a Nginx e `RERANK_CANDIDATES` anche al reranker).
- Un valore non valido ferma il backend all'avvio. Il messaggio nomina la variabile (un errore di campo la nomina in minuscolo, per esempio `generation_model`) e alcuni messaggi stampano i numeri sbagliati; gli URL e le chiavi non vengono mai mostrati. Leggilo con `docker compose logs backend`.
- Le chiavi e le password sono vuote in `.env.example`. Non committare mai il tuo `.env`.

### Stack e accesso

| Variabile | Significato | Predefinito | Obbligatoria |
|---|---|---|---|
| `POSTGRES_PASSWORD` | Password dell'utente PostgreSQL `rag` (database `ragdb`). Ogni carattere è ammesso; se contiene `$`, scrivi il valore tra apici singoli. Il database la imposta solo al primo avvio (vedi Risoluzione dei problemi). | *nessuno* | sì: senza, Compose non parte |
| `HTTP_PORT` | Porta dell'host di Nginx, l'unico servizio che pubblica una porta. Con `compose.tls.yaml` resta pubblicata e reindirizza a HTTPS. | `80` | no |
| `HTTPS_PORT` | Porta dell'host di Nginx per HTTPS. Si legge solo avviando con `compose.tls.yaml`; HTTPS richiede anche `cert.pem` e `key.pem` in `nginx/certs/`. | `443` | solo con `compose.tls.yaml` |
| `DATABASE_URL` | URL del database. Con Docker Compose lo fissa `compose.yaml` (`postgresql+asyncpg://rag@db:5432/ragdb`) e questo valore non viene usato; impostalo solo per avviare il backend fuori da Compose (la password viene da `POSTGRES_PASSWORD`). | *vuoto* | no: solo fuori da Compose |
| `REDIS_URL` | URL di Redis (sessioni e coda di ingestione). Con Docker Compose lo fissa `compose.yaml` (`redis://redis:6379/0`) e questo valore non viene usato; impostalo solo per avviare il backend fuori da Compose. | *vuoto* | no: solo fuori da Compose |
| `OLLAMA_BASE_URL` | URL base di Ollama, che Compose non avvia, come lo raggiungono i container (non `localhost`). Ollama fornisce i vettori dei documenti e delle domande; il controllo di salute chiama `GET /api/version`. Sostituisci l'indirizzo d'esempio. | *nessuno* (esempio: `http://ollama.example.internal:11434`) | sì: Compose rifiuta un valore vuoto |
| `DOCLING_BASE_URL` | URL base di docling-serve, che Compose non avvia, come lo raggiungono i container. Docling legge i file PDF, DOCX, PPTX e XLSX; il controllo di salute chiama `GET /health`. Sostituisci l'indirizzo d'esempio. | *nessuno* (esempio: `http://docling.example.internal:5001`) | sì: Compose rifiuta un valore vuoto |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | Secondi che `/api/health` attende per ogni controllo. | `2` | no |
| `ADMIN_USERNAME` | Nome utente del primo admin: da 3 a 64 lettere, cifre, `.`, `_` o `-`. Si legge solo all'avvio, quando non esiste un admin attivo; poi viene ignorato. | *vuoto* | sì, al primo avvio |
| `ADMIN_PASSWORD` | Password del primo admin, di almeno 12 caratteri. Si legge come `ADMIN_USERNAME`. | *vuoto* | sì, al primo avvio |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Minuti senza richieste dopo i quali una sessione scade (ogni richiesta la rinnova); maggiore di 0. | `480` | no |

### Documenti e ingestione

| Variabile | Significato | Predefinito | Obbligatoria |
|---|---|---|---|
| `UPLOAD_DIR` | Cartella dei file caricati, conservati solo finché il documento non è elaborato. `compose.yaml` la fissa a `/data/uploads` (il volume `uploads`); impostala solo per avviare il backend fuori da Compose. | `/data/uploads` | no |
| `MAX_UPLOAD_SIZE_MB` | File più grande che l'admin può caricare, in MB: un numero intero da 1 a 99999. Lo applicano sia Nginx sia il backend (Nginx ammette 1 MB in più per il modulo); un file più grande riceve 413 `file_too_large`. | `20` | no |
| `DOCLING_API_KEY` | Chiave inviata come `X-Api-Key` quando docling-serve la richiede; altrimenti lasciala vuota. Una chiave sbagliata o mancante manda subito il documento in errore, senza ripetere la chiamata. La legge il worker. | *vuoto* | solo se Docling la richiede |
| `DOCLING_TIMEOUT_SECONDS` | Secondi che una conversione attende Docling; maggiore di 0. docling-serve ha un limite proprio per una conversione sincrona (`DOCLING_SERVE_MAX_SYNC_WAIT` sul server, circa 120 s di default in alcune versioni) e può rispondere 504 prima di questo timeout: alzalo sul server insieme a questo valore. La legge il worker. | `600` | no |
| `DOCLING_INCLUDE_IMAGES` | `true` chiede a Docling di estrarre le immagini dei documenti; il testo non ne ha bisogno. L'interfaccia non ha un interruttore per questa opzione. La legge il worker. | `false` | no |
| `INGESTION_MAX_ATTEMPTS` | Tentativi in tutto (almeno 1) per un errore temporaneo di Docling o di Ollama (connessione rifiutata, timeout, 5xx, 429). Tienilo piccolo: un job viene fermato dopo 3600 s e ogni tentativo può durare fino a `DOCLING_TIMEOUT_SECONDS`. Lo legge il worker. | `3` | no |
| `INGESTION_RETRY_BACKOFF_SECONDS` | Secondi di attesa dopo il primo tentativo fallito (0 o più), raddoppiati a ogni tentativo successivo (10, 20, 40 s e così via). Li legge il worker. | `10` | no |
| `MIN_EXTRACTED_CHARS` | Un documento con meno caratteri non bianchi di questo valore dopo la lettura risulta Non supportato; non c'è OCR. Lo legge il worker. | `50` | no |
| `FAILED_ORIGINAL_RETENTION_DAYS` | Giorni (da 1 a 3650) per cui l'originale di un documento Fallito resta dopo l'errore, così l'admin può riprovare; poi il worker lo cancella (controlla ogni ora) e il documento resta Fallito. Lo legge il worker. | `7` | no |

### Profilo di indicizzazione

Un profilo di indicizzazione è il modello di embedding, la dimensione dei suoi vettori e le dimensioni dei chunk. I vettori di profili diversi non si mescolano mai: la chat cerca solo nel profilo corrente. Quando cambi una di queste variabili, i documenti già indicizzati mantengono il loro profilo e non vengono più trovati; non c'è reindicizzazione, quindi caricali di nuovo.

| Variabile | Significato | Predefinito | Obbligatoria |
|---|---|---|---|
| `EMBEDDING_MODEL` | Modello di Ollama che produce i vettori; deve esistere nel tuo Ollama. Un altro modello avvia un nuovo profilo di indicizzazione: i documenti già indicizzati mantengono il loro e la chat non li trova più, quindi vanno caricati di nuovo (non c'è reindicizzazione). | `bge-m3` | no |
| `EMBEDDING_DIMENSIONS` | Dimensione dei vettori, da 1 a 2000. Deve essere quella che restituisce il modello, altrimenti i documenti falliscono con `embedding_dimension_mismatch`. Un'altra dimensione avvia un nuovo profilo di indicizzazione. | `1024` | no |
| `EMBEDDING_BATCH_SIZE` | Testi inviati a Ollama in ogni richiesta (almeno 1). | `16` | no |
| `EMBEDDING_TIMEOUT_SECONDS` | Secondi che ogni richiesta di embedding attende; maggiore di 0. | `120` | no |
| `CHUNK_TARGET_WORDS` | Dimensione obiettivo di un chunk, in parole (almeno 1). Le tre dimensioni devono rispettare `CHUNK_MIN_WORDS` <= `CHUNK_TARGET_WORDS` <= `CHUNK_MAX_WORDS`, altrimenti il backend non parte. Cambiare un'impostazione dei chunk avvia un nuovo profilo di indicizzazione. | `300` | no |
| `CHUNK_MIN_WORDS` | Dimensione minima di un chunk, in parole (0 o più). Vedi `CHUNK_TARGET_WORDS`. | `50` | no |
| `CHUNK_MAX_WORDS` | Dimensione massima di un chunk, in parole. Vedi `CHUNK_TARGET_WORDS`. | `450` | no |
| `CHUNK_OVERLAP_PERCENT` | Sovrapposizione tra chunk consecutivi di una sezione, in percentuale di `CHUNK_TARGET_WORDS` (da 0 a 99). | `15` | no |

### Chat

| Variabile | Significato | Predefinito | Obbligatoria |
|---|---|---|---|
| `GENERATION_BASE_URL` | Base dell'API che scrive le risposte della chat: una qualsiasi API compatibile con OpenAI (OpenAI, Ollama con `/v1`, altri servizi), un URL `http` o `https` con un host e senza il percorso finale, per esempio `https://api.openai.com/v1`. Vuoto: il backend parte, ogni domanda riceve 503 e il controllo di salute mostra `generation` come `not_configured`. Un altro valore ferma il backend. | *vuoto* | no, ma la chat risponde solo se è impostata |
| `GENERATION_MODEL` | Nome del modello del provider. | *vuoto* | sì, se `GENERATION_BASE_URL` è impostata: senza, il backend non parte |
| `GENERATION_API_KEY` | Inviata come `Authorization: Bearer` quando è impostata, anche dal controllo di salute; lasciala vuota per un provider senza chiave. | *vuoto* | no |
| `GENERATION_TIMEOUT_SECONDS` | Secondi dopo i quali una generazione viene fermata; maggiore di 0. | `300` | no |
| `LOG_LEVEL` | Livello del log del backend su stderr: `DEBUG`, `INFO`, `WARNING`, `ERROR` o `CRITICAL`, in maiuscolo o minuscolo; un altro valore ferma il backend. A `INFO` la chat scrive una riga per risposta (id, conteggi, durate, codici d'errore), mai il testo di una domanda, di una risposta, di un titolo o di un chunk. | `INFO` | no |
| `RETRIEVAL_CANDIDATES` | Numero massimo di chunk che ciascuna delle due ricerche (vettoriale e BM25) restituisce; almeno 1. | `50` | no |
| `RRF_K` | Costante k della Reciprocal Rank Fusion che unisce le due liste: punteggio = somma di 1 / (k + posizione); almeno 1. | `60` | no |
| `CHAT_CONTEXT_CHUNKS` | Chunk della lista unita che il modello riceve; almeno 1. | `8` | no |
| `CHAT_HISTORY_MESSAGES` | Ultimi messaggi completati della conversazione che il modello riceve (0 o più; le risposte fallite sono escluse). Usa un valore pari: con uno dispari la cronologia può iniziare con una risposta, che alcuni provider rifiutano. | `6` | no |
| `CHAT_MAX_QUESTION_CHARS` | Domanda più lunga, in caratteri, dopo aver tolto gli spazi alle due estremità; una più lunga viene rifiutata con 422. Almeno 1. | `4000` | no |

### Riscrittura e reranker

| Variabile | Significato | Predefinito | Obbligatoria |
|---|---|---|---|
| `REWRITE_BASE_URL` | Base di un'API compatibile con OpenAI tutta sua (un servizio o un modello diverso da quello della generazione) che riscrive una domanda di seguito come domanda autonoma, usata solo per la ricerca; un URL `http` o `https` con un host. Vuoto: non configurato, non si invia nulla e il controllo di salute mostra `rewrite` come `not_configured`. Il passo gira solo se l'admin lo attiva anche in «Impostazioni». Un altro valore ferma il backend. | *vuoto* | no |
| `REWRITE_MODEL` | Nome del modello del servizio di riscrittura. | *vuoto* | sì, se `REWRITE_BASE_URL` è impostata: senza, il backend non parte |
| `REWRITE_API_KEY` | Inviata come `Authorization: Bearer` quando è impostata; lasciala vuota per un servizio senza chiave. | *vuoto* | no |
| `REWRITE_TIMEOUT_SECONDS` | Secondi che una riscrittura attende; poi si cerca la domanda originale; un numero finito maggiore di 0, controllato anche quando l'URL base è vuoto. | `30` | no |
| `RERANK_BASE_URL` | URL base del reranker, un URL `http` o `https` con un host. Con il reranker di Compose (profilo `rerank`) scrivi `http://reranker`. Vuoto: non configurato, non si invia nulla, il controllo di salute mostra `rerank` come `not_configured` e `RERANK_API`, `RERANK_MODEL` e `RERANK_CANDIDATES` non vengono controllati. Il passo gira solo se l'admin lo attiva anche in «Impostazioni». | *vuoto* | no |
| `RERANK_API` | Formato dell'API del reranker: `tei` (text-embeddings-inference di Hugging Face, controllato con `GET /health`) o `cohere` (controllato con `GET /models`). Con `RERANK_BASE_URL` impostata, un altro valore ferma il backend. | `tei` | no |
| `RERANK_MODEL` | Nome del modello del reranker; `tei` non lo usa. | *vuoto* | sì, se `RERANK_API` è `cohere` |
| `RERANK_API_KEY` | Inviata come `Authorization: Bearer` quando è impostata; lasciala vuota per un reranker senza chiave. | *vuoto* | no |
| `RERANK_CANDIDATES` | Chunk della lista unita che vengono riordinati, da 1 a `RETRIEVAL_CANDIDATES` (controllato quando `RERANK_BASE_URL` è impostata). Il reranker di Compose accetta altrettanti testi per richiesta. | `20` | no |
| `RERANK_TIMEOUT_SECONDS` | Secondi che un reranking attende; poi si mantiene l'ordine della fusione; un numero finito maggiore di 0, sempre controllato. | `15` | no |
| `RERANK_COMPOSE_MODEL` | Modello di Hugging Face servito dal reranker di Compose (profilo `rerank`), scaricato al primo avvio nel volume `reranker-models`. Vuoto vale `BAAI/bge-reranker-v2-m3`. Lo legge solo `compose.yaml`. | `BAAI/bge-reranker-v2-m3` | no |

## Docker Compose

### I servizi

| Servizio | Cosa fa | Porta sull'host |
|---|---|---|
| `nginx` | serve l'interfaccia e inoltra `/api/` al backend | `HTTP_PORT`, e `HTTPS_PORT` con `compose.tls.yaml` |
| `backend` | l'API (FastAPI); all'avvio applica le migrazioni e crea il primo admin e il profilo di indicizzazione | nessuna |
| `worker` | l'ingestione dei documenti, un job alla volta | nessuna |
| `redis` | sessioni, coda dei job e heartbeat del worker | nessuna |
| `db` | ParadeDB: PostgreSQL 17 con pgvector e pg_search | nessuna |
| `reranker` | facoltativo, solo con il profilo `rerank` | nessuna |

Esistono un solo backend e un solo worker: non scalarli (niente `--scale`). Un secondo worker farebbe fallire il documento che il primo sta elaborando, e la chat tiene le risposte in corso nella memoria dell'unico processo del backend.

### Avvio, arresto e log

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f backend
docker compose down
```

`up --build -d` costruisce e avvia in background; `ps` mostra lo stato di ogni servizio; `logs -f backend` segue il log di un servizio (prova `worker`, `nginx`, `db`); `down` ferma e rimuove i container e conserva i volumi, quindi i dati restano. Fermando il worker, il job in corso ha fino a due minuti per finire.

### HTTPS con compose.tls.yaml

HTTPS è attivo solo quando sono vere due cose: avvii con `compose.tls.yaml` e la cartella `nginx/certs/` contiene il tuo certificato come `cert.pem` e la tua chiave privata come `key.pem`. I due file restano sul server (la cartella ignora tutto tranne i propri segnaposto), non committarli mai. Per un'installazione vera usa un certificato della tua organizzazione o di un'autorità pubblica. Per provare HTTPS puoi creare un certificato autofirmato (il browser avviserà):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout nginx/certs/key.pem -out nginx/certs/cert.pem -days 365 -subj "/CN=localhost"
```

Poi avvia con il file aggiuntivo:

```bash
docker compose -f compose.yaml -f compose.tls.yaml up --build
```

Nginx pubblica `HTTPS_PORT` (443 di default) e serve l'applicazione in HTTPS; la porta HTTP resta pubblicata e risponde 301 verso l'indirizzo `https://`, con la porta quando non è 443. Il cookie di sessione è `Secure` in HTTPS. Usa le stesse opzioni `-f` negli altri comandi (`up -d`, `ps`, `logs`, `down`).

A ogni avvio Nginx scrive nel log quale modalità ha scelto, con una riga che inizia con `entrypoint-tls:` in `docker compose logs nginx`: «cert.pem and key.pem found ... HTTPS on port ..., HTTP redirected» oppure, quando resta in HTTP, il motivo (manca un file, oppure `HTTPS_PORT` non è impostata perché non hai avviato con `compose.tls.yaml`). Dopo aver aggiunto, sostituito o tolto i file, ricrea il container perché li rilegga:

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d --force-recreate nginx
```

### Il reranker su CPU

Il reranker (text-embeddings-inference di Hugging Face) è un servizio di Compose che non parte se non lo chiedi con il profilo `rerank`:

```bash
docker compose --profile rerank up --build
```

Usa l'immagine CPU e non scarica nessuna immagine GPU. Per usarlo:

1. In `.env` imposta `RERANK_BASE_URL=http://reranker` e `RERANK_API=tei`. Compose non le imposta per te. `RERANK_COMPOSE_MODEL` è il modello di Hugging Face che serve (di default `BAAI/bge-reranker-v2-m3`) e `RERANK_CANDIDATES` il massimo di testi che accetta per richiesta.
2. Al primo avvio il servizio scarica il modello da Hugging Face nel volume `reranker-models` e diventa sano solo dopo il download (sono concessi fino a 30 minuti). Fino ad allora `rerank` è `unreachable` nel controllo di salute e la chat mantiene l'ordine della fusione. Gli avvii successivi leggono il modello dal volume.
3. Il servizio non pubblica porte sull'host: lo raggiunge solo il backend, come `http://reranker`.
4. Quando il controllo di salute mostra `rerank` come `ok`, l'admin attiva «Reranker» nella pagina «Impostazioni».

Gli altri comandi `docker compose` (`down`, `logs`) ignorano un servizio di un profilo che non nomini: aggiungi `--profile rerank`, oppure scrivi una volta `COMPOSE_PROFILES=rerank` in `.env`.

### Il reranker su GPU NVIDIA

Su un host x86_64 con una GPU NVIDIA e il NVIDIA Container Toolkit, aggiungi il file `compose.rerank-gpu.yaml`:

```bash
docker compose -f compose.yaml -f compose.rerank-gpu.yaml --profile rerank up --build
```

Il file sostituisce l'immagine del servizio `reranker` con quella per GPU (`ghcr.io/huggingface/text-embeddings-inference:1.9.4`) e riserva una GPU, quindi il reranker resta uno e c'è una sola immagine alla volta. Senza questo file l'avvio non scarica nessuna immagine GPU; il file senza il profilo non avvia nessun reranker. Le impostazioni di `.env` sono le stesse della CPU.

Quali GPU serve l'immagine predefinita (decisione ADR-004):

- l'immagine è compilata per la compute capability 8.0 e serve Ampere (A100, A30, A10, A40, RTX 30xx) e Ada Lovelace (RTX 40xx, L4, L40);
- esistono anche i tag della stessa release per una sola famiglia: `86-1.9.4` per Ampere 8.6 e `89-1.9.4` per Ada;
- Turing, Hopper e Blackwell richiedono un altro tag della stessa release: `turing-1.9.4`, `hopper-1.9.4`, `100-1.9.4` o `120-1.9.4` (vedi la tabella del README di text-embeddings-inference); cambia la riga `image:` di `compose.rerank-gpu.yaml` sul tuo server;
- le GPU più vecchie (Volta, Pascal), altri produttori e gli host ARM non sono coperti.

## Controllo di salute: /api/health

`/api/health` non richiede l'accesso. Attraverso Nginx:

```bash
curl -i http://localhost/api/health
```

Se hai cambiato `HTTP_PORT`, scrivila nell'indirizzo (`http://localhost:<HTTP_PORT>/api/health`). Con `compose.tls.yaml` quell'indirizzo HTTP semplice risponde 301: chiedi quello HTTPS (`-k` solo per un certificato autofirmato, e aggiungi `:<HTTPS_PORT>` se l'hai cambiata):

```bash
curl -k -i https://localhost/api/health
```

Su uno stack nuovo, con configurati solo i servizi obbligatori, risponde 200 e:

```json
{"status":"degraded","checks":{"database":"ok","redis":"ok","ollama":"unreachable","docling":"unreachable","worker":"ok","generation":"not_configured","rewrite":"not_configured","rerank":"not_configured"}}
```

(qui gli indirizzi d'esempio di `.env.example` per Ollama e Docling non esistono, quindi risultano `unreachable`).

### I controlli

| Controllo | Cosa fa il backend | Quando è `not_configured` |
|---|---|---|
| `database` | esegue `select 1` | mai |
| `redis` | invia `PING` | mai |
| `ollama` | `GET {OLLAMA_BASE_URL}/api/version` | mai |
| `docling` | `GET {DOCLING_BASE_URL}/health` | mai |
| `worker` | cerca in Redis la chiave di heartbeat di un worker | mai |
| `generation` | `GET {GENERATION_BASE_URL}/models` | `GENERATION_BASE_URL` è vuota |
| `rewrite` | `GET {REWRITE_BASE_URL}/models` | `REWRITE_BASE_URL` è vuota |
| `rerank` | `GET {RERANK_BASE_URL}/health` con `tei`, `GET {RERANK_BASE_URL}/models` con `cohere` | `RERANK_BASE_URL` è vuota |

Ogni controllo attende al massimo `HEALTH_CHECK_TIMEOUT_SECONDS`. Le chiavi dei servizi vengono inviate come `Authorization: Bearer`, solo al proprio servizio.

### Gli stati e come leggerli

| Stato | Dove | Significato |
|---|---|---|
| `ok` | ogni controllo, e `status` | il controllo ha risposto entro il timeout (2xx per i servizi); `status` è `ok` solo quando ogni controllo è `ok` |
| `degraded` | solo `status` | almeno un controllo non è `ok`: `unreachable` o `not_configured`. Uno stack senza provider di generazione, servizio di riscrittura o reranker è `degraded` per costruzione |
| `unreachable` | ogni controllo | nessuna risposta entro il timeout, un errore di connessione o una risposta che non è 2xx; l'errore in sé non viene mostrato |
| `not_configured` | solo `generation`, `rewrite` e `rerank` | l'URL base è vuoto: il servizio non è usato e non gli si invia nulla |

Il codice HTTP dipende solo da database e Redis: è 503 quando uno dei due è `unreachable`, ed è 200 in ogni altro caso, `degraded` compreso. Gli altri controlli non sono bloccanti: Ollama, Docling, worker, provider di generazione, servizio di riscrittura o reranker `unreachable` non fermano mai il backend né cambiano il codice; rompono solo la funzione che li usa (i documenti restano In coda o finiscono Falliti, le domande ricevono un errore, un passo viene saltato). Il controllo di salute di Compose del backend chiama questa rotta e fallisce con 503, quindi un guasto di database o Redis rende il backend `unhealthy`.

## Dati e volumi

### I volumi

Compose antepone ai nomi il nome del progetto, di default `advancedrag`: `docker volume ls` mostra `advancedrag_pgdata` e così via.

| Volume | Dove è montato | Cosa contiene | Va salvato? |
|---|---|---|---|
| `pgdata` | `/var/lib/postgresql/data` di `db` | tutto: utenti, knowledge base, documenti, chunk e vettori, conversazioni, gli interruttori | sì |
| `uploads` | `/data/uploads` di `backend` e `worker` | i file originali, solo mentre un documento è in elaborazione e, per un documento Fallito, fino a `FAILED_ORIGINAL_RETENTION_DAYS` giorni dopo l'errore | no: perderlo fa perdere solo il «Riprova» dei documenti Falliti |
| `reranker-models` | `/data` di `reranker` | il modello scaricato del reranker | no: viene scaricato di nuovo |

### Copia di sicurezza e ripristino

I dati che contano stanno nel database. Crea un dump dentro il container e copialo fuori (questi comandi funzionano allo stesso modo in ogni shell):

```bash
docker compose exec db pg_dump -U rag -d ragdb -Fc -f /tmp/ragdb.dump
docker compose cp db:/tmp/ragdb.dump ./ragdb.dump
```

Il dump contiene gli hash delle password degli utenti, il testo dei chunk e le conversazioni: conservalo protetto quanto il server. Per ripristinarlo su una nuova installazione, avvia solo il database, carica il dump, poi avvia lo stack con la stessa versione dell'immagine di `db`:

```bash
docker compose up -d --wait db
docker compose cp ./ragdb.dump db:/tmp/ragdb.dump
docker compose exec db pg_restore -U rag -d ragdb --clean --if-exists --no-owner /tmp/ragdb.dump
docker compose up --build -d
```

Il database ripristinato ha già un admin, quindi `ADMIN_USERNAME` e `ADMIN_PASSWORD` non servono. Per ripristinare su un'installazione in funzione, ferma prima i servizi che usano il database: `docker compose stop backend worker`.

### Aggiornamento a una nuova versione

1. Fai una copia di sicurezza, come sopra.
2. Scarica il nuovo codice: `git pull`, oppure `git checkout` del commit che scegli.
3. Confronta il tuo `.env` con il nuovo `.env.example`: una variabile nuova è nella tabella di questa guida, e una che lasci fuori prende il valore predefinito.
4. Ricostruisci e ricrea ciò che è cambiato, con le stesse opzioni `-f` e `--profile` del tuo avvio:

   ```bash
   docker compose up --build -d
   ```

5. Controlla `docker compose ps` e `/api/health`.

Il backend applica le nuove migrazioni del database al proprio avvio, e il worker parte dopo che il backend è sano. Un documento in elaborazione quando il worker si ferma ha fino a due minuti per finire; uno più lungo finisce Fallito (motivo: errore imprevisto) quando il worker riparte e si può riprovare con «Riprova». Una risposta che si stava scrivendo finisce come fallita e si può riprovare dalla chat. Cambiare tra due versioni il modello di embedding, la dimensione dei vettori o una dimensione dei chunk avvia un nuovo profilo di indicizzazione (vedi le variabili).

### Rimuovere tutto

```bash
docker compose down -v
```

`-v` cancella anche i volumi dei servizi che il comando include: il database e gli upload. Senza `--profile rerank` il comando lascia stare il container del reranker e il suo volume `reranker-models` (verificato); aggiungi `--profile rerank` per rimuovere anche quelli. Tutti i dati del database vanno persi: fai prima una copia di sicurezza.

## Risoluzione dei problemi

| Cosa vedi | Perché | Cosa fare |
|---|---|---|
| `docker compose up` si ferma subito con "required variable ... is missing a value" | `POSTGRES_PASSWORD`, `OLLAMA_BASE_URL` o `DOCLING_BASE_URL` è vuota o manca, oppure non c'è il file `.env` | crea `.env` da `.env.example` e imposta le tre variabili |
| il backend esce all'avvio; il log finisce con "Startup stopped: ADMIN_USERNAME is not set ..." | non esiste un admin attivo e `ADMIN_USERNAME` o `ADMIN_PASSWORD` manca, non è valida (il nome ha da 3 a 64 lettere, cifre, `.`, `_`, `-`; la password almeno 12 caratteri) oppure nomina un account esistente che non è un admin attivo | imposta valori validi ed esegui di nuovo `docker compose up -d` |
| il backend esce con un traceback che finisce con "Value error, ..." e nomina una variabile | un valore di `.env` non è valido, per esempio `GENERATION_MODEL must be set when GENERATION_BASE_URL is set` | correggi quella variabile (vedi le tabelle sopra) ed esegui `docker compose up -d` |
| il backend esce con "password authentication failed for user" | `POSTGRES_PASSWORD` è stata cambiata dopo il primo avvio: il database imposta la password solo quando crea il volume | rimetti il vecchio valore, oppure cambiala nel database: `docker compose exec db psql -U rag -d ragdb -c "ALTER USER rag PASSWORD '<nuovo valore>'"`, poi `docker compose up -d --force-recreate backend worker` |
| il container `nginx` esce con "MAX_UPLOAD_SIZE_MB must be a whole number from 1 to 99999" | la variabile non è un numero intero in quell'intervallo | correggila |
| "port is already allocated" | un altro programma usa `HTTP_PORT` o `HTTPS_PORT` | imposta un'altra porta in `.env` |
| l'indirizzo è servito in HTTP anche se vuoi HTTPS | Nginx è rimasto in HTTP: guarda la riga `entrypoint-tls:` di `docker compose logs nginx` | avvia con `compose.tls.yaml`, metti sia `cert.pem` sia `key.pem` in `nginx/certs/`, poi ricrea `nginx` |
| `/api/health` risponde 503 | il database o Redis è `unreachable` | `docker compose ps`, poi `docker compose logs db` o `docker compose logs redis` |
| `ollama` o `docling` è `unreachable` | indirizzo sbagliato, servizio spento, indirizzo che i container non raggiungono (non `localhost`), oppure nessuna risposta 2xx entro `HEALTH_CHECK_TIMEOUT_SECONDS` | prova l'indirizzo da dentro: `docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://ollama.example.internal:11434/api/version', timeout=5).status)"` con il tuo indirizzo; alza `HEALTH_CHECK_TIMEOUT_SECONDS` se il servizio è solo lento |
| `worker` è `unreachable` e i documenti restano «In coda» | il worker non è in esecuzione o non è sano | `docker compose ps worker`, `docker compose logs worker`, `docker compose up -d worker` |
| un documento è «Fallito» con un motivo su Docling | Docling non è raggiungibile, rifiuta la chiave, oppure risponde con un errore o 504 | controlla `DOCLING_BASE_URL`, `DOCLING_API_KEY` e il log di Docling; per un 504 alza `DOCLING_SERVE_MAX_SYNC_WAIT` sul server docling-serve e, insieme, `DOCLING_TIMEOUT_SECONDS` (l'attesa del worker); poi premi «Riprova» (l'originale resta per `FAILED_ORIGINAL_RETENTION_DAYS` giorni) |
| un documento è «Fallito» con un motivo su Ollama o sui vettori | Ollama non è raggiungibile, il modello non è stato scaricato, oppure i vettori hanno una dimensione diversa da `EMBEDDING_DIMENSIONS` | controlla `OLLAMA_BASE_URL`, esegui `ollama pull` del modello, fai coincidere `EMBEDDING_DIMENSIONS` con la dimensione che il modello restituisce; poi premi «Riprova» |
| un documento è «Non supportato» | non è stato estratto abbastanza testo (per esempio una scansione: non c'è OCR) | usa un file con testo; sotto `MIN_EXTRACTED_CHARS` caratteri il documento viene rifiutato |
| un caricamento è rifiutato con «Il file supera la dimensione massima consentita.» | il file è più grande di `MAX_UPLOAD_SIZE_MB` | alza la variabile (Nginx e backend leggono lo stesso valore) ed esegui `docker compose up -d` |
| una domanda riceve «Il provider di generazione non è configurato: contatta l'amministratore.» | `GENERATION_BASE_URL` è vuota | imposta `GENERATION_BASE_URL` e `GENERATION_MODEL` |
| `rerank` resta `unreachable` dopo l'avvio con il profilo `rerank` | il primo avvio sta scaricando il modello da Hugging Face, oppure non lo raggiunge: allora il servizio non diventa mai sano | `docker compose --profile rerank logs reranker`; dai al server accesso a Hugging Face per il primo avvio; nel frattempo la chat mantiene l'ordine della fusione |
| l'interruttore «Reranker» o «Riscrittura della domanda» è disattivato e dice «Non configurato» | l'URL base di quel passo è vuoto in `.env` | impostalo ed esegui `docker compose up -d` |
| i documenti caricati prima di una modifica non vengono più trovati | il modello di embedding, la dimensione dei vettori o una dimensione dei chunk è cambiata: un nuovo profilo di indicizzazione | carica di nuovo i documenti (non c'è reindicizzazione) |
| `docker compose down` o `logs` ignora il reranker | il profilo non è nominato | aggiungi `--profile rerank`, oppure scrivi `COMPOSE_PROFILES=rerank` in `.env` |
| la build fallisce mentre scarica pacchetti o immagini | il server non raggiunge npm, PyPI o i registri delle immagini | dagli accesso; un proxy o una CA privata vanno nei tuoi Dockerfile locali, mai in quelli committati |

## Limiti noti

- Un solo processo del backend (ADR-003) e un solo worker (accettato per la prima versione): non scalare mai i servizi `backend` o `worker`.
- Non c'è reindicizzazione né OCR; la chat cerca solo nei documenti del profilo di indicizzazione corrente.
- Solo il primo avvio del reranker di Compose richiede Hugging Face; un reranker esterno deve accettare almeno `RERANK_CANDIDATES` testi per richiesta, altrimenti si mantiene l'ordine della fusione.
- I test automatici usano servizi finti e non chiamano mai servizi reali. Prima di usare il sistema con il tuo Ollama, il tuo Docling e il tuo provider di generazione, esegui una volta i test dal vivo facoltativi della [pagina d'area dell'ingestione](../../system/ingestion.md) e prova una KB con qualche documento e qualche domanda, come dice la [pagina d'area della chat](../../system/chat.md).
- Rinnovare il certificato TLS spetta a te; ricrea `nginx` dopo aver sostituito i file.
