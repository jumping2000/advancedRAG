import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError } from "../../api/client";
import type { User } from "../../auth/AuthContext";
import type { KnowledgeBase } from "../KnowledgeBasesPage";

const NAME_MAX_LENGTH = 100;
const DESCRIPTION_MAX_LENGTH = 500;

function codeOf(caught: unknown): string {
  return caught instanceof ApiError ? caught.code : "unknown";
}

/** An empty description field means no description. */
function descriptionOf(text: string): string | null {
  return text === "" ? null : text;
}

/** Knowledge Base page of the admin (REQ-008, REQ-009, REQ-012): create, rename, delete after a confirmation, assign
 * and remove users. The admin sees every KB. */
export function KnowledgeBaseAdminPage() {
  const { t } = useTranslation();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [renaming, setRenaming] = useState<KnowledgeBase | null>(null);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [confirming, setConfirming] = useState<KnowledgeBase | null>(null);
  const [membersOf, setMembersOf] = useState<KnowledgeBase | null>(null);
  const [members, setMembers] = useState<User[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [chosen, setChosen] = useState("");

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

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(async () => {
      const created = await api.post<KnowledgeBase>("/kbs", { name, description: descriptionOf(description) });
      setKbs((list) => [...list, created]);
      setName("");
      setDescription("");
      setNotice(t("kbAdmin.created", { name: created.name }));
    });
  }

  function openRename(kb: KnowledgeBase) {
    setError(null);
    setNotice(null);
    setNewName(kb.name);
    setNewDescription(kb.description ?? "");
    setRenaming(kb);
  }

  function rename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!renaming) return;
    const kb = renaming;
    void run(async () => {
      const updated = await api.patch<KnowledgeBase>(`/kbs/${kb.id}`, {
        name: newName,
        description: descriptionOf(newDescription),
      });
      setKbs((list) => list.map((item) => (item.id === updated.id ? updated : item)));
      setMembersOf((open) => (open?.id === updated.id ? updated : open));
      setRenaming(null);
      setNotice(t("kbAdmin.renamed", { name: updated.name }));
    });
  }

  function confirmDeletion() {
    if (!confirming) return;
    const kb = confirming;
    setConfirming(null);
    void run(async () => {
      await api.delete<void>(`/kbs/${kb.id}`);
      setKbs((list) => list.filter((item) => item.id !== kb.id));
      setRenaming((open) => (open?.id === kb.id ? null : open));
      setMembersOf((open) => (open?.id === kb.id ? null : open));
      setNotice(t("kbAdmin.deleted", { name: kb.name }));
    });
  }

  function openMembers(kb: KnowledgeBase) {
    void run(async () => {
      const [assigned, all] = await Promise.all([
        api.get<User[]>(`/kbs/${kb.id}/members`),
        api.get<User[]>("/admin/users"),
      ]);
      setMembers(assigned);
      setUsers(all);
      setChosen("");
      setMembersOf(kb);
    });
  }

  function assign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!membersOf || chosen === "") return;
    const kb = membersOf;
    const userId = chosen;
    void run(async () => {
      await api.put<void>(`/kbs/${kb.id}/members/${userId}`);
      setMembers(await api.get<User[]>(`/kbs/${kb.id}/members`));
      setChosen("");
    });
  }

  function remove(user: User) {
    if (!membersOf) return;
    const kb = membersOf;
    void run(async () => {
      await api.delete<void>(`/kbs/${kb.id}/members/${user.id}`);
      setMembers((list) => list.filter((member) => member.id !== user.id));
    });
  }

  const message = (code: string) =>
    t(`kbAdmin.errors.${code}`, { defaultValue: t(`errors.${code}`, { defaultValue: t("errors.unknown") }) });
  const available = users.filter((user) => !members.some((member) => member.id === user.id));

  return (
    <main>
      <h2>{t("kbAdmin.title")}</h2>
      {error && <p role="alert">{message(error)}</p>}
      {notice && <p role="status">{notice}</p>}

      <form onSubmit={create}>
        <h3>{t("kbAdmin.newKb")}</h3>
        <label htmlFor="kb-new-name">{t("kbAdmin.name")}</label>
        <input
          id="kb-new-name"
          name="name"
          autoComplete="off"
          required
          maxLength={NAME_MAX_LENGTH}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <label htmlFor="kb-new-description">{t("kbAdmin.description")}</label>
        <textarea
          id="kb-new-description"
          name="description"
          maxLength={DESCRIPTION_MAX_LENGTH}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
        <button type="submit" disabled={busy}>
          {t("kbAdmin.create")}
        </button>
      </form>

      <table>
        <thead>
          <tr>
            <th scope="col">{t("kbAdmin.name")}</th>
            <th scope="col">{t("kbAdmin.description")}</th>
            <th scope="col">{t("kbAdmin.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {kbs.map((kb) => (
            <tr key={kb.id}>
              <td>{kb.name}</td>
              <td>{kb.description}</td>
              <td>
                <button type="button" onClick={() => openRename(kb)}>
                  {t("kbAdmin.rename")}
                </button>
                <button type="button" onClick={() => openMembers(kb)}>
                  {t("kbAdmin.members")}
                </button>
                <button type="button" onClick={() => setConfirming(kb)}>
                  {t("kbAdmin.delete")}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {confirming && (
        <div role="dialog" aria-modal="true" aria-labelledby="kb-confirm-text">
          <p id="kb-confirm-text">{t("kbAdmin.confirmDelete", { name: confirming.name })}</p>
          <button type="button" onClick={confirmDeletion}>
            {t("kbAdmin.delete")}
          </button>
          <button type="button" onClick={() => setConfirming(null)}>
            {t("kbAdmin.cancel")}
          </button>
        </div>
      )}

      {renaming && (
        <form onSubmit={rename}>
          <h3>{t("kbAdmin.renameTitle", { name: renaming.name })}</h3>
          <label htmlFor="kb-rename-name">{t("kbAdmin.newName")}</label>
          <input
            id="kb-rename-name"
            name="new-name"
            autoComplete="off"
            required
            maxLength={NAME_MAX_LENGTH}
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          <label htmlFor="kb-rename-description">{t("kbAdmin.newDescription")}</label>
          <textarea
            id="kb-rename-description"
            name="new-description"
            maxLength={DESCRIPTION_MAX_LENGTH}
            value={newDescription}
            onChange={(event) => setNewDescription(event.target.value)}
          />
          <button type="submit" disabled={busy}>
            {t("kbAdmin.save")}
          </button>
          <button type="button" onClick={() => setRenaming(null)}>
            {t("kbAdmin.cancel")}
          </button>
        </form>
      )}

      {membersOf && (
        <section>
          <h3 id="kb-members-title">{t("kbAdmin.membersTitle", { name: membersOf.name })}</h3>
          {members.length === 0 && <p>{t("kbAdmin.noMembers")}</p>}
          <ul aria-labelledby="kb-members-title">
            {members.map((member) => (
              <li key={member.id}>
                <span>{member.username}</span>
                <button type="button" disabled={busy} onClick={() => remove(member)}>
                  {t("kbAdmin.remove")}
                </button>
              </li>
            ))}
          </ul>
          <form onSubmit={assign}>
            <label htmlFor="kb-member-user">{t("kbAdmin.userToAssign")}</label>
            <select id="kb-member-user" value={chosen} onChange={(event) => setChosen(event.target.value)}>
              <option value="">{t("kbAdmin.chooseUser")}</option>
              {available.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.username}
                </option>
              ))}
            </select>
            <button type="submit" disabled={busy || chosen === ""}>
              {t("kbAdmin.assign")}
            </button>
          </form>
        </section>
      )}
    </main>
  );
}
