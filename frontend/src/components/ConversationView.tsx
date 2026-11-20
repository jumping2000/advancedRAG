// SPEC-2026-11-17-fase-3-chat
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError } from "../api/client";

export type SourceOut = {
  number: number;
  document_name: string;
  section_title: string | null;
  page_start: number | null;
  page_end: number | null;
};

export type SourceExcerpt = SourceOut & { available: boolean; content: string | null };

export type MessageOut = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "PENDING" | "COMPLETED" | "FAILED";
  error_code: string | null;
  notice: string | null;
  created_at: string;
  sources: SourceOut[];
  /** Phase 4: the searched question of an answer whose question was rewritten; null (or absent) otherwise. */
  search_query?: string | null;
};

export type ConversationOut = { id: string; kb_id: string; title: string; created_at: string; updated_at: string };

export type ConversationDetail = ConversationOut & { messages: MessageOut[]; read_only: boolean };

/** The text of an error code of the chat API: the chat's own, then those of the KBs and the general ones. */
export function useErrorText() {
  const { t } = useTranslation();
  return (code: string) =>
    t(`chat.errors.${code}`, {
      defaultValue: t(`kbs.errors.${code}`, {
        defaultValue: t(`errors.${code}`, { defaultValue: t("errors.unknown") }),
      }),
    });
}

type Excerpt = { state: "loading" } | { state: "error"; code: string } | { state: "ready"; excerpt: SourceExcerpt };

type Props = {
  /** The id is the one of the conversation that holds the messages (the excerpts are read from it). */
  conversation: Pick<ConversationDetail, "id" | "messages">;
  /** A conversation of someone else (the admin reads it): no «Riprova». The excerpts open all the same. */
  readOnly: boolean;
  /** «Riprova» of the failed last answer. */
  onRetry?: (message: MessageOut) => void;
};

/** The messages of a conversation: the question, the answer as text (the [n] of a source are buttons, nothing is
 * HTML), the sources under it with their excerpts, the reason of a failure and «Riprova» (REQ-019, REQ-020). */
export function ConversationView({ conversation, readOnly, onRetry }: Props) {
  const { t } = useTranslation();
  const errorText = useErrorText();
  const [open, setOpen] = useState<Record<string, Excerpt>>({});
  const { messages } = conversation;

  function toggle(message: MessageOut, number: number) {
    const key = `${message.id}:${number}`;
    if (key in open) {
      setOpen((current) => Object.fromEntries(Object.entries(current).filter(([name]) => name !== key)));
      return;
    }
    setOpen((current) => ({ ...current, [key]: { state: "loading" } }));
    const path = `/conversations/${conversation.id}/messages/${message.id}/sources/${number}`;
    const show = (result: Excerpt) => setOpen((current) => (key in current ? { ...current, [key]: result } : current));
    api
      .get<SourceExcerpt>(path)
      .then((excerpt) => show({ state: "ready", excerpt }))
      .catch((caught: unknown) => show({ state: "error", code: caught instanceof ApiError ? caught.code : "unknown" }));
  }

  /** The text with the valid [n] (those of the sources of the message) as buttons that open their excerpt. */
  function withCitations(message: MessageOut): ReactNode[] {
    const numbers = new Set(message.sources.map((source) => source.number));
    return message.content.split(/(\[\d+\])/).map((part, index) => {
      const number = /^\[(\d+)\]$/.exec(part)?.[1];
      if (number === undefined || !numbers.has(Number(number))) return part;
      return (
        <button key={index} type="button" onClick={() => toggle(message, Number(number))}>
          {part}
        </button>
      );
    });
  }

  function pages(source: SourceOut): string {
    const { page_start: start, page_end: end } = source;
    if (start === null) return "";
    return end === null || end === start ? t("chat.page", { start }) : t("chat.pages", { start, end });
  }

  function excerptOf(message: MessageOut, source: SourceOut): ReactNode {
    const shown = open[`${message.id}:${source.number}`];
    if (!shown) return null;
    if (shown.state === "loading") return <p>{t("chat.loading")}</p>;
    if (shown.state === "error") return <p className="error">{errorText(shown.code)}</p>;
    if (!shown.excerpt.available) return <p>{t("chat.sourceUnavailable")}</p>;
    return <blockquote style={{ whiteSpace: "pre-wrap" }}>{shown.excerpt.content}</blockquote>;
  }

  return (
    <ol aria-label={t("chat.messages")}>
      {messages.map((message, index) => (
        <li key={message.id}>
          <strong>{t(message.role === "user" ? "chat.you" : "chat.assistant")}</strong>
          {message.notice && message.content === "" ? (
            <p>{t(`chat.notices.${message.notice}`, { defaultValue: "" })}</p>
          ) : (
            <p style={{ whiteSpace: "pre-wrap" }}>{withCitations(message)}</p>
          )}
          {message.status === "PENDING" && <p>{t("chat.generating")}</p>}
          {message.status === "FAILED" && (
            <>
              <p className="error">
                {t(`chat.failures.${message.error_code ?? ""}`, { defaultValue: t("errors.unknown") })}
              </p>
              {!readOnly && onRetry && index === messages.length - 1 && (
                <button type="button" onClick={() => onRetry(message)}>
                  {t("chat.retry")}
                </button>
              )}
            </>
          )}
          {message.sources.length > 0 && (
            <ul aria-label={t("chat.sources")}>
              {message.sources.map((source) => {
                const shown = `${message.id}:${source.number}` in open;
                return (
                  <li key={source.number}>
                    <span>
                      [{source.number}] {source.document_name}
                      {source.section_title ? ` · ${source.section_title}` : ""}
                      {pages(source) ? ` · ${pages(source)}` : ""}
                    </span>{" "}
                    <button type="button" onClick={() => toggle(message, source.number)}>
                      {t(shown ? "chat.hideExcerpt" : "chat.showExcerpt")}
                    </button>
                    {excerptOf(message, source)}
                  </li>
                );
              })}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}
