# BUG-003: the line «Ricerca: …» does not keep the line breaks of a rewritten question

- Status: open
- Severity / impact: low, cosmetic: a multi-line rewritten text is shown on one line under the answer; the text is complete and nothing is lost or exposed.
- Affected spec or area: SPEC-2026-11-20-fase-4-rerank-rewrite (REQ-007), `frontend/src/components/ConversationView.tsx`

## Reproduction
1. Switch on «Riscrittura della domanda» with a rewriting service that answers with a text that has a line break (the service may: `rewrite.py` accepts any non-empty text up to `CHAT_MAX_QUESTION_CHARS`, line breaks included).
2. Ask a follow-up question in a conversation; the answer is saved with `search_query` = that text.
3. Read the line under the answer.

Evidence in the code: `frontend/src/components/ConversationView.tsx:117` shows the answer in `<p style={{ whiteSpace: "pre-wrap" }}>`, and line 132 shows the search line in a plain `<p>`: `{message.search_query && <p>{t("chat.searchQuery", { query: message.search_query })}</p>}`. HTML collapses a line break in a plain `<p>` to a space.

## Actual vs expected behaviour
- Actual: the line breaks of the searched text are shown as spaces.
- Expected: the searched text is shown as it was searched, with its line breaks, like the answer (REQ-007 says the line shows the searched question; the spec does not say what happens to a line break, so the choice of `pre-wrap`, the same as the answer, is the owner's to confirm).

## Behaviour that must NOT change
- The line appears only under an answer whose `search_query` is not null, in the language of the interface, with the text «Ricerca: …» / «Search: …» (`chat.searchQuery`), in the chat page and in the read-only page of the admin alike (the tests of `ConversationView.test.tsx`, AC-013 of phase 4).

## Root cause
The line was added in phase 4 (T-05) as a plain paragraph; only the answer and the excerpt got `pre-wrap` in phase 3.

## Fix and tests
- Red test reproducing the bug: not started
- Minimal fix: not started (`whiteSpace: "pre-wrap"` on the paragraph of line 132, if the owner confirms it)
- Non-regression test on the unchanged behaviour: not started

## Closure
- [ ] Full `verify` green and the bug's test passing
- [ ] Spec/ACs/`docs/system/` updated if the bug revealed a missing requirement
- [ ] Lesson recorded in `AGENTS.md` or in an ADR, if any
