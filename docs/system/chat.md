# Area: chat (hybrid retrieval, streamed answers, conversations)

- Last aligned: 2026-11-18
- Linked specs: SPEC-2026-11-17-fase-3-chat

## Current behaviour
A user opens a knowledge base (KB) they are assigned to, asks a question in Italian or English and reads the answer while
the model generates it, with numbered citations `[n]` that open the excerpt of the chunk they refer to. The conversations
are saved: the author lists, reopens, renames and deletes their own; the admin chats on any KB with their own
conversations and, from the page "Conversazioni", reads the conversations of everyone read-only and deletes them. The
stack-level facts (Compose services, Nginx, health, login, users, KBs and their isolation) are in
[fondazioni.md](fondazioni.md); the chunks, vectors and indexes the chat reads are in [ingestion.md](ingestion.md). This
page is the chat as a whole. There is no new service and no new dependency (ADR-003): the generation runs inside the HTTP
request of the question, in the one uvicorn process of the `backend`, and the browser reads it with `fetch` and a
`ReadableStream` (not `EventSource`, which cannot send a body).

**A question** (`POST /api/kbs/{kb_id}/conversations` starts a conversation with its first question, `POST
/api/conversations/{id}/messages` asks in an existing one, `POST /api/conversations/{id}/messages/{message_id}/retry`
regenerates a failed last answer). The checks that answer in JSON, before any stream and with nothing saved, run in this
order: 401 `not_authenticated`; 404 `kb_not_found` (a KB the caller cannot see, or an id that is not a UUID) or 404
`conversation_not_found` (not the caller's, of a KB no longer visible, unknown, not a UUID); 403 `forbidden` (an admin
writing in the conversation of another user: the admin reads it elsewhere, never writes); 422 `validation_error` (a
question empty after trimming, longer than `CHAT_MAX_QUESTION_CHARS`, or holding a NUL or a lone surrogate); 503
`generation_not_configured` (no `GENERATION_BASE_URL`: nothing is saved, nothing is sent anywhere); 409
`answer_in_progress`. The question is saved trimmed (and trimmed is what is embedded and sent to the model); the title of a
new conversation is the first 80 characters of it, without trailing spaces. A single transaction then saves the question
(`user`, COMPLETED) and the answer (`assistant`, PENDING); the partial unique index `messages_one_pending` allows one PENDING
answer per conversation, so two simultaneous questions, or a question racing a retry, give the loser 409
`answer_in_progress`, never a database error. The retry has no body: after the conversation and the provider checks it is
404 `conversation_not_found` for a message id that is not a UUID or not in the conversation (checked first), then 409
`answer_in_progress` when another answer of the conversation is being generated (the message is known, and the loser of a
race on `messages_one_pending` gets the same), then 409 `not_retryable` unless the message is the last of the conversation, an
answer and FAILED (checked inside the `UPDATE`); it puts that answer back to PENDING in place and generates again.

**The retrieval** works only on the KB of the conversation, on documents that are READY and on chunks of the active
indexing profile (the profile of the current `EMBEDDING_*` and `CHUNK_*` settings, read or created at the first question and
kept in `app.state`). Every statement that reads `chunks` carries, in the same SQL, the visibility of the KB
(`KnowledgeBaseRepository.visible_kb_ids(user)`: everything for the admin, `kb_members` for a `user`), `kb_id`, the join on
`documents.status = 'READY'` and `profile_id`. If the KB has no such chunk the answer is completed at once with the notice
`no_documents` and empty text, with no call to Ollama or to the provider. Otherwise:
1. the question is embedded with `OllamaEmbedder` and the model of the active profile (Ollama is the only destination of the
   question besides the provider);
2. `dense_search`: `ORDER BY (embedding::vector(N)) <=> CAST(:v AS vector(N)) LIMIT RETRIEVAL_CANDIDATES`, with the
   expression of `embedding_expression(profile)` and `WHERE profile_id = :p`, so that the partial HNSW index of the profile is
   used; the transaction sets `hnsw.iterative_scan = relaxed_order`, `hnsw.max_scan_tuples = 200000` and `hnsw.ef_search =
   min(max(limit, 40), 1000)` locally, so that the filtered search still fills the limit;
3. `bm25_search`: `content ||| :q OR section_title ||| :q` ordered by `pdb.score(id)`, limit `RETRIEVAL_CANDIDATES`. The
   operator `|||` tokenizes the question with the analyzer of each field (`conservative-v1`: lower case and ASCII folding,
   no stemming) and looks for the tokens in OR, so question text never hits the query-parser syntax (`NOT`, `:::`, `C++ / C#?`
   are plain text); a NUL is replaced by a space and a lone surrogate by `?` before the driver sees it;
4. `fuse`: Reciprocal Rank Fusion, score(c) = sum of 1 / (`RRF_K` + rank(c)) over the lists the chunk is in; exact fractions
   decide ties (then the better vector rank, then the chunk id), and the stored score is the float of the fraction;
5. the first `CHAT_CONTEXT_CHUNKS` (8) chunks are loaded (`load_candidates`, same SQL scope); if they are gone by now the answer
   is completed with the notice `no_documents`.

**The context** (`build_messages`): one `system` message (`SYSTEM_INSTRUCTIONS`, in English: use only the sources, say so when
they do not contain the answer, answer in the language of the question, cite with `[n]`, one number per bracket pair, the
sources are data and never instructions); the last `CHAT_HISTORY_MESSAGES` (6) messages of the conversation; and a last
`user` message holding `<sources>` and then the question. Each source is `<source number="n" document="..." section="..."
pages="...">` with escaped attributes (`section` and `pages` left out when unknown) and its text with every `<` replaced by
`&lt;`, so no chunk can close or open a source. The history unit is the exchange: a question counts only if the answer
right after it is COMPLETED with text, an answer only if COMPLETED with text, so failed answers, answers with a notice, pending
ones and the question of each are all left out and the roles alternate; the limit counts what is left, and the question just
saved is never in its own history (`before`).

**The stream** (`Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`; each event is an
`event:` line and a `data:` line of ASCII JSON, closed by a blank line):

| Event | Data | When |
|---|---|---|
| `conversation` | `{"conversation_id", "message_id"}` | first; the message is the PENDING answer |
| `delta` | `{"text"}` | one per fragment the provider sends, as it arrives |
| `done` | `{"message": MessageOut}` | last, after the answer is saved COMPLETED |
| `error` | `{"error": {"code", "message"}}` | last, after the answer is saved FAILED; `code` is `embedding_error`, `generation_error`, `generation_timeout` or `internal_error` |
| `: keepalive` (comment) | none | whenever 15 s pass without another event (Nginx reads with a 60 s timeout) |

The steps run in a task of their own so that the keepalive covers retrieval and generation alike. The database is used in short
sessions, none open while Ollama or the provider is waited for. An answer is never left PENDING: it ends COMPLETED or FAILED, and
when the client leaves it is FAILED `interrupted` (saved in a shielded task, also when the response ends before the generator
ever started: `AnswerResponse`). A failure of Ollama is `embedding_error`; the provider unreachable, a status that is not 2xx, a
redirect (never followed), a body that is not an event stream, a `finish_reason` other than `stop` (a cut answer is never saved
as completed), a stream with no text fragment, or a stream that closes with neither `[DONE]` nor a finish reason is
`generation_error`; more than `GENERATION_TIMEOUT_SECONDS` for the whole generation is `generation_timeout`; anything not
foreseen is `internal_error` (logged by its type only). Failed answers keep no text. The text of the provider is cleaned by
`clean_citations`: a token is `[`, ASCII digits, `]`, and it stays only when it is the canonical number of a source of the
context (1 to the number of chunks, no leading zero); every other token is removed without touching the text around it (until
none is left that is not a source), and what is not a token (`[2,3]`, `[ 2 ]`, `[-2]`) is plain text. A `[n]` inside code is
treated like any other text. `complete_answer` then saves the text and all the chunks of the context as `message_sources`
(number as the model saw it, document name, section, pages, both ranks, score, and `cited`); `MessageOut.sources` holds only the
cited ones, by number.

**Sources and excerpts.** `GET /api/conversations/{cid}/messages/{mid}/sources/{n}` returns `SourceExcerpt` (`SourceOut` plus
`available` and `content`) for whoever can read the conversation (the author, or the admin). The excerpt is the content of the
chunk, read at that moment with a `LEFT JOIN` on `chunks` limited to the KB of the conversation: no row gives
`available: false` and `content: null`, shown as «Fonte non più disponibile» (the document name saved with the source stays).
`message_sources.chunk_id` has no foreign key (ADR-003), so deleting a document never writes on the sources. A message id or
a number that does not name a cited source of that conversation (or is not a UUID, or a number of 1 to 9 ASCII digits) is the
same 404 `conversation_not_found`.

**Conversations and privacy.** A conversation belongs to one user and one KB. For a `user`, every read goes through
`ConversationRepository.get_for`/`list_for`, one SQL statement that joins the author and a KB the user can still see: a
conversation of someone else, of a KB the user is no longer assigned to, unknown or not a UUID is the same 404, never 403.
- `GET /api/kbs/{kb_id}/conversations`: the caller's own conversations of the KB, `updated_at` descending, then `id`
  descending (for the admin too: the others' are on the admin page); 404 `kb_not_found`.
- `GET /api/conversations/{id}`: `ConversationDetail` (`ConversationOut` plus `messages` and `read_only`); the author, or the
  admin for any conversation, and then `read_only` is true on someone else's.
- `PATCH /api/conversations/{id}` `{"title"}`: the author only (404 for a user, 403 for the admin on another's), then 422
  `validation_error` for a title empty after trimming, longer than 200 characters or not storable; saved trimmed; a rename does
  not change `updated_at`.
- `DELETE /api/conversations/{id}` 204: the author, or the admin for any. The generation in progress is cancelled (after the
  check of who asks, waiting at most `CANCEL_WAIT_SECONDS` = 10), then the conversation goes with its messages and sources.
- `GET /api/admin/conversations?kb_id=&user_id=`: admin only (`require_admin` before the parameters: a `user` gets 403 whatever
  they send); `[AdminConversationOut]` = `ConversationOut` plus `username`, `kb_name`, `message_count` (every message row),
  `updated_at` descending; an empty parameter is no filter; a filter that matches nothing or is not a UUID gives `[]`.
- Deleting a KB deletes its conversations, messages and sources (cascade, `ON DELETE CASCADE`).
- Schemas: `ConversationOut {id, kb_id, title, created_at, updated_at}`; `MessageOut {id, role, content, status, error_code,
  notice, created_at, sources}`; `SourceOut {number, document_name, section_title, page_start, page_end}`. Logs hold ids, counts,
  durations and error codes, never a question, an answer, a title, a chunk, a file name, a vector or the key.

**Concurrency.** `ActiveAnswers` (`app.state.active_answers`, created at first use by `service.active_answers(app)`) maps a
conversation to its running answer tasks, in memory, for the one process that generates them (ADR-003). A delete cancels them,
which closes the connection to the provider, and the stream then ends with neither `done` nor `error`; the route cancels
twice, before and after deleting, and the task of an answer refuses to start when its answer is no longer PENDING
(`is_pending`), so a delete that arrives before the stream started is not missed. `complete_answer` never writes for a
conversation that is gone. Lock order: every writer that takes a conversation row takes it BEFORE any of its messages
(`complete_answer`, `restart_answer` and `start_answer` lock the row with `FOR NO KEY UPDATE`, SQLAlchemy's `with_for_update(key_share=True)`, first; the DELETE, and
the cascade of a KB, lock the conversation and then remove its messages); the opposite order made an answer saved at the moment
of a delete a deadlock. `fail_answer` and `mark_interrupted` change one message each and take no conversation row. At the start
of the backend (`app/run.py`, before uvicorn) `mark_interrupted` ends every PENDING answer as FAILED `interrupted` (REQ-017).

**The chat page** (`/kbs/:kbId`, `ChatPage`, replaces the KB page: the KB name and description stay in the header). The KB is
read first and its conversations only if it is visible; one page state per KB. On the left the own conversations («Nuova
conversazione», open, «Rinomina» with «Salva»/«Annulla», «Elimina» with «Conferma»/«Annulla»); on the right the messages
(`ConversationView`) and the question field.
- While an answer is generated (the page's own stream, or a conversation opened with a PENDING answer) the field, the send
  button and every button of the list are disabled; the question is trimmed before it is sent and shown. Consequence: the UI
  cannot delete a conversation to stop its generation.
- `api.stream(path, body, onEvent, signal?)` reads the events with a parser written for the four events and the keepalive
  comment (events cut between packets, a multibyte character cut in two, `\r\n`, unknown events and invalid JSON ignored); it
  sends `X-Requested-With: XMLHttpRequest`, and `Content-Type: application/json` only with a body.
- A conversation opened with a PENDING answer is read again every 3 s until it ends (the page shows «Risposta in corso…»).
- A stream that ends with neither `done` nor `error` triggers a `GET /conversations/{id}` and shows what the server saved
  (normally FAILED `interrupted` with «Riprova»).
- A refusal before the stream (409, 404, 422, 503, no connection) restores the previous messages and puts the question back in
  the field.
- «Riprova» appears on the last message only when it is FAILED, and a failed answer followed by a question has none.
- Errors are translated by `useErrorText()` (`chat.errors.*`, then `kbs.errors.*`, then `errors.*`). A signed-in page shows
  its load errors as plain paragraphs, without `role="alert"` (the header's language selector owns the alert); the one
  exception is the message «Knowledge base non trovata», which keeps the `role="alert"` of phase 1.
- `ConversationView({conversation: {id, messages}, readOnly, onRetry?})`: `conversation.id` is the conversation that holds the
  messages (the excerpts are read from it). Only the `[n]` that are in `message.sources` are buttons; the text is rendered as
  text (HTML and markdown are never interpreted); a PENDING message shows «Risposta in corso…»; the notice `no_documents` shows
  its translated text; a FAILED message shows the reason of its `error_code` (an unknown code shows the generic error); the
  excerpt is a `<blockquote>` toggled by the citation and by the «Mostra estratto» button of the source, and read again each
  time it opens. `readOnly` hides «Riprova» only; the excerpts still open. `read_only` of `ConversationDetail` is not read by
  the component: the caller passes `readOnly`.

**The admin page «Conversazioni»** (`/admin/conversations`, `ConversationsPage`, menu "Conversazioni"/"Conversations", admin
only: a `user` has no link and the route sends them to `/`, no session to `/login`; the backend refuses with 403 on its own).
It lists the conversations of everyone in a table with title, user, KB, date (`updated_at`, in the browser's locale) and number
of messages, in the order of the backend. The filters are two selects fed by `GET /api/kbs` and `GET /api/admin/users`; each
choice asks `GET /api/admin/conversations` again with `kb_id` and/or `user_id` (an answer for older filters is dropped).
Clicking a title reads `GET /api/conversations/{id}` and shows the messages and sources with `ConversationView` in read-only
mode: no question field, no «Riprova», the excerpts open (admin read of the sources). «Elimina» asks for a confirmation
(«Conferma»/«Annulla») and then calls `DELETE`: the row goes away and an open conversation is closed; if the conversation
is already gone the translated error appears and the list is read again. Load and delete errors are plain paragraphs, not
`role="alert"`.

### Settings (`.env`, all in `.env.example`, `compose.yaml` service `backend` only: the worker does not read them)
Provider: `GENERATION_BASE_URL` (empty: not configured; an http or https URL with a host, else the backend does not start),
`GENERATION_MODEL` (required when the URL is set), `GENERATION_API_KEY` (secret, sent as `Authorization: Bearer` when set),
`GENERATION_TIMEOUT_SECONDS` (300, above 0). Retrieval and context: `RETRIEVAL_CANDIDATES` (50, at least 1, per search),
`RRF_K` (60, at least 1), `CHAT_CONTEXT_CHUNKS` (8, at least 1), `CHAT_HISTORY_MESSAGES` (6, 0 or more),
`CHAT_MAX_QUESTION_CHARS` (4000, at least 1). `GET /api/health` shows `checks.generation` as `ok` (`GET {base}/models` answers
2xx within `HEALTH_CHECK_TIMEOUT_SECONDS`), `unreachable` or `not_configured` (no request is sent); a stack without a provider
is therefore `degraded`.

### End-to-end tests of the area
Both use the full Compose stack through Nginx, their own project name and host port, and `down -v` at the end; the stack is
`compose.yaml` plus `backend/tests/e2e/compose.fakes.yaml` and its profile `generation`: a service that serves the fake provider
of `backend/tests/fakes/services.py` with the steps of `E2E_GENERATION_SCRIPT` (a JSON list: a string is a fragment of text, a
number a pause in seconds) at `E2E_GENERATION_URL=http://generation:8080/v1`. `test_chat_ui.py` (project
`advancedrag-e2e-chatui`, port 18680, script of 90 s) proves that each fragment reaches the browser within 2 s through Nginx;
`test_conversations_ui.py` (project `advancedrag-e2e-convui`, port 18780, a fast script) proves the admin page.

## Interfaces
| Method and path | Who | Success | Errors |
|---|---|---|---|
| `POST /api/kbs/{kb_id}/conversations` `{"question"}` | session | stream (SSE), creates the conversation | 404 `kb_not_found`; 422 `validation_error`; 503 `generation_not_configured` |
| `POST /api/conversations/{id}/messages` `{"question"}` | author | stream (SSE) | 404 `conversation_not_found`; 403 `forbidden` (admin on another's); 422; 503; 409 `answer_in_progress` |
| `POST /api/conversations/{id}/messages/{mid}/retry` (no body) | author | stream (SSE) | as above, plus 409 `not_retryable` |
| `GET /api/kbs/{kb_id}/conversations` | session | 200 `[ConversationOut]`, own, newest first | 404 `kb_not_found` |
| `GET /api/conversations/{id}` | author, admin | 200 `ConversationDetail` | 404 `conversation_not_found` |
| `PATCH /api/conversations/{id}` `{"title"}` | author | 200 `ConversationOut` | 404; 403 (admin on another's); 422 |
| `DELETE /api/conversations/{id}` | author, admin | 204 | 404 `conversation_not_found` |
| `GET /api/conversations/{cid}/messages/{mid}/sources/{n}` | author, admin | 200 `SourceExcerpt` | 404 `conversation_not_found` |
| `GET /api/admin/conversations?kb_id=&user_id=` | admin | 200 `[AdminConversationOut]` | 403 `forbidden` |

Provider: `POST {GENERATION_BASE_URL}/chat/completions` with `stream: true` (`model`, `messages`), `GET {GENERATION_BASE_URL}/models`
for the health check. Ollama is called only to embed the question.

## Diagrams

### Flowchart
```mermaid
flowchart TD
    A[POST question] --> B{Session and KB visible?}
    B -- no --> B1[404 conversation_not_found / kb_not_found]
    B -- yes --> C{Admin writing on another user's conversation?}
    C -- yes --> C1[403 forbidden]
    C -- no --> D{Question 1..CHAT_MAX_QUESTION_CHARS after trim?}
    D -- no --> D1[422 validation_error, nothing saved]
    D -- yes --> E{GENERATION_BASE_URL set?}
    E -- no --> E1[503 generation_not_configured]
    E -- yes --> F[Save user message + PENDING answer]
    F -- unique index messages_one_pending --> F1[409 answer_in_progress]
    F --> G{Searchable chunks: READY, active profile?}
    G -- no --> G1[Complete with notice no_documents, no external call]
    G -- yes --> H[Embed question with Ollama]
    H -- error --> X[fail_answer embedding_error, event error]
    H --> I[dense_search + bm25_search, filtered on KB, READY, profile]
    I --> J[fuse RRF k=60, top CHAT_CONTEXT_CHUNKS]
    J --> K[build_messages: sources + last 6 completed messages]
    K --> L[Stream from provider, event delta per fragment, keepalive every 15 s]
    L -- timeout --> X2[fail_answer generation_timeout]
    L -- unreachable / non 2xx / broken stream --> X3[fail_answer generation_error]
    L -- unexpected internal failure --> X5[fail_answer internal_error]
    L -- client disconnects or conversation deleted --> X4[fail_answer interrupted or stop]
    L -- DONE --> M[clean_citations: drop n out of range]
    M --> N[complete_answer: text + sources with ranks and scores]
    N --> O[event done with MessageOut]
```

### Sequence diagram
```mermaid
sequenceDiagram
    actor B as Browser (ChatPage)
    participant N as Nginx
    participant R as chat routes
    participant S as answer_events (task)
    participant DB as PostgreSQL
    participant OL as Ollama
    participant G as Generation provider
    participant A as ActiveAnswers
    B->>N: POST /api/kbs/id/conversations {question}
    N->>R: proxy (no buffering)
    R->>DB: create conversation, save question + PENDING answer
    R-->>B: 200 text/event-stream, event conversation
    R->>S: AnswerResponse starts the task
    S->>A: register(conversation_id, task)
    S->>DB: has_searchable_chunks (short session)
    S->>OL: embed the question
    S->>DB: dense_search, bm25_search, load_candidates, history
    S->>G: POST /chat/completions stream
    loop each fragment, and a keepalive after 15 s of silence
        G-->>S: delta
        S-->>B: event delta (or : keepalive)
    end
    S->>DB: complete_answer (text, sources) in a new session
    S-->>B: event done {message}
    B->>R: GET /conversations/id/messages/mid/sources/n
    R->>DB: chunk of the KB of the conversation (LEFT JOIN)
    R-->>B: SourceExcerpt
    B->>R: DELETE /conversations/id (author or admin)
    R->>A: cancel (twice: before and after the delete)
    A->>S: cancel task, provider connection closed
    R->>DB: lock conversation row, delete messages and sources
    R-->>B: 204
```

### State diagram
```mermaid
stateDiagram-v2
    [*] --> PENDING: question saved with its answer
    PENDING --> COMPLETED: complete_answer (also with notice no_documents)
    PENDING --> FAILED: embedding_error, generation_error, generation_timeout, internal_error
    PENDING --> FAILED: client left or conversation deleted, interrupted
    PENDING --> FAILED: backend restarted, mark_interrupted
    FAILED --> PENDING: retry of the last answer
    COMPLETED --> [*]: conversation deleted
    FAILED --> [*]: conversation deleted
    PENDING --> [*]: conversation deleted
```

### Class diagram
```mermaid
classDiagram
    class ChatRoutes {
        +start_conversation(kb_id, question)
        +ask_in_conversation(conversation_id, question)
        +retry_answer(conversation_id, message_id)
        +list_conversations(kb_id)
        +read_conversation(conversation_id)
        +rename_conversation(conversation_id, title)
        +delete_conversation(conversation_id)
        +read_source(conversation_id, message_id, number)
        +list_all_conversations(kb_id, user_id)
    }
    class ConversationRepository {
        +list_for(user, kb_id)
        +get_for(user, conversation_id)
        +get_for_admin(conversation_id)
        +start_answer(conversation, question)
        +restart_answer(conversation, message_id)
        +complete_answer(message_id, content, notice, sources)
        +fail_answer(message_id, code) bool
        +history(conversation, limit, before)
        +excerpt(conversation, message_id, number)
        +list_all(kb_id, user_id)
        +mark_interrupted() int
    }
    class AnswerResponse {
        +abandon()
    }
    class answer_events {
        +answer_events(request, user, conversation, user_message, answer)
    }
    class ActiveAnswers {
        +register(conversation_id, task)
        +cancel(conversation_id) bool
    }
    class retrieval {
        +dense_search(...)
        +bm25_search(...)
        +fuse(dense, bm25, k) list~Ranked~
        +load_candidates(...) list~Candidate~
        +has_searchable_chunks(...) bool
    }
    class prompt {
        +build_messages(question, candidates, history)
        +clean_citations(text, count)
    }
    class OpenAICompatibleClient {
        +stream(messages) AsyncIterator~str~
        +check(timeout) str
    }
    class OllamaEmbedder {
        +embed(texts) list
    }
    class KnowledgeBaseRepository {
        +visible_kb_ids(user) Select
    }
    class Conversation
    class Message
    class MessageSource
    class ConversationsPage
    class ChatPage
    class ConversationView
    ChatRoutes ..> ConversationRepository
    ChatRoutes ..> AnswerResponse : streams
    AnswerResponse ..> answer_events : body of the response
    answer_events ..> ActiveAnswers : registers the task, through _with_keepalive
    answer_events ..> retrieval
    answer_events ..> prompt
    answer_events ..> OpenAICompatibleClient
    answer_events ..> OllamaEmbedder
    answer_events ..> ConversationRepository : complete_answer, fail_answer
    ChatRoutes ..> ActiveAnswers : cancel on delete
    retrieval ..> KnowledgeBaseRepository : visible_kb_ids
    ConversationRepository ..> KnowledgeBaseRepository : visible_kb_ids
    ConversationRepository ..> Conversation
    ConversationRepository ..> Message
    ConversationRepository ..> MessageSource
    ChatPage ..> ConversationView
    ConversationsPage ..> ConversationView : readOnly
    ChatPage ..> ChatRoutes : HTTP and SSE
    ConversationsPage ..> ChatRoutes : HTTP
```

### ER diagram
```mermaid
erDiagram
    users ||--o{ conversations : writes
    knowledge_bases ||--o{ conversations : "is asked in"
    conversations ||--o{ messages : holds
    messages ||--o{ message_sources : "has context of"
    chunks }o..o{ message_sources : "chunk_id, no foreign key"
    conversations {
        uuid id PK
        uuid kb_id FK "ON DELETE CASCADE"
        uuid user_id FK
        text title
        timestamptz created_at
        timestamptz updated_at
    }
    messages {
        uuid id PK
        uuid conversation_id FK "ON DELETE CASCADE"
        text role "user or assistant"
        text content
        text status "PENDING, COMPLETED or FAILED"
        text error_code
        text notice "no_documents"
        timestamptz created_at
    }
    message_sources {
        uuid message_id PK
        int number PK
        uuid chunk_id "no FK"
        text document_name
        text section_title
        int page_start
        int page_end
        int dense_rank
        int bm25_rank
        float score
        bool cited
    }
```

## Constraints and delicate points
- Isolation (anti-goal of the spec): every access to a KB's chunks, conversations, messages or sources goes through
  `KnowledgeBaseRepository.visible_kb_ids`/`ConversationRepository.get_for`/`list_for`, or a query joined with `kb_members` in the
  same SQL statement; only the exact role `admin` skips the join. The excerpt is looked up in the KB of the conversation only.
  `tests/test_conversation_isolation.py` proves it by calling the repository and the retrieval directly. A chunk, source or
  metadata of a KB the caller cannot see never leaves any API or query.
- The admin reads every conversation but writes in none of another user's (403 on a question, a retry or a rename); the admin
  page passes `readOnly` to `ConversationView` and never shows a question field.
- Vectors of different indexing profiles never mix: every vector search filters on `profile_id` and orders by the profile's own
  expression, so the partial HNSW index is used (AC-008 checks it with 60 chunks of another profile).
- Migration `0005_conversations` is applied and never edited: a change is a new migration; its `downgrade` drops the three
  tables.
- Automated tests never call the real Ollama, Docling or generation provider: they use the fakes of `backend/tests/fakes/`.
- Logs (REQ-102): ids, counts, durations, codes and the type of an exception; never a question, answer, title, chunk, file name,
  vector or the key (`tests/test_chat_logging_privacy.py`).
- The delete-before-registration window (a DELETE arriving before the answer task registered itself) is closed: the task checks
  `is_pending` first and the route cancels twice.

## Known limits
- Manual run before real use: automated tests use fakes only. Before real use run the system once by hand with the real Ollama
  and a real OpenAI-compatible provider (a KB with a few documents, a question in each language, a citation opened, a deleted
  document, a stopped provider) and read the answers.
- `ActiveAnswers` is single-process (ADR-003): more uvicorn processes or replicas of the backend need a shared mechanism (Redis)
  and a review of `mark_interrupted` at start-up, which is a new ADR. Never scale the `backend` service.
- The residual PENDING window at shutdown (REQ-017): if the process stops while it is saving the end of an answer, that answer
  can stay PENDING until the next start, when `mark_interrupted` ends it as `interrupted` (one process only).
- AC-045 (the other APIs stay served during a slow generation) only detects an event-loop block longer than
  `HEALTH_CHECK_TIMEOUT_SECONDS` + 1 s; a shorter block is not seen by the test.
- Documents indexed with an old indexing profile (another model, size or chunker setting) are not searchable: there is no
  re-indexing (spec non-scope); the admin uploads them again.
- An odd `CHAT_HISTORY_MESSAGES` is accepted (owner's decision). `ConversationRepository.history` filters the messages first
  and applies the limit to what is left, so the limit is exact, but with an odd value (for example 5) the history can start
  with an answer whose question was cut off: the provider then gets `system, assistant, user, ...`, and some
  OpenAI-compatible providers want a `user` message first. Use an even value (the default is 6).
- `[n]` inside code in an answer is treated like any other text: it is kept when it is a source and removed otherwise.
- ChatPage, accepted low findings of the T-06 review: (1) a stream that closes cleanly before the `conversation` event on a NEW
  conversation leaves the controls disabled until the page is reloaded; (2) a question can be sent during the short window in
  which a conversation is loading, and the load then overwrites the messages; (3) a `done` event without `message` would make the
  view throw (the backend always sends it).
- Admin page «Conversazioni», four low edge cases accepted as known limits (no code change): (a) when a filter hides the
  conversation that is open, its messages stay on screen without the title; (b) when opening a conversation fails with 404 the
  selection stays and the list is not read again; (c) if the admin opens conversation B while the delete of A is in flight, B is
  closed; (d) a list request already in flight when a delete succeeds can bring the deleted row back until the next load.
- `spec.md` still says `@@@` for the BM25 operator in its Constraints text; the code uses `|||` (see The retrieval above). This
  page is the source of truth: a task does not change the spec.
- No rewriting of the question, no reranker, no evaluation, no export, search or paging of the conversations, no feedback on the
  answers, no provider other than OpenAI-compatible (spec non-scope).

## Characterization tests
- none: the area was built from the spec (greenfield); its acceptance tests are listed under References.

## References
- Spec, plan and ADR: [spec.md](../specs/2026-11-17-fase-3-chat/spec.md), [plan.md](../specs/2026-11-17-fase-3-chat/plan.md),
  [ADR-003](../adr/ADR-003-chat.md); stack, login and KBs: [fondazioni.md](fondazioni.md); chunks and indexes:
  [ingestion.md](ingestion.md).
- Backend: [routes.py](../../backend/app/chat/routes.py), [service.py](../../backend/app/chat/service.py),
  [repository.py](../../backend/app/chat/repository.py), [retrieval.py](../../backend/app/chat/retrieval.py),
  [prompt.py](../../backend/app/chat/prompt.py), [generation.py](../../backend/app/chat/generation.py),
  [schemas.py](../../backend/app/chat/schemas.py), [health.py](../../backend/app/api/health.py),
  [run.py](../../backend/app/run.py).
- Data: [0005_conversations.py](../../backend/migrations/versions/0005_conversations.py).
- Frontend: [ChatPage.tsx](../../frontend/src/pages/ChatPage.tsx),
  [ConversationView.tsx](../../frontend/src/components/ConversationView.tsx),
  [ConversationsPage.tsx](../../frontend/src/pages/admin/ConversationsPage.tsx), [client.ts](../../frontend/src/api/client.ts),
  [App.tsx](../../frontend/src/app/App.tsx).
- Tests: `backend/tests/` (`test_generation_client.py`, `test_retrieval.py`, `test_rrf.py`, `test_prompt.py`, `test_chat_ask.py`,
  `test_chat_streaming.py`, `test_chat_errors.py`, `test_chat_retry.py`, `test_conversations.py`,
  `test_conversation_isolation.py`, `test_chat_sources.py`, `test_chat_logging_privacy.py`), `backend/tests/e2e/`
  (`test_chat_ui.py`, `test_conversations_ui.py`), `frontend/src/pages/ChatPage.test.tsx`,
  `frontend/src/pages/admin/ConversationsPage.test.tsx`.
