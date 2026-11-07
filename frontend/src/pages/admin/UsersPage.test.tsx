// SPEC-2026-11-06-fase-1-fondazioni
import { fireEvent, render, screen, within } from "@testing-library/react";

import { App } from "../../app/App";

type Route = (init: RequestInit | undefined) => Response;
type UserOut = { id: string; username: string; role: "admin" | "user"; is_active: boolean; language: null };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const apiError = (status: number, code: string) => () => json(status, { error: { code, message: "English text" } });
const noContent = () => new Response(null, { status: 204 });

const anna: UserOut = {
  id: "8a1c7f9e-0000-4000-8000-0000000000a1",
  username: "anna",
  role: "admin",
  is_active: true,
  language: null,
};
const mario: UserOut = {
  id: "8a1c7f9e-0000-4000-8000-0000000000b2",
  username: "mario",
  role: "user",
  is_active: true,
  language: null,
};
const luigi: UserOut = { ...mario, id: "8a1c7f9e-0000-4000-8000-0000000000c3", username: "luigi", is_active: false };

/** Mock only the network boundary: every call is recorded, unknown routes fail the test. */
function mockApi(routes: Record<string, Route>) {
  const calls: { key: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const key = `${init?.method ?? "GET"} ${new URL(url, "http://localhost").pathname}`;
    calls.push({ key, body: init?.body === undefined ? undefined : JSON.parse(String(init.body)) });
    const route = routes[key];
    if (!route) throw new Error(`unexpected request ${key}`);
    return route(init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

/** The admin anna is signed in and the backend lists the given users. */
function adminRoutes(users: UserOut[], extra: Record<string, Route> = {}) {
  return mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/admin/users": () => json(200, users),
    ...extra,
  });
}

async function openUsersPage(heading: string) {
  window.history.pushState({}, "", "/admin/users");
  render(<App />);
  return screen.findByRole("heading", { name: heading });
}

async function rowOf(username: string): Promise<HTMLElement> {
  const cell = await screen.findByRole("cell", { name: username });
  const row = cell.closest("tr");
  if (!row) throw new Error(`no row for ${username}`);
  return row;
}

function fillNewUser(
  username: string,
  password: string,
  labels = { username: "Nome utente", password: "Password iniziale" },
) {
  fireEvent.change(screen.getByLabelText(labels.username), { target: { value: username } });
  fireEvent.change(screen.getByLabelText(labels.password), { target: { value: password } });
}

beforeEach(() => {
  window.history.pushState({}, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-015: the admin opens Users from the menu and sees every user without passwords", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([anna, mario, luigi]);
  render(<App />);

  const banner = await screen.findByRole("banner");
  fireEvent.click(await within(banner).findByRole("link", { name: "Utenti" }));

  expect(await screen.findByRole("heading", { name: "Utenti" })).toBeInTheDocument();
  expect(within(await rowOf("mario")).getByText("Attivo")).toBeInTheDocument();
  expect(within(await rowOf("mario")).getByText("Utente")).toBeInTheDocument();
  expect(within(await rowOf("anna")).getByText("Amministratore")).toBeInTheDocument();
  expect(within(await rowOf("luigi")).getByText("Disattivato")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/argon2|password_hash/);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-031: a user sees no Users menu and cannot open the Users page", async () => {
  browserLanguages(["it-IT"]);
  const calls = mockApi({ "GET /api/auth/me": () => json(200, mario) });
  window.history.pushState({}, "", "/admin/users");
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Utenti" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Utenti" })).not.toBeInTheDocument();
  expect(calls.map((call) => call.key)).not.toContain("GET /api/admin/users");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-016: creating a user sends JSON with the role and adds the user to the list", async () => {
  browserLanguages(["it-IT"]);
  const created: UserOut = { ...mario, id: "8a1c7f9e-0000-4000-8000-0000000000d4", username: "peach", role: "admin" };
  const calls = adminRoutes([anna], { "POST /api/admin/users": () => json(201, created) });
  await openUsersPage("Utenti");

  fillNewUser("peach", "initial-password-1");
  fireEvent.change(screen.getByLabelText("Ruolo"), { target: { value: "admin" } });
  fireEvent.click(screen.getByRole("button", { name: "Crea utente" }));

  const row = await rowOf("peach");
  expect(within(row).getByText("Amministratore")).toBeInTheDocument();
  expect(within(row).getByText("Attivo")).toBeInTheDocument();
  const post = calls.find((call) => call.key === "POST /api/admin/users");
  expect(post?.body).toEqual({ username: "peach", password: "initial-password-1", role: "admin" });
  expect(screen.getByLabelText("Nome utente")).toHaveValue("");
  expect(screen.getByLabelText("Password iniziale")).toHaveValue("");
  expect(document.body.textContent).not.toContain("initial-password-1");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-016: the role of a new user is user unless the admin chooses admin", async () => {
  browserLanguages(["en-US"]);
  const calls = adminRoutes([anna], { "POST /api/admin/users": () => json(201, mario) });
  await openUsersPage("Users");

  fillNewUser("mario", "initial-password-1", { username: "Username", password: "Initial password" });
  fireEvent.click(screen.getByRole("button", { name: "Create user" }));

  expect(within(await rowOf("mario")).getByText("User")).toBeInTheDocument();
  expect(calls.find((call) => call.key === "POST /api/admin/users")?.body).toEqual({
    username: "mario",
    password: "initial-password-1",
    role: "user",
  });
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-014: a too short password shows the error in Italian and adds nobody", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([anna], { "POST /api/admin/users": apiError(422, "password_too_short") });
  await openUsersPage("Utenti");

  fillNewUser("mario", "a".repeat(11));
  fireEvent.click(screen.getByRole("button", { name: "Crea utente" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("La password deve avere almeno 12 caratteri.");
  expect(screen.queryByRole("cell", { name: "mario" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Nome utente")).toHaveValue("mario");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-016: a name already used shows the 409 error in the UI language", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([anna, mario], { "POST /api/admin/users": apiError(409, "username_taken") });
  await openUsersPage("Utenti");

  fillNewUser("Mario", "initial-password-1");
  fireEvent.click(screen.getByRole("button", { name: "Crea utente" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Questo nome utente è già usato.");
  expect(screen.getAllByRole("cell", { name: /^mario$/i })).toHaveLength(1);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-016: in English the 409 error is shown in English", async () => {
  browserLanguages(["en-GB"]);
  adminRoutes([anna, mario], { "POST /api/admin/users": apiError(409, "username_taken") });
  await openUsersPage("Users");

  fillNewUser("Mario", "initial-password-1", { username: "Username", password: "Initial password" });
  fireEvent.click(screen.getByRole("button", { name: "Create user" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("This username is already taken.");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-016: an invalid name shows the rule of the names", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([anna], { "POST /api/admin/users": apiError(422, "invalid_username") });
  await openUsersPage("Utenti");

  fillNewUser("màrio", "initial-password-1");
  fireEvent.click(screen.getByRole("button", { name: "Crea utente" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Il nome utente deve avere da 3 a 64 caratteri tra lettere, cifre, punto, trattino basso e trattino.",
  );
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-017: the admin confirms the deactivation, then reactivates without confirmation", async () => {
  browserLanguages(["it-IT"]);
  const calls = adminRoutes([anna, mario], {
    [`PATCH /api/admin/users/${mario.id}`]: (init) =>
      json(200, { ...mario, is_active: (JSON.parse(String(init?.body)) as { is_active: boolean }).is_active }),
  });
  await openUsersPage("Utenti");

  fireEvent.click(within(await rowOf("mario")).getByRole("button", { name: "Disattiva" }));

  const dialog = await screen.findByRole("dialog", {
    name: "Disattivare mario? Tutte le sue sessioni verranno chiuse.",
  });
  expect(calls.map((call) => call.key)).not.toContain(`PATCH /api/admin/users/${mario.id}`);
  fireEvent.click(within(dialog).getByRole("button", { name: "Disattiva" }));

  expect(await within(await rowOf("mario")).findByText("Disattivato")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(within(await rowOf("mario")).getByRole("button", { name: "Riattiva" }));

  expect(await within(await rowOf("mario")).findByText("Attivo")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(within(await rowOf("mario")).getByRole("button", { name: "Disattiva" })).toBeInTheDocument();
  const patches = calls.filter((call) => call.key === `PATCH /api/admin/users/${mario.id}`);
  expect(patches.map((call) => call.body)).toEqual([{ is_active: false }, { is_active: true }]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-017: cancelling the confirmation sends nothing and the user stays active", async () => {
  browserLanguages(["en-US"]);
  const calls = adminRoutes([anna, mario]);
  await openUsersPage("Users");

  fireEvent.click(within(await rowOf("mario")).getByRole("button", { name: "Deactivate" }));
  const dialog = await screen.findByRole("dialog", {
    name: "Deactivate mario? All their sessions will be closed.",
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(within(await rowOf("mario")).getByText("Active")).toBeInTheDocument();
  expect(calls.map((call) => call.key)).not.toContain(`PATCH /api/admin/users/${mario.id}`);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-019: the admin's own row has no Deactivate button", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([anna, mario]);
  await openUsersPage("Utenti");

  const own = await rowOf("anna");
  expect(within(own).getByText("Attivo")).toBeInTheDocument();
  expect(within(own).queryByRole("button", { name: "Disattiva" })).not.toBeInTheDocument();
  expect(within(own).getByRole("button", { name: "Reimposta password" })).toBeInTheDocument();
  expect(within(await rowOf("mario")).getByRole("button", { name: "Disattiva" })).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-018: the admin resets the password of a user", async () => {
  browserLanguages(["it-IT"]);
  const calls = adminRoutes([anna, mario], { [`PUT /api/admin/users/${mario.id}/password`]: noContent });
  await openUsersPage("Utenti");

  fireEvent.click(within(await rowOf("mario")).getByRole("button", { name: "Reimposta password" }));
  expect(screen.getByRole("heading", { name: "Reimposta la password di mario" })).toBeInTheDocument();
  const field = screen.getByLabelText("Nuova password");
  expect(field).toHaveAttribute("type", "password");
  fireEvent.change(field, { target: { value: "brand-new-password-42" } });
  fireEvent.click(screen.getByRole("button", { name: "Salva password" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Password di mario reimpostata.");
  expect(screen.queryByLabelText("Nuova password")).not.toBeInTheDocument();
  expect(calls.find((call) => call.key === `PUT /api/admin/users/${mario.id}/password`)?.body).toEqual({
    password: "brand-new-password-42",
  });
  expect(document.body.textContent).not.toContain("brand-new-password-42");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-014: a too short new password shows the error and keeps the reset form", async () => {
  browserLanguages(["en-US"]);
  adminRoutes([anna, mario], {
    [`PUT /api/admin/users/${mario.id}/password`]: apiError(422, "password_too_short"),
  });
  await openUsersPage("Users");

  fireEvent.click(within(await rowOf("mario")).getByRole("button", { name: "Reset password" }));
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: "b".repeat(11) } });
  fireEvent.click(screen.getByRole("button", { name: "Save password" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("The password must be at least 12 characters long.");
  expect(screen.getByLabelText("New password")).toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-018: the reset form can be closed without sending anything", async () => {
  browserLanguages(["en-US"]);
  const calls = adminRoutes([anna, mario]);
  await openUsersPage("Users");

  fireEvent.click(within(await rowOf("mario")).getByRole("button", { name: "Reset password" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  expect(calls.map((call) => call.key)).not.toContain(`PUT /api/admin/users/${mario.id}/password`);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-018: resetting their own password sends the admin to the login page", async () => {
  browserLanguages(["it-IT"]);
  const calls = adminRoutes([anna, mario], {
    [`PUT /api/admin/users/${anna.id}/password`]: noContent,
    "POST /api/auth/logout": apiError(401, "not_authenticated"),
  });
  await openUsersPage("Utenti");

  fireEvent.click(within(await rowOf("anna")).getByRole("button", { name: "Reimposta password" }));
  fireEvent.change(screen.getByLabelText("Nuova password"), { target: { value: "brand-new-password-42" } });
  fireEvent.click(screen.getByRole("button", { name: "Salva password" }));

  expect(await screen.findByRole("button", { name: "Accedi" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(
    "La tua password è stata reimpostata: accedi di nuovo con la nuova password.",
  );
  expect(screen.queryByRole("heading", { name: "Utenti" })).not.toBeInTheDocument();
  expect(within(screen.getByRole("banner")).queryByText("anna")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(calls.find((call) => call.key === `PUT /api/admin/users/${anna.id}/password`)?.body).toEqual({
    password: "brand-new-password-42",
  });
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-018: in English the sign-in-again message is in English", async () => {
  browserLanguages(["en-US"]);
  adminRoutes([anna], {
    [`PUT /api/admin/users/${anna.id}/password`]: noContent,
    "POST /api/auth/logout": apiError(401, "not_authenticated"),
  });
  await openUsersPage("Users");

  fireEvent.click(within(await rowOf("anna")).getByRole("button", { name: "Reset password" }));
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: "brand-new-password-42" } });
  fireEvent.click(screen.getByRole("button", { name: "Save password" }));

  expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Your password has been reset: sign in again with the new password.",
  );
});
