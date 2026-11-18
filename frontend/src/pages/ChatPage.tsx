// SPEC-2026-11-17-fase-3-chat
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import { api, ApiError, type StreamEvent } from "../api/client";
import {
  ConversationView,
  useErrorText,
  type ConversationDetail,
  type ConversationOut,
  type MessageOut,
} from "../components/ConversationView";
import { codeOf, type KnowledgeBase } from "./KnowledgeBasesPage";

/** Seconds between two reads of a conversation that was opened with an answer still in progress */
const POLL_MS = 3000;

type Renaming = { id: string; title: string; error: string | null };

const text = (value: unknown): string => (typeof value === "string" ? value : "");

function placeholder(id: string, role: "user" | "assistant", content: string): MessageOut {
  const status = role === "user" ? "COMPLETED" : "PENDING";
  return { id, role, content, status, error_code: null, notice: null, created_at: "", sources: [] };
}

/** The page of a KB (REQ-019): its name and description, the own conversations on the left, the messages and the
 * question on the right. The answer is read from the stream as it arrives. A KB that is not assigned and one that does
 * not exist show the same message. */
export function ChatPage() {
  const { kbId = "" } = useParams();
  // One page state per KB: another KB starts from nothing
  return <KbChat key={kbId} kbId={kbId} />;
}

function KbChat({ kbId }: { kbId: string }) {
  const { t } = useTranslation();
  const errorText = useErrorText();
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [kbError, setKbError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationOut[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageOut[]>([]);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [question, setQuestion] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<Renaming | null>(null);
  const [confirming, setConfirming] = useState<ConversationOut | null>(null);
  const [pollRound, setPollRound] = useState(0);
  const abort = useRef<AbortController | null>(null);
  const opened = useRef(0); // which opening of a conversation may still write its messages

  // An answer in progress that this page did not start (the conversation was opened with it): the page keeps reading
  // the conversation until it ends. The one this page streams ends with its stream.
  const pending = messages.some((message) => message.status === "PENDING");
  const busy = streaming || pending;

  useEffect(() => {
    let current = true;
    async function load() {
      try {
        const found = await api.get<KnowledgeBase>(`/kbs/${encodeURIComponent(kbId)}`);
        if (current) setKb(found);
      } catch (caught) {
        if (current) setKbError(codeOf(caught));
        return; // a KB that is not visible has no conversations to ask for
      }
      try {
        const list = await api.get<ConversationOut[]>(`/kbs/${encodeURIComponent(kbId)}/conversations`);
        if (current) setConversations(list);
      } catch {
        if (current) setConversations([]);
      }
    }
    void load();
    return () => {
      current = false;
      abort.current?.abort();
      opened.current += 1;
    };
  }, [kbId]);

  useEffect(() => {
    if (!selectedId || !pending || streaming) return;
    let current = true;
    const timer = setTimeout(() => {
      api
        .get<ConversationDetail>(`/conversations/${selectedId}`)
        .then((detail) => {
          if (!current) return;
          setMessages(detail.messages);
          setPollRound((round) => round + 1);
        })
        .catch((caught: unknown) => {
          if (!current) return;
          setActionError(codeOf(caught));
          setMessages((list) =>
            list.map((m) => (m.status === "PENDING" ? { ...m, status: "FAILED", error_code: "interrupted" } : m)),
          );
        });
    }, POLL_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [selectedId, pending, streaming, pollRound]);

  function refreshList() {
    api
      .get<ConversationOut[]>(`/kbs/${encodeURIComponent(kbId)}/conversations`)
      .then(setConversations)
      .catch(() => undefined); // the list on screen stays
  }

  async function open(conversation: ConversationOut) {
    const mine = ++opened.current;
    setSelectedId(conversation.id);
    setMessages([]);
    setActionError(null);
    setLoadingConversation(true);
    try {
      const detail = await api.get<ConversationDetail>(`/conversations/${conversation.id}`);
      if (mine === opened.current) setMessages(detail.messages);
    } catch (caught) {
      if (mine === opened.current) setActionError(codeOf(caught));
    } finally {
      if (mine === opened.current) setLoadingConversation(false);
    }
  }

  function startNew() {
    opened.current += 1;
    setSelectedId(null);
    setMessages([]);
    setActionError(null);
    setLoadingConversation(false);
  }

  /** The answer of `path` (a question or a retry), shown as its events arrive. `before` is what the messages were: it
   * comes back if the server refuses before the stream starts, and `asked` goes back to the field. */
  async function stream(path: string, body: unknown, before: MessageOut[], asked?: string) {
    const controller = new AbortController();
    abort.current = controller;
    const isNew = selectedId === null;
    let conversationId = selectedId;
    let started = false;
    let ended = false;
    const last = (update: (message: MessageOut) => MessageOut) =>
      setMessages((list) => list.map((message, index) => (index === list.length - 1 ? update(message) : message)));

    function onEvent({ type, data }: StreamEvent) {
      const fields = (data ?? {}) as Record<string, unknown>;
      if (type === "conversation") {
        started = true;
        conversationId = text(fields.conversation_id);
        last((message) => ({ ...message, id: text(fields.message_id) }));
        if (isNew) {
          setSelectedId(conversationId);
          refreshList();
        }
      } else if (type === "delta") {
        last((message) => ({ ...message, content: message.content + text(fields.text) }));
      } else if (type === "done") {
        ended = true;
        last(() => fields.message as MessageOut);
      } else {
        ended = true;
        const code = text((fields.error as Record<string, unknown> | undefined)?.code);
        last((message) => ({ ...message, status: "FAILED", content: "", error_code: code || "internal_error" }));
      }
    }

    setStreaming(true);
    try {
      await api.stream(path, body, onEvent, controller.signal);
    } catch (caught) {
      if (controller.signal.aborted) return;
      if (!started) {
        // Refused before the stream (or no connection): nothing was answered
        setMessages(before);
        if (asked !== undefined) setQuestion(asked);
        setActionError(codeOf(caught));
        if (caught instanceof ApiError && caught.status === 404) refreshList();
        setStreaming(false);
        return;
      }
    }
    if (controller.signal.aborted) return;
    if (!ended && conversationId) {
      // The stream ended without done or error: what the server saved is what counts
      try {
        const detail = await api.get<ConversationDetail>(`/conversations/${conversationId}`);
        if (!controller.signal.aborted) setMessages(detail.messages);
      } catch (caught) {
        if (controller.signal.aborted) return;
        setActionError(codeOf(caught));
        last((message) => ({ ...message, status: "FAILED", content: "", error_code: "interrupted" }));
      }
    }
    setStreaming(false);
    refreshList();
  }

  function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const asked = question.trim();
    if (!asked || busy) return;
    const before = messages;
    setActionError(null);
    setQuestion("");
    setMessages([
      ...before,
      placeholder("pending-question", "user", asked),
      placeholder("pending-answer", "assistant", ""),
    ]);
    const path =
      selectedId === null ? `/kbs/${encodeURIComponent(kbId)}/conversations` : `/conversations/${selectedId}/messages`;
    void stream(path, { question: asked }, before, asked);
  }

  function retry(failed: MessageOut) {
    if (busy || selectedId === null) return;
    const before = messages;
    setActionError(null);
    setMessages(
      before.map((m) => (m.id === failed.id ? { ...m, status: "PENDING", content: "", error_code: null } : m)),
    );
    void stream(`/conversations/${selectedId}/messages/${failed.id}/retry`, undefined, before);
  }

  async function saveTitle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!renaming) return;
    try {
      const updated = await api.patch<ConversationOut>(`/conversations/${renaming.id}`, {
        title: renaming.title.trim(),
      });
      setConversations((list) => list?.map((c) => (c.id === updated.id ? updated : c)) ?? null);
      setRenaming(null);
    } catch (caught) {
      setRenaming({ ...renaming, error: codeOf(caught) });
    }
  }

  async function confirmDeletion() {
    if (!confirming) return;
    const target = confirming;
    setConfirming(null);
    try {
      await api.delete<void>(`/conversations/${target.id}`);
      setConversations((list) => list?.filter((c) => c.id !== target.id) ?? null);
      if (target.id === selectedId) startNew();
    } catch (caught) {
      setActionError(codeOf(caught));
      refreshList();
    }
  }

  const selected = conversations?.find((c) => c.id === selectedId);

  return (
    <main>
      {kbError && <p role="alert">{errorText(kbError)}</p>}
      {kb && (
        <>
          <h2>{kb.name}</h2>
          {kb.description && <p>{kb.description}</p>}
          <nav aria-label={t("chat.conversations")}>
            <h3>{t("chat.conversations")}</h3>
            <button type="button" disabled={busy} onClick={startNew}>
              {t("chat.new")}
            </button>
            {conversations?.length === 0 && <p>{t("chat.noConversations")}</p>}
            {conversations && conversations.length > 0 && (
              <ul>
                {conversations.map((conversation) =>
                  renaming?.id === conversation.id ? (
                    <li key={conversation.id}>
                      <form onSubmit={(event) => void saveTitle(event)}>
                        <label htmlFor="chat-title">{t("chat.renameLabel")}</label>
                        <input
                          id="chat-title"
                          value={renaming.title}
                          onChange={(event) => setRenaming({ ...renaming, title: event.target.value, error: null })}
                        />
                        <button type="submit">{t("chat.save")}</button>
                        <button type="button" onClick={() => setRenaming(null)}>
                          {t("chat.cancel")}
                        </button>
                        {renaming.error && <p className="error">{errorText(renaming.error)}</p>}
                      </form>
                    </li>
                  ) : (
                    <li key={conversation.id}>
                      <button
                        type="button"
                        disabled={busy}
                        aria-current={conversation.id === selectedId ? "true" : undefined}
                        onClick={() => void open(conversation)}
                      >
                        {conversation.title}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setRenaming({ id: conversation.id, title: conversation.title, error: null })}
                      >
                        {t("chat.rename")}
                      </button>
                      <button type="button" disabled={busy} onClick={() => setConfirming(conversation)}>
                        {t("chat.delete")}
                      </button>
                    </li>
                  ),
                )}
              </ul>
            )}
          </nav>
          <section>
            {selected && <h3>{selected.title}</h3>}
            {loadingConversation && <p>{t("chat.loading")}</p>}
            {!loadingConversation && messages.length === 0 && <p>{t("chat.empty")}</p>}
            {messages.length > 0 && (
              <ConversationView
                key={selectedId ?? "new"}
                conversation={{ id: selectedId ?? "", messages }}
                readOnly={false}
                onRetry={retry}
              />
            )}
            {actionError && <p className="error">{errorText(actionError)}</p>}
            <form onSubmit={send}>
              <label htmlFor="chat-question">{t("chat.question")}</label>
              <textarea
                id="chat-question"
                rows={3}
                value={question}
                disabled={busy}
                onChange={(event) => setQuestion(event.target.value)}
              />
              <button type="submit" disabled={busy || question.trim() === ""}>
                {t("chat.send")}
              </button>
            </form>
          </section>
          {confirming && (
            <div role="dialog" aria-modal="true" aria-labelledby="chat-confirm-text">
              <p id="chat-confirm-text">{t("chat.confirmDelete", { title: confirming.title })}</p>
              <button type="button" onClick={() => void confirmDeletion()}>
                {t("chat.confirm")}
              </button>
              <button type="button" onClick={() => setConfirming(null)}>
                {t("chat.cancel")}
              </button>
            </div>
          )}
        </>
      )}
      <Link to="/">{t("kbs.back")}</Link>
    </main>
  );
}
