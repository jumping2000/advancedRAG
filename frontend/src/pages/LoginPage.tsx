import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { t } = useTranslation();
  const { login } = useAuth();
  const navigate = useNavigate();
  // A page that ended the session may send a message to show here (an i18n key), e.g. after an own password reset.
  const notice = (useLocation().state as { notice?: unknown } | null)?.notice;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username, password);
      navigate("/", { replace: true });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.code : "unknown");
      setBusy(false);
    }
  }

  return (
    <main>
      <h2>{t("login.title")}</h2>
      {typeof notice === "string" && <p role="status">{t(notice)}</p>}
      <form onSubmit={(event) => void submit(event)}>
        <label htmlFor="login-username">{t("login.username")}</label>
        <input
          id="login-username"
          name="username"
          autoComplete="username"
          required
          value={username}
          onChange={(event) => setUsername(event.target.value)}
        />
        <label htmlFor="login-password">{t("login.password")}</label>
        <input
          id="login-password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {error && <p role="alert">{t(`errors.${error}`, { defaultValue: t("errors.unknown") })}</p>}
        <button type="submit" disabled={busy}>
          {t("login.submit")}
        </button>
      </form>
    </main>
  );
}
