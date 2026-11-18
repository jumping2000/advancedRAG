// SPEC-2026-11-17-fase-3-chat
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import type { User } from "../../auth/AuthContext";
import {
  ConversationView,
  useErrorText,
  type ConversationDetail,
  type ConversationOut,
  type MessageOut,
} from "../../components/ConversationView";
import { codeOf, type KnowledgeBase } from "../KnowledgeBasesPage";

type AdminConversation = ConversationOut & { username: string; kb_name: string; message_count: number };

/** Conversations page of the admin (REQ-020): the conversations of everyone, filtered by KB and by user; one opens
 * read-only (messages and sources, no question field, no «Riprova»); «Elimina» asks for a confirmation first. */
export function ConversationsPage() {
  const { t, i18n } = useTranslation();
  const errorText = useErrorText();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [kbId, setKbId] = useState("");
  const [userId, setUserId] = useState("");
  const [rows, setRows] = useState<AdminConversation[] | null>(null);
  const [reloads, setReloads] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageOut[]>([]);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [confirming, setConfirming] = useState<AdminConversation | null>(null);
  const opened = useRef(0); // which opening of a conversation may still write its messages

  useEffect(() => {
    let current = true;
    api
      .get<KnowledgeBase[]>("/kbs")
      .then((list) => current && setKbs(list))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    api
      .get<User[]>("/admin/users")
      .then((list) => current && setUsers(list))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    return () => {
      current = false;
    };
  }, []);

  // The list for the filters on screen: an answer that arrives for older filters is dropped.
  useEffect(() => {
    let current = true;
    const query = new URLSearchParams();
    if (kbId) query.set("kb_id", kbId);
    if (userId) query.set("user_id", userId);
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    api
      .get<AdminConversation[]>(`/admin/conversations${suffix}`)
      .then((list) => current && setRows(list))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    return () => {
      current = false;
    };
  }, [kbId, userId, reloads]);

  function close() {
    opened.current += 1;
    setSelectedId(null);
    setMessages([]);
    setLoadingConversation(false);
  }

  function filter(setter: (value: string) => void, value: string) {
    setError(null);
    setter(value);
  }

  async function open(conversation: AdminConversation) {
    const mine = ++opened.current;
    setError(null);
    setSelectedId(conversation.id);
    setMessages([]);
    setLoadingConversation(true);
    try {
      const detail = await api.get<ConversationDetail>(`/conversations/${conversation.id}`);
      if (mine === opened.current) setMessages(detail.messages);
    } catch (caught) {
      if (mine === opened.current) setError(codeOf(caught));
    } finally {
      if (mine === opened.current) setLoadingConversation(false);
    }
  }

  async function confirmDeletion() {
    if (!confirming) return;
    const target = confirming;
    setConfirming(null);
    setError(null);
    try {
      await api.delete<void>(`/conversations/${target.id}`);
      setRows((list) => list?.filter((row) => row.id !== target.id) ?? null);
      if (target.id === selectedId) close();
    } catch (caught) {
      setError(codeOf(caught));
      setReloads((count) => count + 1); // what is on screen may be out of date
    }
  }

  const selected = rows?.find((row) => row.id === selectedId);

  return (
    <main>
      <h2>{t("convAdmin.title")}</h2>
      {error && <p className="error">{errorText(error)}</p>}

      <div>
        <label htmlFor="conv-kb">{t("convAdmin.kb")}</label>
        <select id="conv-kb" value={kbId} onChange={(event) => filter(setKbId, event.target.value)}>
          <option value="">{t("convAdmin.allKbs")}</option>
          {kbs.map((kb) => (
            <option key={kb.id} value={kb.id}>
              {kb.name}
            </option>
          ))}
        </select>
        <label htmlFor="conv-user">{t("convAdmin.user")}</label>
        <select id="conv-user" value={userId} onChange={(event) => filter(setUserId, event.target.value)}>
          <option value="">{t("convAdmin.allUsers")}</option>
          {users.map((user) => (
            <option key={user.id} value={user.id}>
              {user.username}
            </option>
          ))}
        </select>
      </div>

      {rows?.length === 0 && !error && <p>{t("chat.noConversations")}</p>}
      <table>
        <thead>
          <tr>
            <th scope="col">{t("convAdmin.titleColumn")}</th>
            <th scope="col">{t("convAdmin.user")}</th>
            <th scope="col">{t("convAdmin.kb")}</th>
            <th scope="col">{t("convAdmin.date")}</th>
            <th scope="col">{t("convAdmin.count")}</th>
            <th scope="col">{t("convAdmin.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => (
            <tr key={row.id}>
              <td>
                <button
                  type="button"
                  aria-current={row.id === selectedId ? "true" : undefined}
                  onClick={() => void open(row)}
                >
                  {row.title}
                </button>
              </td>
              <td>{row.username}</td>
              <td>{row.kb_name}</td>
              <td>{new Date(row.updated_at).toLocaleString(i18n.language)}</td>
              <td>{row.message_count}</td>
              <td>
                <button type="button" onClick={() => setConfirming(row)}>
                  {t("chat.delete")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {selectedId && (
        <section>
          {selected && <h3>{selected.title}</h3>}
          {loadingConversation && <p>{t("chat.loading")}</p>}
          {messages.length > 0 && (
            <ConversationView key={selectedId} conversation={{ id: selectedId, messages }} readOnly />
          )}
        </section>
      )}

      {confirming && (
        <div role="dialog" aria-modal="true" aria-labelledby="conv-confirm-text">
          <p id="conv-confirm-text">{t("chat.confirmDelete", { title: confirming.title })}</p>
          <button type="button" onClick={() => void confirmDeletion()}>
            {t("chat.confirm")}
          </button>
          <button type="button" onClick={() => setConfirming(null)}>
            {t("chat.cancel")}
          </button>
        </div>
      )}
    </main>
  );
}
