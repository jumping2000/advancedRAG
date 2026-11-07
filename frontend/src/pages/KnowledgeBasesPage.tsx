import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export type KnowledgeBase = {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
};

function codeOf(caught: unknown): string {
  return caught instanceof ApiError ? caught.code : "unknown";
}

function useMessage() {
  const { t } = useTranslation();
  return (code: string) =>
    t(`kbs.errors.${code}`, { defaultValue: t(`errors.${code}`, { defaultValue: t("errors.unknown") }) });
}

/** The home page of every signed-in user: the KBs the backend lets them see (REQ-010, REQ-012). */
export function KnowledgeBasesPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const message = useMessage();
  const [kbs, setKbs] = useState<KnowledgeBase[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    api
      .get<KnowledgeBase[]>("/kbs")
      .then((list) => current && setKbs(list))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    return () => {
      current = false;
    };
  }, []);

  return (
    <main>
      <p>{t("home.signedInAs", { username: user?.username })}</p>
      <h2 id="kbs-title">{t("kbs.title")}</h2>
      {/* Not an alert: the header's language selector owns the alert of a failed language save. */}
      {error && <p className="error">{message(error)}</p>}
      {kbs && kbs.length === 0 && <p>{t("kbs.empty")}</p>}
      {kbs && kbs.length > 0 && (
        <ul aria-labelledby="kbs-title">
          {kbs.map((kb) => (
            <li key={kb.id}>
              <Link to={`/kbs/${kb.id}`}>{kb.name}</Link>
              {kb.description && <p>{kb.description}</p>}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

/** One KB opened from the list; a KB not assigned and one that does not exist show the same message (REQ-010). */
export function KnowledgeBasePage() {
  const { t } = useTranslation();
  const { kbId = "" } = useParams();
  const message = useMessage();
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    api
      .get<KnowledgeBase>(`/kbs/${encodeURIComponent(kbId)}`)
      .then((found) => current && setKb(found))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    return () => {
      current = false;
    };
  }, [kbId]);

  return (
    <main>
      {error && <p role="alert">{message(error)}</p>}
      {kb && (
        <>
          <h2>{kb.name}</h2>
          {kb.description && <p>{kb.description}</p>}
        </>
      )}
      <Link to="/">{t("kbs.back")}</Link>
    </main>
  );
}
