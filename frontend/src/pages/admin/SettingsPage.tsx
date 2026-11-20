// SPEC-2026-11-20-fase-4-rerank-rewrite
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import { codeOf } from "../KnowledgeBasesPage";

type StepState = { enabled: boolean; configured: boolean };
type SettingsOut = { rerank: StepState; rewrite: StepState };
type Step = keyof SettingsOut;

/** The two switches in the order of the page: the field each one saves and its texts. */
const STEPS: { step: Step; field: "rewrite_enabled" | "rerank_enabled"; label: string; help: string }[] = [
  { step: "rewrite", field: "rewrite_enabled", label: "settings.rewrite", help: "settings.rewriteHelp" },
  { step: "rerank", field: "rerank_enabled", label: "settings.rerank", help: "settings.rerankHelp" },
];

/** Settings page of the admin (REQ-012): the switches of the question rewriting and of the reranker, each saved by
 * `PUT /api/admin/settings` as soon as it changes. A switch whose service is not configured in `.env` is disabled with
 * «Non configurato» and still shows the saved state. A refused save puts the switch back and shows the reason. */
export function SettingsPage() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<SettingsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<Step[]>([]); // a switch waits for its own save before it changes again

  useEffect(() => {
    let current = true;
    api
      .get<SettingsOut>("/admin/settings")
      .then((saved) => current && setSettings(saved))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    return () => {
      current = false;
    };
  }, []);

  async function change(step: Step, field: string, enabled: boolean) {
    if (!settings) return;
    const previous = settings;
    setError(null);
    setSaving((list) => [...list, step]);
    // Shown at once, put back if refused; each answer changes only its own step (the other may be saving too)
    const show = (state: StepState) => setSettings((shown) => (shown ? { ...shown, [step]: state } : shown));
    show({ ...previous[step], enabled });
    try {
      const saved = await api.put<SettingsOut>("/admin/settings", { [field]: enabled });
      show(saved[step]);
    } catch (caught) {
      show(previous[step]);
      setError(codeOf(caught));
    } finally {
      setSaving((list) => list.filter((name) => name !== step));
    }
  }

  const message = (code: string) =>
    t(`settings.errors.${code}`, { defaultValue: t(`errors.${code}`, { defaultValue: t("errors.unknown") }) });

  return (
    <main>
      <h2>{t("settings.title")}</h2>
      {/* Not an alert: the header's language selector owns the alert of a failed language save. */}
      {error && <p className="error">{message(error)}</p>}
      {settings &&
        STEPS.map(({ step, field, label, help }) => {
          const state = settings[step];
          const id = `settings-${step}`;
          return (
            <div key={step}>
              <input
                id={id}
                type="checkbox"
                role="switch"
                checked={state.enabled}
                disabled={!state.configured || saving.includes(step)}
                aria-describedby={`${id}-help${state.configured ? "" : ` ${id}-status`}`}
                onChange={(event) => void change(step, field, event.target.checked)}
              />
              <label htmlFor={id}>{t(label)}</label>
              <p id={`${id}-help`}>{t(help)}</p>
              {!state.configured && <p id={`${id}-status`}>{t("settings.notConfigured")}</p>}
            </div>
          );
        })}
    </main>
  );
}
