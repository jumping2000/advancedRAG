import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { Language } from "../i18n";

const LANGUAGES: Language[] = ["it", "en"];

/** Changes the UI language without reloading; after sign-in it also saves it in the profile (REQ-011). */
export function LanguageSelector() {
  const { t, i18n } = useTranslation();
  const { setLanguage } = useAuth();
  const [error, setError] = useState<string | null>(null);

  async function choose(language: Language) {
    setError(null);
    try {
      await setLanguage(language);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.code : "unknown");
    }
  }

  return (
    <div>
      <label htmlFor="language-selector">{t("language.label")}</label>
      <select
        id="language-selector"
        value={i18n.resolvedLanguage}
        onChange={(event) => void choose(event.target.value as Language)}
      >
        {LANGUAGES.map((language) => (
          <option key={language} value={language}>
            {t(`language.${language}`)}
          </option>
        ))}
      </select>
      {error && <p role="alert">{t(`errors.${error}`, { defaultValue: t("errors.unknown") })}</p>}
    </div>
  );
}
