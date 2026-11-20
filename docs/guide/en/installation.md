# Installation and operations guide

[Italiano](../it/installation.md)

This guide takes an admin from a clone of the repository to a running stack, and keeps it running: the prerequisites, the external services, every variable of the `.env` file, HTTPS, the optional reranker, the health check, the data to back up, the update to a new version and the common failures. The texts of the interface that it names are those of the English interface. The user guide, the admin guide and the architecture guide are separate guides.

The technical details are in the area pages: [foundations](../../system/fondazioni.md) (stack, Nginx, login, health, knowledge bases), [ingestion](../../system/ingestion.md) (documents, worker, Docling and Ollama) and [chat](../../system/chat.md) (retrieval, rewriting, reranker, generation). They are written in English for developers.

## Prerequisites

### On the server

- Docker Engine with the Compose plugin (the `docker compose` command, Compose v2). The commands of the first start, HTTPS, the health check, back up, restore, update and removal were tried with Docker 29.9 and Compose 5.6. Not run: `ollama pull`, the start of the reranker on CPU and on GPU, and `git pull`; the sections on the reranker come from `compose.yaml`, ADR-004 and `.env.example` (their `docker compose` configuration was checked with `config`).
- Git, to get the code. You do not need Python, `uv` or Node.js to run the stack: the images build everything.
- Access, from the server, to the registries that the build and the start use: the Docker images (`paradedb/paradedb`, `redis`, `nginx`, `python`, `node`, and `ghcr.io/huggingface/text-embeddings-inference` for the optional reranker), npm and PyPI for the build, and Hugging Face for the model of the optional reranker.
- A free host port for Nginx, the only service that publishes one: `HTTP_PORT` (80 by default), and `HTTPS_PORT` (443 by default) when you use HTTPS.
- The project gives no sizing figures. The heavy parts are the models of the services you provide and, if you use it, the reranker.

### Services you provide

Compose starts five services (Nginx, backend, worker, Redis, database) and, on request, the reranker. The services below run outside Compose and you provide them, at addresses that the containers can reach:

| Service | Needed for | Variables |
|---|---|---|
| Ollama | the vectors of the documents and of the questions: without it no document is indexed and no question is searched | `OLLAMA_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` |
| Docling (docling-serve) | reading PDF, DOCX, PPTX and XLSX files; TXT and MD files are read directly | `DOCLING_BASE_URL`, `DOCLING_API_KEY` |
| A generation provider | the answers of the chat: any OpenAI-compatible API; without it every question gets an error | `GENERATION_BASE_URL`, `GENERATION_MODEL`, `GENERATION_API_KEY` |
| A rewriting service (optional) | rewriting a follow-up question as a standalone one | `REWRITE_BASE_URL`, `REWRITE_MODEL`, `REWRITE_API_KEY` |
| A reranker (optional) | reordering the retrieved passages: the one of Compose, or another service | `RERANK_BASE_URL`, `RERANK_API`, `RERANK_MODEL`, `RERANK_API_KEY` |

## Ollama, Docling and the other external services

### Ollama

Ollama gives the embeddings, in the indexing profile (`EMBEDDING_MODEL`, `bge-m3` by default, with vectors of `EMBEDDING_DIMENSIONS`, 1024 by default). Pull the model in Ollama before the first document:

```bash
ollama pull bge-m3
```

Ollama listens on the local address by default; make it listen on an address that the containers can reach (the `OLLAMA_HOST` setting of Ollama) and write that address in `OLLAMA_BASE_URL`. Inside a container `localhost` is the container itself, never the host. The health check calls `GET /api/version`.

Ollama can also be the generation provider: its OpenAI-compatible API is at `/v1`, for example `http://ollama.example.internal:11434/v1`.

### Docling

docling-serve reads PDF, DOCX, PPTX and XLSX files (`POST /v1/convert/file`); the health check calls `GET /health`. If your docling-serve asks for a key, write it in `DOCLING_API_KEY`; leave it empty otherwise. Two things to know:

- there is no OCR: a scan without text becomes Unsupported (the reason says that not enough text could be extracted);
- docling-serve has its own limit for a synchronous conversion and may answer 504 before `DOCLING_TIMEOUT_SECONDS`: raise it on the server together with the variable.

### Generation provider, rewriting service and reranker

The generation provider is required for the chat to answer: set `GENERATION_BASE_URL` and `GENERATION_MODEL`. With `GENERATION_BASE_URL` empty the backend starts all the same, every question gets the error «The generation provider is not configured: contact the administrator.» and the health check shows `generation` as `not_configured`.

The rewriting service and the reranker are optional and independent. A step runs only when two things are true: its base URL is set in `.env`, and the admin has switched it on in the page «Settings» (the switches «Question rewriting» and «Reranker»). While the base URL is empty the switch is disabled and says «Not configured». The reranker of Compose is described below.

## First start

### Get the code and create the .env file

```bash
git clone https://github.com/jumping2000/advancedRAG.git
cd advancedRAG
cp .env.example .env
```

`.env` is ignored by git: it holds your secrets and is never committed.

### Set the required variables

Open `.env` and set at least these five (the placeholders below are only examples: use your own values and strong passwords):

```bash
POSTGRES_PASSWORD=change-me
OLLAMA_BASE_URL=http://ollama.example.internal:11434
DOCLING_BASE_URL=http://docling.example.internal:5001
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me-at-least-12-characters
```

Compose refuses to start when `POSTGRES_PASSWORD`, `OLLAMA_BASE_URL` or `DOCLING_BASE_URL` is empty. `ADMIN_USERNAME` and `ADMIN_PASSWORD` create the first admin and are needed at the first start only. For the chat to answer, also set `GENERATION_BASE_URL` and `GENERATION_MODEL`. Every other variable has a default; the next section lists them all.

### Start the stack and sign in

```bash
docker compose up --build
```

The first start builds the frontend and the backend images, which takes a few minutes. Add `-d` to run in the background. At start the backend applies the database migrations, creates the first admin and the indexing profile, and then the worker and Nginx start; `docker compose ps` shows every service as `healthy` when they are ready.

Open `http://localhost` (or `http://localhost:<HTTP_PORT>` if you changed the port). The page «Sign in» asks for the username and the password of the first admin. The interface follows the language of the browser, then the language saved in the profile. From here on the admin guide explains how to create users and knowledge bases and how to upload documents.

A new stack answers `degraded` on `/api/health` until you configure the optional services: see the section on the health check.

## The variables of the .env file

The `.env` file is in the root of the repository and is read by Docker Compose. A line is `NAME=value`; a variable that is empty or absent takes the default of the table. After a change, run `docker compose up -d` again (with the same `-f` and `--profile` options as the start) and Compose recreates the services whose variables changed.

- **Default** is the value used when the variable is absent or empty. Where it says *empty*, empty means that the feature is off or the value is not set. Where it says *none*, there is no default: Compose refuses to start when the variable is absent or empty (the example, when there is one, is the value of `.env.example`).
- **Required** says when the stack does not start without the variable.
- Backend and worker receive different sets. Both get the database, Redis, Ollama and Docling URLs, `UPLOAD_DIR` and the `EMBEDDING_*` and `CHUNK_*` variables. Only the worker gets `DOCLING_API_KEY`, `DOCLING_TIMEOUT_SECONDS`, `DOCLING_INCLUDE_IMAGES`, `INGESTION_*`, `MIN_EXTRACTED_CHARS` and `FAILED_ORIGINAL_RETENTION_DAYS`. The other variables go to the backend only, except the ports and `RERANK_COMPOSE_MODEL`, which Compose itself reads (`MAX_UPLOAD_SIZE_MB` also goes to Nginx and `RERANK_CANDIDATES` also to the reranker).
- A value that is not valid stops the backend at start. The message names the variable (a field error names it in lower case, for example `generation_model`) and some messages print the numbers that were wrong; the URLs and the keys are never shown. Read it with `docker compose logs backend`.
- The keys and passwords are empty in `.env.example`. Never commit your `.env`.

### Stack and access

| Variable | Meaning | Default | Required |
|---|---|---|---|
| `POSTGRES_PASSWORD` | Password of the PostgreSQL user `rag` (database `ragdb`). Any character is allowed; if it contains `$`, write the value in single quotes. The database sets it at its first start only (see Troubleshooting). | *none* | yes: Compose does not start without it |
| `HTTP_PORT` | Host port of Nginx, the only service that publishes a port. With `compose.tls.yaml` it stays published and redirects to HTTPS. | `80` | no |
| `HTTPS_PORT` | Host port of Nginx for HTTPS. Read only when you start with `compose.tls.yaml`; HTTPS also needs `cert.pem` and `key.pem` in `nginx/certs/`. | `443` | only with `compose.tls.yaml` |
| `DATABASE_URL` | URL of the database. With Docker Compose it is fixed by `compose.yaml` (`postgresql+asyncpg://rag@db:5432/ragdb`) and this value is not used; set it only to run the backend outside Compose (the password comes from `POSTGRES_PASSWORD`). | *empty* | no: only outside Compose |
| `REDIS_URL` | URL of Redis (sessions and the ingestion queue). With Docker Compose it is fixed by `compose.yaml` (`redis://redis:6379/0`) and this value is not used; set it only to run the backend outside Compose. | *empty* | no: only outside Compose |
| `OLLAMA_BASE_URL` | Base URL of Ollama, which Compose does not start, as the containers can reach it (not `localhost`). Ollama gives the vectors of the documents and of the questions; the health check calls `GET /api/version`. Replace the example address. | *none* (example: `http://ollama.example.internal:11434`) | yes: Compose refuses an empty value |
| `DOCLING_BASE_URL` | Base URL of docling-serve, which Compose does not start, as the containers can reach it. Docling reads PDF, DOCX, PPTX and XLSX files; the health check calls `GET /health`. Replace the example address. | *none* (example: `http://docling.example.internal:5001`) | yes: Compose refuses an empty value |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | Seconds that `/api/health` waits for each check. | `2` | no |
| `ADMIN_USERNAME` | Username of the first admin: 3 to 64 letters, digits, `.`, `_` or `-`. Read only at start-up, when no active admin exists; ignored afterwards. | *empty* | yes, at the first start |
| `ADMIN_PASSWORD` | Password of the first admin, at least 12 characters. Read like `ADMIN_USERNAME`. | *empty* | yes, at the first start |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Minutes without requests after which a session expires (each request renews it); above 0. | `480` | no |

### Documents and ingestion

| Variable | Meaning | Default | Required |
|---|---|---|---|
| `UPLOAD_DIR` | Folder of the uploaded files, kept only until the document is processed. `compose.yaml` fixes it to `/data/uploads` (the `uploads` volume); set it only to run the backend outside Compose. | `/data/uploads` | no |
| `MAX_UPLOAD_SIZE_MB` | Largest file the admin can upload, in MB: a whole number from 1 to 99999. Nginx and the backend both enforce it (Nginx allows 1 MB more for the form); a larger file gets 413 `file_too_large`. | `20` | no |
| `DOCLING_API_KEY` | Key sent as `X-Api-Key` when docling-serve asks for one; leave it empty otherwise. A wrong or missing key fails the document at once, without repeating the call. Read by the worker. | *empty* | only if Docling asks for it |
| `DOCLING_TIMEOUT_SECONDS` | Seconds that a conversion waits for Docling; above 0. docling-serve has its own limit for a synchronous conversion (`DOCLING_SERVE_MAX_SYNC_WAIT` on the server, about 120 s by default in some versions) and may answer 504 before this timeout: raise it on the server together with this value. Read by the worker. | `600` | no |
| `DOCLING_INCLUDE_IMAGES` | `true` asks Docling to extract the images of the documents; the text does not need them. The interface has no switch for it. Read by the worker. | `false` | no |
| `INGESTION_MAX_ATTEMPTS` | Attempts in all (at least 1) for a temporary error of Docling or Ollama (connection refused, timeout, 5xx, 429). Keep it small: a job is stopped after 3600 s and each attempt can take up to `DOCLING_TIMEOUT_SECONDS`. Read by the worker. | `3` | no |
| `INGESTION_RETRY_BACKOFF_SECONDS` | Seconds waited after the first failed attempt (0 or more), doubled at each following one (10, 20, 40 s and so on). Read by the worker. | `10` | no |
| `MIN_EXTRACTED_CHARS` | A document with fewer non-white characters than this after parsing is Unsupported; there is no OCR. Read by the worker. | `50` | no |
| `FAILED_ORIGINAL_RETENTION_DAYS` | Days (1 to 3650) that the original of a Failed document is kept after it failed, so that the admin can retry it; then the worker deletes it (it checks every hour) and the document stays Failed. Read by the worker. | `7` | no |

### Indexing profile

An indexing profile is the embedding model, the size of its vectors and the chunk sizes. The vectors of different profiles never mix: the chat searches the current profile only. When you change one of these variables the documents already indexed keep their profile and are not found any more; there is no re-indexing, so upload them again.

| Variable | Meaning | Default | Required |
|---|---|---|---|
| `EMBEDDING_MODEL` | Ollama model that gives the vectors; it must exist in your Ollama. Another model starts a new indexing profile: the documents already indexed keep theirs and are no longer found by the chat, so upload them again (there is no re-indexing). | `bge-m3` | no |
| `EMBEDDING_DIMENSIONS` | Size of the vectors, from 1 to 2000. It must be the size the model returns, or documents fail with `embedding_dimension_mismatch`. Another size starts a new indexing profile. | `1024` | no |
| `EMBEDDING_BATCH_SIZE` | Texts sent to Ollama in each request (at least 1). | `16` | no |
| `EMBEDDING_TIMEOUT_SECONDS` | Seconds that each embedding request waits; above 0. | `120` | no |
| `CHUNK_TARGET_WORDS` | Target size of a chunk, in words (at least 1). The three sizes must satisfy `CHUNK_MIN_WORDS` <= `CHUNK_TARGET_WORDS` <= `CHUNK_MAX_WORDS`, or the backend does not start. Changing a chunk setting starts a new indexing profile. | `300` | no |
| `CHUNK_MIN_WORDS` | Smallest size of a chunk, in words (0 or more). See `CHUNK_TARGET_WORDS`. | `50` | no |
| `CHUNK_MAX_WORDS` | Largest size of a chunk, in words. See `CHUNK_TARGET_WORDS`. | `450` | no |
| `CHUNK_OVERLAP_PERCENT` | Overlap between consecutive chunks of a section, as a percent of `CHUNK_TARGET_WORDS` (0 to 99). | `15` | no |

### Chat

| Variable | Meaning | Default | Required |
|---|---|---|---|
| `GENERATION_BASE_URL` | Base of the API that writes the answers of the chat: any OpenAI-compatible API (OpenAI, Ollama with `/v1`, other services), an `http` or `https` URL with a host and without the final path, for example `https://api.openai.com/v1`. Empty: the backend starts, every question answers 503 and the health check shows `generation` as `not_configured`. Another value stops the backend. | *empty* | no, but the chat answers only with it |
| `GENERATION_MODEL` | Name of the model of the provider. | *empty* | yes, when `GENERATION_BASE_URL` is set: the backend does not start without it |
| `GENERATION_API_KEY` | Sent as `Authorization: Bearer` when set, also by the health check; leave it empty for a provider without a key. | *empty* | no |
| `GENERATION_TIMEOUT_SECONDS` | Seconds after which a generation is stopped; above 0. | `300` | no |
| `LOG_LEVEL` | Level of the backend log on stderr: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case; another value stops the backend. At `INFO` the chat writes one line per answer (ids, counts, durations, error codes), never the text of a question, an answer, a title or a chunk. | `INFO` | no |
| `RETRIEVAL_CANDIDATES` | Chunks that each of the two searches (vector and BM25) returns at most; at least 1. | `50` | no |
| `RRF_K` | Constant k of the Reciprocal Rank Fusion that merges the two lists: score = sum of 1 / (k + rank); at least 1. | `60` | no |
| `CHAT_CONTEXT_CHUNKS` | Chunks of the merged list that the model receives; at least 1. | `8` | no |
| `CHAT_HISTORY_MESSAGES` | Last completed messages of the conversation that the model receives (0 or more; failed answers are left out). Use an even value: with an odd one the history can start with an answer, which some providers refuse. | `6` | no |
| `CHAT_MAX_QUESTION_CHARS` | Longest question, in characters, after the spaces at both ends are removed; a longer one is refused with 422. At least 1. | `4000` | no |

### Rewriting and reranker

| Variable | Meaning | Default | Required |
|---|---|---|---|
| `REWRITE_BASE_URL` | Base of an OpenAI-compatible API of its own (another service or model than the generation) that rewrites a follow-up question as a standalone one, used only for the search; an `http` or `https` URL with a host. Empty: not configured, nothing is sent and the health check shows `rewrite` as `not_configured`. The step runs only when the admin also switches it on in «Settings». Another value stops the backend. | *empty* | no |
| `REWRITE_MODEL` | Name of the model of the rewriting service. | *empty* | yes, when `REWRITE_BASE_URL` is set: the backend does not start without it |
| `REWRITE_API_KEY` | Sent as `Authorization: Bearer` when set; leave it empty for a service without a key. | *empty* | no |
| `REWRITE_TIMEOUT_SECONDS` | Seconds that a rewriting waits, then the original question is searched; a finite number above 0, checked also when the base URL is empty. | `30` | no |
| `RERANK_BASE_URL` | Base URL of the reranker, an `http` or `https` URL with a host. With the reranker of Compose (profile `rerank`) write `http://reranker`. Empty: not configured, nothing is sent, the health check shows `rerank` as `not_configured` and `RERANK_API`, `RERANK_MODEL` and `RERANK_CANDIDATES` are not checked. The step runs only when the admin also switches it on in «Settings». | *empty* | no |
| `RERANK_API` | Format of the reranker API: `tei` (Hugging Face text-embeddings-inference, checked with `GET /health`) or `cohere` (checked with `GET /models`). With `RERANK_BASE_URL` set, another value stops the backend. | `tei` | no |
| `RERANK_MODEL` | Name of the model of the reranker; `tei` does not use it. | *empty* | yes, when `RERANK_API` is `cohere` |
| `RERANK_API_KEY` | Sent as `Authorization: Bearer` when set; leave it empty for a reranker without a key. | *empty* | no |
| `RERANK_CANDIDATES` | Chunks of the merged list that are reordered, from 1 to `RETRIEVAL_CANDIDATES` (checked when `RERANK_BASE_URL` is set). The reranker of Compose accepts that many texts per request. | `20` | no |
| `RERANK_TIMEOUT_SECONDS` | Seconds that a reranking waits, then the order of the fusion is kept; a finite number above 0, always checked. | `15` | no |
| `RERANK_COMPOSE_MODEL` | Hugging Face model served by the reranker of Compose (profile `rerank`), downloaded at its first start into the `reranker-models` volume. Empty means `BAAI/bge-reranker-v2-m3`. Only `compose.yaml` reads it. | `BAAI/bge-reranker-v2-m3` | no |

## Docker Compose

### The services

| Service | What it does | Host port |
|---|---|---|
| `nginx` | serves the interface and sends `/api/` to the backend | `HTTP_PORT`, and `HTTPS_PORT` with `compose.tls.yaml` |
| `backend` | the API (FastAPI); at start it applies the migrations and creates the first admin and the indexing profile | none |
| `worker` | the ingestion of the documents, one job at a time | none |
| `redis` | sessions, the queue of the jobs and the heartbeat of the worker | none |
| `db` | ParadeDB: PostgreSQL 17 with pgvector and pg_search | none |
| `reranker` | optional, only with the profile `rerank` | none |

There is one backend and one worker: do not scale them (no `--scale`). A second worker would make the document that the first is processing fail, and the chat keeps its running answers in the memory of the single backend process.

### Start, stop and logs

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f backend
docker compose down
```

`up --build -d` builds and starts in the background; `ps` shows the state of every service; `logs -f backend` follows the log of one service (try `worker`, `nginx`, `db`); `down` stops and removes the containers and keeps the volumes, so the data stays. Stopping the worker gives the job in progress up to two minutes to finish.

### HTTPS with compose.tls.yaml

HTTPS is on only when two things are true: you start with `compose.tls.yaml`, and the folder `nginx/certs/` holds your certificate as `cert.pem` and your private key as `key.pem`. Both files stay on the server (the folder ignores everything but its own placeholders), never commit them. For a real installation use a certificate of your organization or of a public authority. To try HTTPS you can make a self-signed certificate (the browser will warn):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout nginx/certs/key.pem -out nginx/certs/cert.pem -days 365 -subj "/CN=localhost"
```

Then start with the override:

```bash
docker compose -f compose.yaml -f compose.tls.yaml up --build
```

Nginx publishes `HTTPS_PORT` (443 by default) and serves the application over HTTPS; the HTTP port stays published and answers 301 to the `https://` address, with the port when it is not 443. The session cookie is `Secure` over HTTPS. Use the same `-f` options in the other commands (`up -d`, `ps`, `logs`, `down`).

At every start Nginx logs which mode it chose, a line that begins with `entrypoint-tls:` in `docker compose logs nginx`: "cert.pem and key.pem found ... HTTPS on port ..., HTTP redirected" or, when it stays on HTTP, the reason (a file missing, or `HTTPS_PORT` not set because you did not start with `compose.tls.yaml`). After adding, replacing or removing the files, recreate the container so that it reads them again:

```bash
docker compose -f compose.yaml -f compose.tls.yaml up -d --force-recreate nginx
```

### The reranker on CPU

The reranker (Hugging Face text-embeddings-inference) is a service of Compose that does not start unless you ask for it with the profile `rerank`:

```bash
docker compose --profile rerank up --build
```

This uses the CPU image and pulls no GPU image. To use it:

1. In `.env` set `RERANK_BASE_URL=http://reranker` and `RERANK_API=tei`. Compose does not set them for you. `RERANK_COMPOSE_MODEL` is the Hugging Face model it serves (`BAAI/bge-reranker-v2-m3` by default) and `RERANK_CANDIDATES` the most texts it accepts per request.
2. At its first start the service downloads the model from Hugging Face into the `reranker-models` volume, and it is healthy only after the download (up to 30 minutes are allowed). Until then `rerank` is `unreachable` in the health check and the chat keeps the order of the fusion. Later starts read the model from the volume.
3. The service publishes no host port: only the backend reaches it, as `http://reranker`.
4. When the health check shows `rerank` as `ok`, the admin switches «Reranker» on in the page «Settings».

The other `docker compose` commands (`down`, `logs`) ignore a service of a profile that you do not name: add `--profile rerank` to them, or write `COMPOSE_PROFILES=rerank` in `.env` once.

### The reranker on a NVIDIA GPU

On a x86_64 host with an NVIDIA GPU and the NVIDIA Container Toolkit, add the file `compose.rerank-gpu.yaml`:

```bash
docker compose -f compose.yaml -f compose.rerank-gpu.yaml --profile rerank up --build
```

The file replaces the image of the `reranker` service with the GPU one (`ghcr.io/huggingface/text-embeddings-inference:1.9.4`) and reserves one GPU, so there is still one reranker and only one image at a time. Without this file the start pulls no GPU image; the file without the profile starts no reranker. The settings of `.env` are the same as on CPU.

Which GPUs the default image serves (decision ADR-004):

- the image is built for compute capability 8.0 and serves Ampere (A100, A30, A10, A40, RTX 30xx) and Ada Lovelace (RTX 40xx, L4, L40);
- the tags of the same release for one family also exist: `86-1.9.4` for Ampere 8.6 and `89-1.9.4` for Ada;
- Turing, Hopper and Blackwell need another tag of the same release: `turing-1.9.4`, `hopper-1.9.4`, `100-1.9.4` or `120-1.9.4` (see the table of the text-embeddings-inference README); change the `image:` line of `compose.rerank-gpu.yaml` on your server;
- older GPUs (Volta, Pascal), other makers and ARM hosts are not covered.

## Health check: /api/health

`/api/health` needs no sign-in. Through Nginx:

```bash
curl -i http://localhost/api/health
```

If you changed `HTTP_PORT`, write it in the address (`http://localhost:<HTTP_PORT>/api/health`). With `compose.tls.yaml` that plain HTTP address answers 301: ask the HTTPS one (`-k` only for a self-signed certificate, and add `:<HTTPS_PORT>` if you changed it):

```bash
curl -k -i https://localhost/api/health
```

On a new stack, with only the required services configured, it answers 200 and:

```json
{"status":"degraded","checks":{"database":"ok","redis":"ok","ollama":"unreachable","docling":"unreachable","worker":"ok","generation":"not_configured","rewrite":"not_configured","rerank":"not_configured"}}
```

(here the example addresses of `.env.example` for Ollama and Docling do not exist, so they are `unreachable`).

### The checks

| Check | What the backend does | When it is `not_configured` |
|---|---|---|
| `database` | runs `select 1` | never |
| `redis` | sends `PING` | never |
| `ollama` | `GET {OLLAMA_BASE_URL}/api/version` | never |
| `docling` | `GET {DOCLING_BASE_URL}/health` | never |
| `worker` | looks for the heartbeat key of a worker in Redis | never |
| `generation` | `GET {GENERATION_BASE_URL}/models` | `GENERATION_BASE_URL` is empty |
| `rewrite` | `GET {REWRITE_BASE_URL}/models` | `REWRITE_BASE_URL` is empty |
| `rerank` | `GET {RERANK_BASE_URL}/health` with `tei`, `GET {RERANK_BASE_URL}/models` with `cohere` | `RERANK_BASE_URL` is empty |

Each check waits at most `HEALTH_CHECK_TIMEOUT_SECONDS`. The keys of the services are sent as `Authorization: Bearer`, to their own service only.

### The states and how to read them

| State | Where | Meaning |
|---|---|---|
| `ok` | every check, and `status` | the check answered within the timeout (2xx for the services); `status` is `ok` only when every check is `ok` |
| `degraded` | `status` only | at least one check is not `ok`: `unreachable` or `not_configured`. A stack without generation provider, rewriting service or reranker is `degraded` by design |
| `unreachable` | every check | no answer within the timeout, a connection error or an answer that is not 2xx; the error itself is not shown |
| `not_configured` | `generation`, `rewrite` and `rerank` only | the base URL is empty: the service is not used and nothing is sent to it |

The HTTP code depends only on the database and Redis: it is 503 when one of the two is `unreachable`, and 200 in every other case, `degraded` included. The other checks are non-blocking: an `unreachable` Ollama, Docling, worker, generation provider, rewriting service or reranker never stops the backend or changes the code; it only breaks the feature that uses it (documents stay Queued or end Failed, questions get an error, a step is skipped). The Compose health check of the backend calls this route and fails on 503, so a database or Redis outage makes the backend `unhealthy`.

## Data and volumes

### The volumes

Compose prefixes the names with the project name, `advancedrag` by default: `docker volume ls` shows `advancedrag_pgdata` and so on.

| Volume | Where it is mounted | What it holds | Back it up? |
|---|---|---|---|
| `pgdata` | `/var/lib/postgresql/data` of `db` | everything: users, knowledge bases, documents, chunks and vectors, conversations, the switches | yes |
| `uploads` | `/data/uploads` of `backend` and `worker` | the original files, only while a document is processed and, for a Failed document, until `FAILED_ORIGINAL_RETENTION_DAYS` days after the error | no: losing it only loses the retry of the Failed documents |
| `reranker-models` | `/data` of `reranker` | the downloaded model of the reranker | no: it is downloaded again |

### Back up and restore

The data that matters is in the database. Make a dump inside the container and copy it out (these commands work the same in every shell):

```bash
docker compose exec db pg_dump -U rag -d ragdb -Fc -f /tmp/ragdb.dump
docker compose cp db:/tmp/ragdb.dump ./ragdb.dump
```

The dump holds the password hashes of the users, the text of the chunks and the conversations: store it as protected as the server. To restore it on a new installation, start only the database, load the dump, then start the stack with the same image version of `db`:

```bash
docker compose up -d --wait db
docker compose cp ./ragdb.dump db:/tmp/ragdb.dump
docker compose exec db pg_restore -U rag -d ragdb --clean --if-exists --no-owner /tmp/ragdb.dump
docker compose up --build -d
```

The restored database already has an admin, so `ADMIN_USERNAME` and `ADMIN_PASSWORD` are not needed. To restore over a running installation, stop the services that use the database first: `docker compose stop backend worker`.

### Update to a new version

1. Make a backup, as above.
2. Get the new code: `git pull`, or `git checkout` of the commit you choose.
3. Compare your `.env` with the new `.env.example`: a new variable is in the table of this guide, and one that you leave out takes its default.
4. Rebuild and recreate what changed, with the same `-f` and `--profile` options as your start:

   ```bash
   docker compose up --build -d
   ```

5. Check `docker compose ps` and `/api/health`.

The backend applies the new database migrations at its start, and the worker starts after the backend is healthy. A document being processed when the worker stops has up to two minutes to finish; a longer one ends Failed (reason: unexpected error) when the worker starts again and can be retried with «Retry». An answer that was being written ends as failed and can be retried from the chat. Changing the embedding model, the size of the vectors or a chunk size between two versions starts a new indexing profile (see the variables).

### Remove everything

```bash
docker compose down -v
```

`-v` also deletes the volumes of the services that the command includes: the database and the uploads. Without `--profile rerank` the command leaves the reranker container and its `reranker-models` volume alone (checked); add `--profile rerank` to remove them too. All the data of the database is lost: make a backup first.

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| `docker compose up` stops at once with "required variable ... is missing a value" | `POSTGRES_PASSWORD`, `OLLAMA_BASE_URL` or `DOCLING_BASE_URL` is empty or missing, or there is no `.env` | create `.env` from `.env.example` and set the three variables |
| the backend exits at start; the log ends with "Startup stopped: ADMIN_USERNAME is not set ..." | no active admin exists and `ADMIN_USERNAME` or `ADMIN_PASSWORD` is missing, not valid (the name has 3 to 64 letters, digits, `.`, `_`, `-`; the password at least 12 characters) or names an existing account that is not an active admin | set valid values and run `docker compose up -d` again |
| the backend exits with a traceback that ends with "Value error, ..." naming a variable | a value of `.env` is not valid, for example `GENERATION_MODEL must be set when GENERATION_BASE_URL is set` | correct that variable (see the tables above) and run `docker compose up -d` |
| the backend exits with "password authentication failed for user" | `POSTGRES_PASSWORD` was changed after the first start: the database sets the password only when it creates the volume | put the old value back, or change it in the database: `docker compose exec db psql -U rag -d ragdb -c "ALTER USER rag PASSWORD '<new value>'"`, then `docker compose up -d --force-recreate backend worker` |
| the `nginx` container exits with "MAX_UPLOAD_SIZE_MB must be a whole number from 1 to 99999" | the variable is not a whole number in that range | correct it |
| "port is already allocated" | another program uses `HTTP_PORT` or `HTTPS_PORT` | set another port in `.env` |
| the address is served over HTTP although you want HTTPS | Nginx stayed on HTTP: look at the `entrypoint-tls:` line of `docker compose logs nginx` | start with `compose.tls.yaml`, and put both `cert.pem` and `key.pem` in `nginx/certs/`, then recreate `nginx` |
| `/api/health` answers 503 | the database or Redis is `unreachable` | `docker compose ps`, then `docker compose logs db` or `docker compose logs redis` |
| `ollama` or `docling` is `unreachable` | wrong address, a service that is down, an address that the containers cannot reach (not `localhost`), or no 2xx answer within `HEALTH_CHECK_TIMEOUT_SECONDS` | try the address from inside: `docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://ollama.example.internal:11434/api/version', timeout=5).status)"` with your address; raise `HEALTH_CHECK_TIMEOUT_SECONDS` if the service is only slow |
| `worker` is `unreachable`, and the documents stay «Queued» | the worker is not running or not healthy | `docker compose ps worker`, `docker compose logs worker`, `docker compose up -d worker` |
| a document is «Failed» with a reason about Docling | Docling is not reachable, refuses the key, or answers an error or 504 | check `DOCLING_BASE_URL`, `DOCLING_API_KEY` and the log of Docling; for a 504 raise `DOCLING_SERVE_MAX_SYNC_WAIT` on the docling-serve server and, together with it, `DOCLING_TIMEOUT_SECONDS` (the wait of the worker); then press «Retry» (the original is kept for `FAILED_ORIGINAL_RETENTION_DAYS` days) |
| a document is «Failed» with a reason about Ollama or the vectors | Ollama is not reachable, the model is not pulled, or the vectors have a size other than `EMBEDDING_DIMENSIONS` | check `OLLAMA_BASE_URL`, run `ollama pull` of the model, make `EMBEDDING_DIMENSIONS` equal to the size the model returns; then press «Retry» |
| a document is «Unsupported» | not enough text could be extracted (for example a scan: there is no OCR) | use a file with text; fewer than `MIN_EXTRACTED_CHARS` characters are refused |
| an upload is refused with «The file exceeds the maximum allowed size.» | the file is larger than `MAX_UPLOAD_SIZE_MB` | raise the variable (Nginx and the backend read the same value) and run `docker compose up -d` |
| a question gets «The generation provider is not configured: contact the administrator.» | `GENERATION_BASE_URL` is empty | set `GENERATION_BASE_URL` and `GENERATION_MODEL` |
| `rerank` stays `unreachable` after starting with the profile `rerank` | the first start is downloading the model from Hugging Face, or cannot reach it: then the service never becomes healthy | `docker compose --profile rerank logs reranker`; give the server access to Hugging Face for the first start; meanwhile the chat keeps the order of the fusion |
| the switch «Reranker» or «Question rewriting» is disabled and says «Not configured» | the base URL of that step is empty in `.env` | set it and run `docker compose up -d` |
| the documents uploaded before a change are not found any more | the embedding model, the vector size or a chunk size changed: a new indexing profile | upload the documents again (there is no re-indexing) |
| `docker compose down` or `logs` ignores the reranker | the profile is not named | add `--profile rerank`, or write `COMPOSE_PROFILES=rerank` in `.env` |
| the build fails while downloading packages or images | the server cannot reach npm, PyPI or the image registries | give it access; a proxy or a private CA goes in your local Dockerfiles, never in the committed ones |

## Known limits

- One backend process (ADR-003) and one worker (accepted for the first version): never scale the `backend` or `worker` services.
- There is no re-indexing and no OCR; the chat searches only the documents of the current indexing profile.
- Only the first start of the reranker of Compose needs Hugging Face; an external reranker must accept at least `RERANK_CANDIDATES` texts per request, or the order of the fusion is kept.
- The automated tests use fakes and never call real services. Before using the system with your real Ollama, Docling and generation provider, run the opt-in live tests of the [ingestion area page](../../system/ingestion.md) once and try a KB with a few documents and a few questions, as the [chat area page](../../system/chat.md) says.
- Renewing the TLS certificate is up to you; recreate `nginx` after you replace the files.
