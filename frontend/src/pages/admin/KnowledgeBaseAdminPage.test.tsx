// SPEC-2026-11-06-fase-1-fondazioni
// The Knowledge Base page of the admin (create, rename, delete with confirmation, assign and remove users) and the
// list of the own KBs every signed-in user sees (REQ-012): AC-020, AC-021, AC-022, AC-023, AC-024, AC-031.
import { fireEvent, render, screen, within } from "@testing-library/react";

import { App } from "../../app/App";

type Route = (init: RequestInit | undefined) => Response;
type UserOut = { id: string; username: string; role: "admin" | "user"; is_active: boolean; language: null };
type KbOut = { id: string; name: string; description: string | null; created_at: string };

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
const mario: UserOut = { ...anna, id: "8a1c7f9e-0000-4000-8000-0000000000b2", username: "mario", role: "user" };
const luigi: UserOut = { ...mario, id: "8a1c7f9e-0000-4000-8000-0000000000c3", username: "luigi" };

const manuali: KbOut = {
  id: "5b2d8e10-0000-4000-8000-0000000000a1",
  name: "Manuali",
  description: "Manuali tecnici",
  created_at: "2026-11-07T09:30:00Z",
};
const procedure: KbOut = {
  id: "5b2d8e10-0000-4000-8000-0000000000b2",
  name: "Procedure",
  description: null,
  created_at: "2026-11-07T09:40:00Z",
};

/** Mock only the network boundary: every call is recorded, unknown routes fail the request. */
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

const keys = (calls: { key: string }[]) => calls.map((call) => call.key);

/** The admin anna is signed in and the backend holds the given KBs. */
function adminRoutes(kbs: KbOut[], extra: Record<string, Route> = {}) {
  return mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/kbs": () => json(200, kbs),
    ...extra,
  });
}

async function openAdminPage(heading = "Knowledge Base") {
  window.history.pushState({}, "", "/admin/kbs");
  render(<App />);
  return screen.findByRole("heading", { name: heading, level: 2 });
}

async function rowOf(name: string): Promise<HTMLElement> {
  const cell = await screen.findByRole("cell", { name });
  const row = cell.closest("tr");
  if (!row) throw new Error(`no row for ${name}`);
  return row;
}

beforeEach(() => {
  window.history.pushState({}, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// AC-031 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-06-fase-1-fondazioni/AC-031: a user sees neither the Users nor the Knowledge Base admin menu", async () => {
  browserLanguages(["it-IT"]);
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Manuali" })).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Utenti" })).not.toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Knowledge Base" })).not.toBeInTheDocument();
  expect(keys(calls).filter((key) => key.includes("/api/admin/"))).toEqual([]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-031: a user who opens the Knowledge Base admin page gets the own list instead", async () => {
  browserLanguages(["en-US"]);
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  window.history.pushState({}, "", "/admin/kbs");
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Your knowledge bases" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Knowledge Base" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Create knowledge base" })).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/");
  expect(keys(calls).filter((key) => key !== "GET /api/auth/me" && key !== "GET /api/kbs")).toEqual([]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-031: the admin sees both admin menus and opens the Knowledge Base page", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([manuali, procedure]);
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByRole("link", { name: "Utenti" })).toBeInTheDocument();
  fireEvent.click(within(banner).getByRole("link", { name: "Knowledge Base" }));

  expect(await screen.findByRole("heading", { name: "Knowledge Base", level: 2 })).toBeInTheDocument();
  expect(within(await rowOf("Manuali")).getByText("Manuali tecnici")).toBeInTheDocument();
  expect(await rowOf("Procedure")).toBeInTheDocument();
  expect(window.location.pathname).toBe("/admin/kbs");
});

// AC-023, AC-024: the own list and the opened KB --------------------------------------------------------------------

it("SPEC-2026-11-06-fase-1-fondazioni/AC-023: a signed-in user sees the list of the KBs the API returns, with their descriptions", async () => {
  browserLanguages(["it-IT"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali, procedure]),
  });
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Le tue knowledge base" })).toBeInTheDocument();
  const list = await screen.findByRole("list", { name: "Le tue knowledge base" });
  const items = within(list).getAllByRole("listitem");
  expect(items.map((item) => within(item).getByRole("link").textContent)).toEqual(["Manuali", "Procedure"]);
  expect(within(items[0]).getByText("Manuali tecnici")).toBeInTheDocument();
  expect(within(items[0]).getByRole("link")).toHaveAttribute("href", `/kbs/${manuali.id}`);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-023: a user with no KB is told that none is assigned", async () => {
  browserLanguages(["en-US"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, []),
  });
  render(<App />);

  expect(await screen.findByText("No knowledge base is assigned to you.")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Manuali" })).not.toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-024: the user opens an assigned KB from the list", async () => {
  browserLanguages(["it-IT"]);
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
    [`GET /api/kbs/${manuali.id}`]: () => json(200, manuali),
  });
  render(<App />);

  fireEvent.click(await screen.findByRole("link", { name: "Manuali" }));

  expect(await screen.findByRole("heading", { name: "Manuali", level: 2 })).toBeInTheDocument();
  expect(screen.getByText("Manuali tecnici")).toBeInTheDocument();
  expect(window.location.pathname).toBe(`/kbs/${manuali.id}`);
  expect(keys(calls)).toContain(`GET /api/kbs/${manuali.id}`);
  fireEvent.click(screen.getByRole("link", { name: "Torna all'elenco" }));
  expect(await screen.findByRole("heading", { name: "Le tue knowledge base" })).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-024: a KB that is not assigned or does not exist shows the same not-found message", async () => {
  browserLanguages(["it-IT"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    [`GET /api/kbs/${procedure.id}`]: apiError(404, "kb_not_found"),
  });
  window.history.pushState({}, "", `/kbs/${procedure.id}`);
  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("Knowledge base non trovata.");
  expect(screen.queryByRole("heading", { name: "Procedure" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Torna all'elenco" })).toHaveAttribute("href", "/");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-024: in English the not-found message is in English", async () => {
  browserLanguages(["en-GB"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs/not-a-uuid": apiError(404, "kb_not_found"),
  });
  window.history.pushState({}, "", "/kbs/not-a-uuid");
  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("Knowledge base not found.");
});

// AC-020: create and rename, the name already used -------------------------------------------------------------------

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: the admin creates a KB with name and description", async () => {
  browserLanguages(["it-IT"]);
  const created: KbOut = {
    ...procedure,
    id: "5b2d8e10-0000-4000-8000-0000000000c3",
    name: "Ricette",
    description: "Cucina",
  };
  const calls = adminRoutes([manuali], { "POST /api/kbs": () => json(201, created) });
  await openAdminPage();

  fireEvent.change(screen.getByLabelText("Nome"), { target: { value: "Ricette" } });
  fireEvent.change(screen.getByLabelText("Descrizione"), { target: { value: "Cucina" } });
  fireEvent.click(screen.getByRole("button", { name: "Crea knowledge base" }));

  expect(within(await rowOf("Ricette")).getByText("Cucina")).toBeInTheDocument();
  expect(await screen.findByRole("status")).toHaveTextContent("Knowledge base Ricette creata.");
  expect(calls.find((call) => call.key === "POST /api/kbs")?.body).toEqual({ name: "Ricette", description: "Cucina" });
  expect(screen.getByLabelText("Nome")).toHaveValue("");
  expect(screen.getByLabelText("Descrizione")).toHaveValue("");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: an empty description is sent as no description", async () => {
  browserLanguages(["en-US"]);
  const created: KbOut = { ...procedure, id: "5b2d8e10-0000-4000-8000-0000000000c3", name: "Ricette" };
  const calls = adminRoutes([], { "POST /api/kbs": () => json(201, created) });
  await openAdminPage();

  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Ricette" } });
  fireEvent.click(screen.getByRole("button", { name: "Create knowledge base" }));

  expect(await rowOf("Ricette")).toBeInTheDocument();
  expect(calls.find((call) => call.key === "POST /api/kbs")?.body).toEqual({ name: "Ricette", description: null });
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: the name and description fields follow the limits of the spec", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([]);
  await openAdminPage();

  expect(screen.getByLabelText("Nome")).toHaveAttribute("maxLength", "100");
  expect(screen.getByLabelText("Nome")).toBeRequired();
  expect(screen.getByLabelText("Descrizione")).toHaveAttribute("maxLength", "500");
  expect(screen.getByLabelText("Descrizione")).not.toBeRequired();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: a name already used shows the 409 error in Italian and adds nothing", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([manuali], { "POST /api/kbs": apiError(409, "kb_name_taken") });
  await openAdminPage();

  fireEvent.change(screen.getByLabelText("Nome"), { target: { value: "manuali" } });
  fireEvent.click(screen.getByRole("button", { name: "Crea knowledge base" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Esiste già una knowledge base con questo nome.");
  expect(screen.getAllByRole("cell", { name: /^manuali$/i })).toHaveLength(1);
  expect(screen.getByLabelText("Nome")).toHaveValue("manuali");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: in English the 409 error is shown in English", async () => {
  browserLanguages(["en-US"]);
  adminRoutes([manuali], { "POST /api/kbs": apiError(409, "kb_name_taken") });
  await openAdminPage();

  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "MANUALI" } });
  fireEvent.click(screen.getByRole("button", { name: "Create knowledge base" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("A knowledge base with this name already exists.");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: a name or description outside the limits shows the rule", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([], { "POST /api/kbs": apiError(422, "validation_error") });
  await openAdminPage();

  fireEvent.change(screen.getByLabelText("Nome"), { target: { value: "   " } });
  fireEvent.click(screen.getByRole("button", { name: "Crea knowledge base" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Il nome deve avere da 1 a 100 caratteri e la descrizione al massimo 500.",
  );
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: the admin renames a KB and changes its description", async () => {
  browserLanguages(["it-IT"]);
  const calls = adminRoutes([manuali, procedure], {
    [`PATCH /api/kbs/${procedure.id}`]: (init) => json(200, { ...procedure, ...JSON.parse(String(init?.body)) }),
  });
  await openAdminPage();

  fireEvent.click(within(await rowOf("Procedure")).getByRole("button", { name: "Rinomina" }));
  expect(screen.getByRole("heading", { name: "Rinomina Procedure" })).toBeInTheDocument();
  expect(screen.getByLabelText("Nuovo nome")).toHaveValue("Procedure");
  fireEvent.change(screen.getByLabelText("Nuovo nome"), { target: { value: "Procedure interne" } });
  fireEvent.change(screen.getByLabelText("Nuova descrizione"), { target: { value: "Solo per il personale" } });
  fireEvent.click(screen.getByRole("button", { name: "Salva" }));

  const row = await rowOf("Procedure interne");
  expect(within(row).getByText("Solo per il personale")).toBeInTheDocument();
  expect(screen.queryByRole("cell", { name: "Procedure" })).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Nuovo nome")).not.toBeInTheDocument();
  expect(calls.find((call) => call.key === `PATCH /api/kbs/${procedure.id}`)?.body).toEqual({
    name: "Procedure interne",
    description: "Solo per il personale",
  });
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: renaming to a name already used shows the error and keeps the old name", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([manuali, procedure], { [`PATCH /api/kbs/${procedure.id}`]: apiError(409, "kb_name_taken") });
  await openAdminPage();

  fireEvent.click(within(await rowOf("Procedure")).getByRole("button", { name: "Rinomina" }));
  fireEvent.change(screen.getByLabelText("Nuovo nome"), { target: { value: "manuali" } });
  fireEvent.click(screen.getByRole("button", { name: "Salva" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Esiste già una knowledge base con questo nome.");
  expect(await rowOf("Procedure")).toBeInTheDocument();
  expect(screen.getByLabelText("Nuovo nome")).toHaveValue("manuali");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-020: the rename form can be closed without sending anything", async () => {
  browserLanguages(["en-US"]);
  const calls = adminRoutes([manuali]);
  await openAdminPage();

  fireEvent.click(within(await rowOf("Manuali")).getByRole("button", { name: "Rename" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(screen.queryByLabelText("New name")).not.toBeInTheDocument();
  expect(keys(calls).filter((key) => key.startsWith("PATCH"))).toEqual([]);
});

// AC-021: deletion with confirmation ---------------------------------------------------------------------------------

it("SPEC-2026-11-06-fase-1-fondazioni/AC-021: the admin confirms the deletion and the KB leaves the list", async () => {
  browserLanguages(["it-IT"]);
  const calls = adminRoutes([manuali, procedure], { [`DELETE /api/kbs/${manuali.id}`]: noContent });
  await openAdminPage();

  fireEvent.click(within(await rowOf("Manuali")).getByRole("button", { name: "Elimina" }));

  const dialog = await screen.findByRole("dialog", {
    name: "Eliminare la knowledge base Manuali? Anche le assegnazioni dei suoi utenti verranno rimosse.",
  });
  expect(keys(calls)).not.toContain(`DELETE /api/kbs/${manuali.id}`);
  fireEvent.click(within(dialog).getByRole("button", { name: "Elimina" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Knowledge base Manuali eliminata.");
  expect(screen.queryByRole("cell", { name: "Manuali" })).not.toBeInTheDocument();
  expect(await rowOf("Procedure")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(keys(calls).filter((key) => key.startsWith("DELETE"))).toEqual([`DELETE /api/kbs/${manuali.id}`]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-021: cancelling the confirmation deletes nothing", async () => {
  browserLanguages(["en-US"]);
  const calls = adminRoutes([manuali]);
  await openAdminPage();

  fireEvent.click(within(await rowOf("Manuali")).getByRole("button", { name: "Delete" }));
  const dialog = await screen.findByRole("dialog", {
    name: "Delete the knowledge base Manuali? The assignments of its users will be removed too.",
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await rowOf("Manuali")).toBeInTheDocument();
  expect(keys(calls).filter((key) => key.startsWith("DELETE"))).toEqual([]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-021: a KB deleted meanwhile shows the not-found message", async () => {
  browserLanguages(["it-IT"]);
  adminRoutes([manuali], { [`DELETE /api/kbs/${manuali.id}`]: apiError(404, "kb_not_found") });
  await openAdminPage();

  fireEvent.click(within(await rowOf("Manuali")).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Elimina" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Knowledge base non trovata.");
});

// AC-022: assign and remove users ------------------------------------------------------------------------------------

function memberRoutes(members: UserOut[]) {
  const state = { members: [...members] };
  const routes: Record<string, Route> = {
    "GET /api/admin/users": () => json(200, [anna, mario, luigi]),
    [`GET /api/kbs/${manuali.id}/members`]: () => json(200, state.members),
  };
  for (const user of [anna, mario, luigi]) {
    routes[`PUT /api/kbs/${manuali.id}/members/${user.id}`] = () => {
      if (!state.members.some((member) => member.id === user.id)) state.members.push(user);
      return noContent();
    };
    routes[`DELETE /api/kbs/${manuali.id}/members/${user.id}`] = () => {
      state.members = state.members.filter((member) => member.id !== user.id);
      return noContent();
    };
  }
  return { state, routes };
}

async function openMembers(labels = { members: "Utenti assegnati", title: "Utenti assegnati a Manuali" }) {
  fireEvent.click(within(await rowOf("Manuali")).getByRole("button", { name: labels.members }));
  return screen.findByRole("list", { name: labels.title });
}

it("SPEC-2026-11-06-fase-1-fondazioni/AC-022: the admin sees the assigned users and assigns another one", async () => {
  browserLanguages(["it-IT"]);
  const { state, routes } = memberRoutes([mario]);
  const calls = adminRoutes([manuali], routes);
  await openAdminPage();

  const members = await openMembers();
  expect(
    within(members)
      .getAllByRole("listitem")
      .map((item) => item.firstChild?.textContent),
  ).toEqual(["mario"]);
  const picker = screen.getByLabelText("Utente da assegnare");
  const offered = () =>
    within(picker)
      .getAllByRole("option")
      .map((option) => option.textContent);
  expect(offered()).toEqual(["Scegli un utente", "anna", "luigi"]);

  fireEvent.change(picker, { target: { value: luigi.id } });
  fireEvent.click(screen.getByRole("button", { name: "Assegna" }));

  expect(await within(members).findByText("luigi")).toBeInTheDocument();
  expect(keys(calls)).toContain(`PUT /api/kbs/${manuali.id}/members/${luigi.id}`);
  expect(state.members.map((member) => member.username)).toEqual(["mario", "luigi"]);
  expect(offered()).toEqual(["Scegli un utente", "anna"]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-022: an already assigned user is not offered again and nothing is assigned twice", async () => {
  browserLanguages(["it-IT"]);
  const { routes } = memberRoutes([mario, luigi, anna]);
  const calls = adminRoutes([manuali], routes);
  await openAdminPage();

  await openMembers();

  const picker = screen.getByLabelText("Utente da assegnare");
  expect(
    within(picker)
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual(["Scegli un utente"]);
  expect(screen.getByRole("button", { name: "Assegna" })).toBeDisabled();
  expect(keys(calls).filter((key) => key.startsWith("PUT"))).toEqual([]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-022: the admin removes a user from the KB", async () => {
  browserLanguages(["en-US"]);
  const { state, routes } = memberRoutes([mario, luigi]);
  const calls = adminRoutes([manuali], routes);
  await openAdminPage();

  const members = await openMembers({ members: "Assigned users", title: "Users assigned to Manuali" });
  const marioItem = within(members).getByText("mario").closest("li");
  if (!marioItem) throw new Error("no item for mario");
  fireEvent.click(within(marioItem).getByRole("button", { name: "Remove" }));

  expect(await within(members).findByText("luigi")).toBeInTheDocument();
  await vi.waitFor(() => expect(within(members).queryByText("mario")).not.toBeInTheDocument());
  expect(keys(calls)).toContain(`DELETE /api/kbs/${manuali.id}/members/${mario.id}`);
  expect(state.members.map((member) => member.username)).toEqual(["luigi"]);
  expect(within(screen.getByLabelText("User to assign")).getByRole("option", { name: "mario" })).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-022: a KB without users says so", async () => {
  browserLanguages(["it-IT"]);
  const { routes } = memberRoutes([]);
  adminRoutes([manuali], routes);
  await openAdminPage();

  fireEvent.click(within(await rowOf("Manuali")).getByRole("button", { name: "Utenti assegnati" }));

  expect(await screen.findByText("Nessun utente assegnato.")).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-022: an assignment error is shown in the UI language", async () => {
  browserLanguages(["it-IT"]);
  const { routes } = memberRoutes([]);
  routes[`PUT /api/kbs/${manuali.id}/members/${luigi.id}`] = apiError(404, "user_not_found");
  adminRoutes([manuali], routes);
  await openAdminPage();

  await openMembers();
  fireEvent.change(screen.getByLabelText("Utente da assegnare"), { target: { value: luigi.id } });
  fireEvent.click(screen.getByRole("button", { name: "Assegna" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Utente non trovato.");
  expect(screen.queryByText("luigi", { selector: "li *, li" })).not.toBeInTheDocument();
});
