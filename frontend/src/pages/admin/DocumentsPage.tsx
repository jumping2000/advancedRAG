import { useTranslation } from "react-i18next";

/** Skeleton (T-07): the Documents page of the admin (REQ-020); the content comes with the implementation. */
export function DocumentsPage() {
  const { t } = useTranslation();
  return (
    <main>
      <h2>{t("documents.title")}</h2>
    </main>
  );
}
