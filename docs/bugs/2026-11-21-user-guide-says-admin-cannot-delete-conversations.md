# BUG-001: the user guide says the administrators can only read the conversations, but they can also delete them

- Status: open
- Severity / impact: medium, a wrong privacy statement told to the users of the application: it hides that an administrator can delete any conversation. No data is at risk; the application behaves as specified.
- Affected spec or area: SPEC-2026-11-20-fase-5-documentazione (REQ-004, REQ-005), `docs/guide/en/user.md`, `docs/guide/it/user.md`; behaviour specified by SPEC-2026-11-17-fase-3-chat and described in `docs/system/chat.md`

## Reproduction
Found by the final check of phases 1-4 (`docs/audit/2026-11-20-controllo-finale.md`, part 3), by reading the guides against the code.

1. Read the user guide, line 118 of both languages:
   - `docs/guide/en/user.md:118`: "The administrators can read every conversation, in their page «Conversations», but only read it: they cannot write in yours."
   - `docs/guide/it/user.md:118`: "Gli amministratori possono leggere ogni conversazione, nella loro pagina «Conversazioni», ma solo leggerla: non possono scrivere nelle tue."
2. Compare with the code and the other documents:
   - `frontend/src/pages/admin/ConversationsPage.tsx:101` calls `api.delete('/conversations/${target.id}')` and line 168 shows the button `chat.delete` («Delete»);
   - `backend/app/chat/routes.py:283-300`, `delete_conversation`, takes `ReadableConversation`, which for an admin is any conversation (`get_for_admin`);
   - `docs/system/chat.md` (Current behaviour): the admin "reads the conversations of everyone read-only and deletes them";
   - `docs/specs/2026-11-17-fase-3-chat/spec.md:20`: "legge in sola lettura le conversazioni di tutti e le cancella";
   - `docs/guide/en/admin.md:176-178` and the Italian twin say that the admin can delete a conversation ("Deleting is final, for the user too").

## Actual vs expected behaviour
- Actual: the user guide tells the users that an administrator can only read their conversations; it does not say that an administrator can delete them. The two guides of the same release contradict each other (user guide against admin guide).
- Expected (REQ-004 and SPEC-2026-11-17-fase-3-chat, "Confermato dall'owner"): the user guide says that administrators can read every conversation and delete it, and cannot write in yours. The wording of REQ-005 of the phase 5 spec ("in sola lettura") has the same imprecision (the guide admin explains the deletion): a change to a spec is a new design PR, so the bug fix only changes the two user guides.

## Behaviour that must NOT change
- The application: an admin reads every conversation, deletes any, writes in none of another user's (403).
- The texts of the interface (`it.json`, `en.json`) and the English and Italian guides having the same number of `##` and `###` titles, the link to the twin on the first line, and every interface text that the tests of the guides check.

## Root cause
The guide was written from the sentence of the area page "read-only", and the delete of the admin page was described only in the admin guide; the test of the user guide (`test_docs_user_guide.py`) checks interface texts, not statements about the roles.

## Fix and tests
- Red test reproducing the bug: not started
- Minimal fix: not started (the sentence of `docs/guide/en/user.md:118` and `docs/guide/it/user.md:118`)
- Non-regression test on the unchanged behaviour: not started

## Closure
- [ ] Full `verify` green and the bug's test passing
- [ ] Spec/ACs/`docs/system/` updated if the bug revealed a missing requirement
- [ ] Lesson recorded in `AGENTS.md` or in an ADR, if any
