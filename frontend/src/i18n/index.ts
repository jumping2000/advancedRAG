import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./en.json";
import it from "./it.json";

export type Language = "it" | "en";

/** Italian if the first browser language starts with "it", English otherwise (REQ-011). */
export function browserLanguage(): Language {
  const first = navigator.languages?.[0] ?? navigator.language ?? "";
  return first.toLowerCase().startsWith("it") ? "it" : "en";
}

i18n.on("languageChanged", (language) => {
  document.documentElement.lang = language;
});

void i18n.use(initReactI18next).init({
  resources: { it: { translation: it }, en: { translation: en } },
  lng: browserLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },
  initAsync: false,
});

export { i18n };
