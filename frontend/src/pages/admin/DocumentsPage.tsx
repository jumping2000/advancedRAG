import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError } from "../../api/client";
import type { KnowledgeBase } from "../KnowledgeBasesPage";

/** While a document is QUEUED or PROCESSING the list is read again at this interval (REQ-020). */
const POLL_INTERVAL_MS = 3000;
const KIB = 1024;

type DocumentStatus = "QUEUED" | "PROCESSING" | "READY" | "FAILED" | "UNSUPPORTED";

type DocumentItem = {
  id: string;
  kb_id: string;
  filename: string;
  size_bytes: number;
  status: DocumentStatus;
  status_reason: { code: string; message: string } | null;
  chunk_count: number;
  created_at: string;
  updated_at: string;
};

/** A file of the last selection that is being sent or was refused (an accepted one becomes a row of the table). */
type Upload = { key: number; name: string; error: string | null };

function codeOf(caught: unknown): string {
  return caught instanceof ApiError ? caught.code : "unknown";
}

const isActive = (document: DocumentItem) => document.status === "QUEUED" || document.status === "PROCESSING";

function sizeText(bytes: number, language: string): string {
  const number = (value: number) => new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(value);
  if (bytes < KIB) return `${number(bytes)} B`;
  if (bytes < KIB * KIB) return `${number(bytes / KIB)} KB`;
  return `${number(bytes / (KIB * KIB))} MB`;
}

/** Documents page of the admin (REQ-020): choose a KB, send one or more files (one request each), follow the state of
 * every document, retry a FAILED one and delete after a confirmation. The list is read again every 3 seconds while a
 * document is QUEUED or PROCESSING. */
export function DocumentsPage() {
  const { t, i18n } = useTranslation();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState("");
  const [documents, setDocuments] = useState<DocumentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [uploading, setUploading] = useState(false);
  const [retryErrors, setRetryErrors] = useState<Record<string, string>>({});
  const [withoutOriginal, setWithoutOriginal] = useState<Record<string, true>>({});
  const [confirming, setConfirming] = useState<DocumentItem | null>(null);
  // The KB whose list the page shows, and a counter of the changes made here: an answer that arrives for another KB, or
  // after a change, is older than what is on the screen and is dropped.
  const current = useRef("");
  const changes = useRef(0);
  const nextKey = useRef(0);

  useEffect(() => {
    let live = true;
    api
      .get<KnowledgeBase[]>("/kbs")
      .then((list) => live && setKbs(list))
      .catch((caught: unknown) => live && setError(codeOf(caught)));
    return () => {
      live = false;
    };
  }, []);

  const refresh = useCallback(async (id: string) => {
    const seen = changes.current;
    try {
      const list = await api.get<DocumentItem[]>(`/kbs/${id}/documents`);
      if (current.current !== id || changes.current !== seen) return;
      setDocuments(list);
      setError(null);
    } catch (caught) {
      if (current.current === id) setError(codeOf(caught));
    }
  }, []);

  const active = documents?.some(isActive) ?? false;
  useEffect(() => {
    if (kbId === "" || !active) return;
    let reading = false;
    const timer = setInterval(() => {
      if (reading) return;
      reading = true;
      void refresh(kbId).finally(() => {
        reading = false;
      });
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [kbId, active, refresh]);

  function chooseKb(id: string) {
    current.current = id;
    changes.current += 1;
    setKbId(id);
    setDocuments(null);
    setError(null);
    setActionError(null);
    setUploads([]);
    setRetryErrors({});
    setWithoutOriginal({});
    setConfirming(null);
    if (id !== "") void refresh(id);
  }

  async function sendFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (kbId === "" || files.length === 0) return;
    const id = kbId;
    const items = files.map((file) => ({ key: (nextKey.current += 1), name: file.name, error: null }));
    setUploads(items);
    setActionError(null);
    setUploading(true);
    for (const [index, file] of files.entries()) {
      const { key } = items[index];
      try {
        const created = await api.upload<DocumentItem>(`/kbs/${id}/documents`, file);
        changes.current += 1;
        setDocuments((list) => [created, ...(list ?? []).filter((item) => item.id !== created.id)]);
        setUploads((list) => list.filter((item) => item.key !== key));
      } catch (caught) {
        const code = codeOf(caught);
        setUploads((list) => list.map((item) => (item.key === key ? { ...item, error: code } : item)));
      }
    }
    setUploading(false);
  }

  async function retry(document: DocumentItem) {
    const id = kbId;
    setRetryErrors((errors) => Object.fromEntries(Object.entries(errors).filter(([key]) => key !== document.id)));
    changes.current += 1;
    try {
      const requeued = await api.post<DocumentItem>(`/kbs/${id}/documents/${document.id}/retry`);
      changes.current += 1;
      setDocuments((list) => list?.map((item) => (item.id === requeued.id ? requeued : item)) ?? null);
    } catch (caught) {
      const code = codeOf(caught);
      setRetryErrors((errors) => ({ ...errors, [document.id]: code }));
      if (code === "original_missing") setWithoutOriginal((marked) => ({ ...marked, [document.id]: true }));
      else if (code === "not_retryable") void refresh(id);
    }
  }

  async function confirmDeletion() {
    if (!confirming) return;
    const document = confirming;
    const id = kbId;
    setConfirming(null);
    setActionError(null);
    changes.current += 1;
    try {
      await api.delete<void>(`/kbs/${id}/documents/${document.id}`);
      changes.current += 1;
      setDocuments((list) => list?.filter((item) => item.id !== document.id) ?? null);
    } catch (caught) {
      setActionError(codeOf(caught));
      void refresh(id);
    }
  }

  const message = (code: string) =>
    t(`documents.errors.${code}`, { defaultValue: t(`errors.${code}`, { defaultValue: t("errors.unknown") }) });
  const language = i18n.language;

  return (
    <main>
      <h2>{t("documents.title")}</h2>
      {/* Not an alert: the header's language selector owns the alert of a failed language save. */}
      {error && <p className="error">{message(error)}</p>}
      {actionError && <p className="error">{message(actionError)}</p>}

      <label htmlFor="documents-kb">{t("documents.kb")}</label>
      <select id="documents-kb" value={kbId} disabled={uploading} onChange={(event) => chooseKb(event.target.value)}>
        <option value="">{t("documents.chooseKb")}</option>
        {kbs.map((kb) => (
          <option key={kb.id} value={kb.id}>
            {kb.name}
          </option>
        ))}
      </select>

      <label htmlFor="documents-files">{t("documents.files")}</label>
      <input
        id="documents-files"
        type="file"
        multiple
        disabled={documents === null || uploading}
        onChange={(event) => void sendFiles(event)}
      />

      {uploads.length > 0 && (
        <ul aria-label={t("documents.uploads")}>
          {uploads.map((upload) => (
            <li key={upload.key}>
              <span>{upload.name}</span>: <span>{upload.error ? message(upload.error) : t("documents.uploading")}</span>
            </li>
          ))}
        </ul>
      )}

      {documents && documents.length === 0 && <p>{t("documents.empty")}</p>}
      {documents && documents.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">{t("documents.name")}</th>
              <th scope="col">{t("documents.size")}</th>
              <th scope="col">{t("documents.status")}</th>
              <th scope="col">{t("documents.reason")}</th>
              <th scope="col">{t("documents.date")}</th>
              <th scope="col">{t("documents.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => (
              <tr key={document.id}>
                <td>{document.filename}</td>
                <td>{sizeText(document.size_bytes, language)}</td>
                <td>{t(`documents.statuses.${document.status}`)}</td>
                <td>
                  {document.status_reason &&
                    t(`documents.reasons.${document.status_reason.code}`, {
                      defaultValue: document.status_reason.message,
                    })}
                </td>
                <td>{new Date(document.created_at).toLocaleString(language)}</td>
                <td>
                  {document.status === "FAILED" && !withoutOriginal[document.id] && (
                    <button type="button" onClick={() => void retry(document)}>
                      {t("documents.retry")}
                    </button>
                  )}
                  <button type="button" onClick={() => setConfirming(document)}>
                    {t("documents.delete")}
                  </button>
                  {retryErrors[document.id] && <p className="error">{message(retryErrors[document.id])}</p>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirming && (
        <div role="dialog" aria-modal="true" aria-labelledby="documents-confirm-text">
          <p id="documents-confirm-text">{t("documents.confirmDelete", { name: confirming.filename })}</p>
          <button type="button" onClick={() => void confirmDeletion()}>
            {t("documents.confirm")}
          </button>
          <button type="button" onClick={() => setConfirming(null)}>
            {t("documents.cancel")}
          </button>
        </div>
      )}
    </main>
  );
}
