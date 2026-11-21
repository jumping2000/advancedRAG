# BUG-002: the docstring of `_embed` says that Ollama is the only destination of the question

- Status: open
- Severity / impact: low, a comment that is false since phase 4; nothing is sent where it should not be (the destinations are checked by `test_chat_steps_privacy.py`), but a reader who trusts the comment gets the privacy picture wrong.
- Affected spec or area: SPEC-2026-11-20-fase-4-rerank-rewrite (REQ-101, AC-026), `backend/app/chat/service.py`, `docs/system/chat.md` (destinations of the chat content)

## Reproduction
1. Read `backend/app/chat/service.py:466`: `"""The vector of the question: Ollama and the model of the active profile (the only destination of the question)."""`.
2. Compare with `docs/system/chat.md` (end of «The reranker»): the destinations of the chat content are the generation provider (question, history, context), Ollama (the searched text), `REWRITE_BASE_URL` (question and history, when the rewriting is active) and `RERANK_BASE_URL` (the searched text and the texts of the candidates, when the reranking is active). `_search_text` and `_rerank` in the same file send the question to the other services.

## Actual vs expected behaviour
- Actual: the docstring claims a single destination. The claim was already partial in phase 3 (the question also goes to the generation provider) and is false for the question and the history since the rewriting of phase 4.
- Expected: the docstring says what the function does and nothing about the other destinations, for example "The vector of the text that is searched, from Ollama and the model of the active profile"; the list of destinations stays in `docs/system/chat.md`.

## Behaviour that must NOT change
- `_embed` itself, its arguments, its `embedding_error` failure and the destinations of the content (`test_chat_steps_privacy.py`, AC-026).

## Root cause
The docstring was written in phase 3 and not read again when phase 4 added two services that receive the question.

## Fix and tests
- Red test reproducing the bug: not started (a comment: no test; the review checks it)
- Minimal fix: not started (the docstring of `_embed`, `backend/app/chat/service.py:466`)
- Non-regression test on the unchanged behaviour: not started (the existing suite)

## Closure
- [ ] Full `verify` green and the bug's test passing
- [ ] Spec/ACs/`docs/system/` updated if the bug revealed a missing requirement
- [ ] Lesson recorded in `AGENTS.md` or in an ADR, if any
