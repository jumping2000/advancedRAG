// SPEC-2026-11-17-fase-3-chat
// Skeleton of T-06: the types and the props of the component, so that the tests can import it; it shows nothing yet.

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
};

export type ConversationOut = { id: string; kb_id: string; title: string; created_at: string; updated_at: string };

export type ConversationDetail = ConversationOut & { messages: MessageOut[]; read_only: boolean };

type Props = {
  conversation: Pick<ConversationDetail, "id" | "messages">;
  readOnly: boolean;
  onRetry?: (message: MessageOut) => void;
};

export function ConversationView(props: Props) {
  void props;
  return null;
}
