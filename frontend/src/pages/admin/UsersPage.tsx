import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError } from "../../api/client";
import type { User } from "../../auth/AuthContext";

type Role = User["role"];

function codeOf(caught: unknown): string {
  return caught instanceof ApiError ? caught.code : "unknown";
}

/** Users page of the admin (REQ-007, REQ-012): create, deactivate or reactivate, reset the password. */
export function UsersPage() {
  const { t } = useTranslation();
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [resetting, setResetting] = useState<User | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let current = true;
    api
      .get<User[]>("/admin/users")
      .then((list) => current && setUsers(list))
      .catch((caught: unknown) => current && setError(codeOf(caught)));
    return () => {
      current = false;
    };
  }, []);

  /** Run an API action: one at a time, its error shown in the UI language. */
  async function run(action: () => Promise<void>) {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await action();
    } catch (caught) {
      setError(codeOf(caught));
    } finally {
      setBusy(false);
    }
  }

  function replace(user: User) {
    setUsers((list) => list.map((item) => (item.id === user.id ? user : item)));
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(async () => {
      const created = await api.post<User>("/admin/users", { username, password, role });
      setUsers((list) => [...list, created]);
      setUsername("");
      setPassword("");
      setRole("user");
      setNotice(t("users.created", { username: created.username }));
    });
  }

  function setActive(user: User, isActive: boolean) {
    void run(async () => replace(await api.patch<User>(`/admin/users/${user.id}`, { is_active: isActive })));
  }

  function openReset(user: User) {
    setError(null);
    setNotice(null);
    setNewPassword("");
    setResetting(user);
  }

  function resetPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!resetting) return;
    const user = resetting;
    void run(async () => {
      await api.put<void>(`/admin/users/${user.id}/password`, { password: newPassword });
      setResetting(null);
      setNewPassword("");
      setNotice(t("users.passwordReset", { username: user.username }));
    });
  }

  const message = (code: string) =>
    t(`users.errors.${code}`, { defaultValue: t(`errors.${code}`, { defaultValue: t("errors.unknown") }) });

  return (
    <main>
      <h2>{t("users.title")}</h2>
      {error && <p role="alert">{message(error)}</p>}
      {notice && <p role="status">{notice}</p>}

      <form onSubmit={create}>
        <h3>{t("users.newUser")}</h3>
        <label htmlFor="users-new-username">{t("users.username")}</label>
        <input
          id="users-new-username"
          name="username"
          autoComplete="off"
          required
          value={username}
          onChange={(event) => setUsername(event.target.value)}
        />
        <label htmlFor="users-new-password">{t("users.initialPassword")}</label>
        <input
          id="users-new-password"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <label htmlFor="users-new-role">{t("users.role")}</label>
        <select id="users-new-role" value={role} onChange={(event) => setRole(event.target.value as Role)}>
          <option value="user">{t("users.roles.user")}</option>
          <option value="admin">{t("users.roles.admin")}</option>
        </select>
        <button type="submit" disabled={busy}>
          {t("users.create")}
        </button>
      </form>

      <table>
        <thead>
          <tr>
            <th scope="col">{t("users.username")}</th>
            <th scope="col">{t("users.role")}</th>
            <th scope="col">{t("users.status")}</th>
            <th scope="col">{t("users.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {users.map((user) => (
            <tr key={user.id}>
              <td>{user.username}</td>
              <td>{t(`users.roles.${user.role}`)}</td>
              <td>{user.is_active ? t("users.active") : t("users.inactive")}</td>
              <td>
                <button type="button" onClick={() => setActive(user, !user.is_active)}>
                  {user.is_active ? t("users.deactivate") : t("users.reactivate")}
                </button>
                <button type="button" onClick={() => openReset(user)}>
                  {t("users.resetPassword")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {resetting && (
        <form onSubmit={resetPassword}>
          <h3>{t("users.resetTitle", { username: resetting.username })}</h3>
          <label htmlFor="users-reset-password">{t("users.newPassword")}</label>
          <input
            id="users-reset-password"
            name="new-password"
            type="password"
            autoComplete="new-password"
            required
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
          <button type="submit" disabled={busy}>
            {t("users.savePassword")}
          </button>
          <button type="button" onClick={() => setResetting(null)}>
            {t("users.cancel")}
          </button>
        </form>
      )}
    </main>
  );
}
