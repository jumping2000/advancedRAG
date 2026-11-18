// SPEC-2026-11-17-fase-3-chat
// The Conversations page of the admin (REQ-020): the conversations of everyone with title, user, KB, date and number of
// messages; filters by KB and by user; a conversation opens read-only with its messages and sources; «Elimina» after a
// confirmation; and the page and its menu stay away from a `user`: AC-042, AC-043.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../../app/App";
import type { ConversationOut, MessageOut, SourceExcerpt, SourceOut } from "../../components/ConversationView";
import { i18n } from "../../i18n";

type UserOut = { id: string; username: string; role: "admin" | "user"; is_active: boolean; language: null };
type KbOut = { id: string; name: string; description: string | null; created_at: string };
type AdminConversation = ConversationOut & { username: string; kb_name: string; message_count: number };
type Detail = ConversationOut & { messages: MessageOut[]; read_only: boolean };
type Route = (init: RequestInit | undefined, url: URL) => Response | Promise<Response>;
type Call = { key: string; url: URL; init: RequestInit | undefined };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
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
const lucia: UserOut = { ...anna, id: "8a1c7f9e-0000-4000-8000-0000000000c3", username: "lucia", role: "user" };

const manuali: KbOut = {
  id: "5b2d8e10-0000-4000-8000-0000000000a1",
  name: "Manuali",
  description: null,
  created_at: "2026-11-17T09:30:00Z",
};
const procedure: KbOut = { ...manuali, id: "5b2d8e10-0000-4000-8000-0000000000b2", name: "Procedure" };

const pad = (n: number) => String(n).padStart(12, "0");
function conv(n: number, title: string, owner: UserOut, kb: KbOut, messageCount: number): AdminConversation {
  return {
    id: `c0000000-0000-4000-8000-${pad(n)}`,
    kb_id: kb.id,
    title,
    created_at: "2026-11-17T10:00:00Z",
    updated_at: "2026-11-17T10:30:00Z",
    username: owner.username,
    kb_name: kb.name,
    message_count: messageCount,
  };
}
function msg(n: number, role: "user" | "assistant", content: string, extra: Partial<MessageOut> = {}): MessageOut {
  return {
    id: `a0000000-0000-4000-8000-${pad(n)}`,
    role,
    content,
    status: "COMPLETED",
    error_code: null,
    notice: null,
    created_at: "2026-11-17T10:00:00Z",
    sources: [],
    ...extra,
  };
}
const detail = (c: AdminConversation, messages: MessageOut[]): Detail => ({
  id: c.id,
  kb_id: c.kb_id,
  title: c.title,
  created_at: c.created_at,
  updated_at: c.updated_at,
  messages,
  read_only: true,
});

const prezzi = conv(1, "Prezzi del listino", mario, manuali, 2);
const garanzia = conv(2, "Garanzia del prodotto", lucia, procedure, 4);
const ferie = conv(3, "Regole sulle ferie", mario, procedure, 2);

const listino: SourceOut = {
  number: 1,
  document_name: "listino.pdf",
  section_title: "Prezzi",
  page_start: 3,
  page_end: 3,
};
const prezziQuestion = msg(1, "user", "Quanto costa il prodotto?");
const prezziAnswer = msg(2, "assistant", "Il prezzo comprende IVA al 22% [1].", { sources: [listino] });

const LIST = "GET /api/admin/conversations";
const getOf = (c: ConversationOut) => `GET /api/conversations/${c.id}`;
const deleteOf = (c: ConversationOut) => `DELETE /api/conversations/${c.id}`;
const sourceOf = (c: ConversationOut, m: MessageOut, n: number) =>
  `GET /api/conversations/${c.id}/messages/${m.id}/sources/${n}`;

/** Mock only the network boundary: every call is recorded, unknown routes fail the request. */
function mockApi(routes: Record<string, Route>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const url = new URL(raw, "http://localhost");
      const key = `${init?.method ?? "GET"} ${url.pathname}`;
      calls.push({ key, url, init });
      const route = routes[key];
      if (!route) throw new Error(`unexpected request ${key}`);
      return route(init, url);
    }),
  );
  return calls;
}

const keys = (calls: Call[]) => calls.map((call) => call.key);
const callsOf = (calls: Call[], key: string) => calls.filter((call) => call.key === key);
const count = (calls: Call[], key: string) => callsOf(calls, key).length;
const queryOf = (call: Call) => Object.fromEntries(call.url.searchParams.entries());
const lastQuery = (calls: Call[]) => queryOf(callsOf(calls, LIST).at(-1) as Call);

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

/** The admin anna is signed in; `server.conversations` is what the backend holds (the list route applies the filters
 * as the backend does: an empty or missing parameter is no filter). */
function adminRoutes(server: { conversations: AdminConversation[] }, extra: Record<string, Route> = {}) {
  return mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/kbs": () => json(200, [manuali, procedure]),
    "GET /api/admin/users": () => json(200, [anna, mario, lucia]),
    [LIST]: (_init, url) => {
      const kb = url.searchParams.get("kb_id");
      const owner = url.searchParams.get("user_id");
      const users = [anna, mario, lucia];
      return json(
        200,
        server.conversations.filter(
          (c) => (!kb || c.kb_id === kb) && (!owner || users.find((u) => u.id === owner)?.username === c.username),
        ),
      );
    },
    ...extra,
  });
}

async function openPage(heading = "Conversazioni") {
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);
  await screen.findByRole("heading", { name: heading, level: 2 });
}

const dataRows = () => screen.queryAllByRole("row").slice(1);
const titles = () => dataRows().map((row) => within(row).getAllByRole("cell")[0].textContent);

async function rowOf(title: string): Promise<HTMLElement> {
  const cell = await screen.findByRole("cell", { name: title });
  const row = cell.closest("tr");
  if (!row) throw new Error(`no row for ${title}`);
  return row;
}

async function filterBy(label: string, option: string, value: string) {
  const select = await screen.findByLabelText(label);
  await screen.findByRole("option", { name: option });
  fireEvent.change(select, { target: { value } });
}

const noKeysOnScreen = () => expect(document.body.textContent).not.toMatch(/\b(convAdmin|chat|errors)\.[A-Za-z_]+/);

beforeEach(() => {
  window.history.pushState({}, "", "/");
  browserLanguages(["it-IT"]);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  void i18n.changeLanguage("it");
});

// AC-042 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-17-fase-3-chat/AC-042: the admin sees the conversations of everyone with title, user, KB, date and number of messages", async () => {
  const calls = adminRoutes({ conversations: [prezzi, garanzia] });
  await openPage();

  const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
  expect(headers.slice(0, 5)).toEqual(["Titolo", "Utente", "Knowledge base", "Data", "Messaggi"]);
  expect(await rowOf("Prezzi del listino")).toBeInTheDocument();
  expect(titles()).toEqual(["Prezzi del listino", "Garanzia del prodotto"]); // the order of the backend
  const cells = within(dataRows()[0]).getAllByRole("cell");
  expect(cells[1]).toHaveTextContent("mario");
  expect(cells[2]).toHaveTextContent("Manuali");
  expect(cells[3]).toHaveTextContent("2026");
  expect(cells[4]).toHaveTextContent("2");
  expect(within(dataRows()[1]).getAllByRole("cell")[1]).toHaveTextContent("lucia");
  expect(within(dataRows()[1]).getAllByRole("cell")[4]).toHaveTextContent("4");
  expect(count(calls, LIST)).toBe(1);
  expect(lastQuery(calls)).toEqual({}); // no filter at first
  noKeysOnScreen();
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: the KB and user filters list the KBs and the users, and each choice asks the backend again", async () => {
  const calls = adminRoutes({ conversations: [prezzi, garanzia, ferie] });
  await openPage();
  expect(await rowOf("Prezzi del listino")).toBeInTheDocument();
  expect(titles()).toHaveLength(3);

  await filterBy("Knowledge base", "Procedure", procedure.id);
  await waitFor(() => expect(titles()).toEqual(["Garanzia del prodotto", "Regole sulle ferie"]));
  expect(lastQuery(calls)).toEqual({ kb_id: procedure.id });

  await filterBy("Utente", "mario", mario.id);
  await waitFor(() => expect(titles()).toEqual(["Regole sulle ferie"]));
  expect(lastQuery(calls)).toEqual({ kb_id: procedure.id, user_id: mario.id });

  await filterBy("Knowledge base", "Tutte", "");
  await waitFor(() => expect(titles()).toEqual(["Prezzi del listino", "Regole sulle ferie"]));
  expect(lastQuery(calls)).toEqual({ user_id: mario.id });

  await filterBy("Utente", "Tutti", "");
  await waitFor(() => expect(titles()).toHaveLength(3));
  expect(lastQuery(calls)).toEqual({});
  expect(count(calls, "GET /api/kbs")).toBe(1);
  expect(count(calls, "GET /api/admin/users")).toBe(1);
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: a filter that matches nothing shows the empty message", async () => {
  adminRoutes({ conversations: [prezzi] });
  await openPage();
  expect(await rowOf("Prezzi del listino")).toBeInTheDocument();

  await filterBy("Utente", "lucia", lucia.id);

  expect(await screen.findByText("Nessuna conversazione.")).toBeInTheDocument();
  expect(dataRows()).toHaveLength(0);
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: an answer to an older filter does not replace the list of the newer one", async () => {
  let release: () => void = () => undefined;
  const slow = new Promise<void>((resolve) => {
    release = resolve;
  });
  const calls = adminRoutes(
    { conversations: [prezzi, garanzia] },
    {
      [LIST]: async (_init, url) => {
        const kb = url.searchParams.get("kb_id");
        if (kb === manuali.id) {
          await slow;
          return json(200, [prezzi]);
        }
        return json(200, [garanzia]);
      },
    },
  );
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);
  await screen.findByRole("heading", { name: "Conversazioni", level: 2 });
  await waitFor(() => expect(count(calls, LIST)).toBe(1));

  await filterBy("Knowledge base", "Manuali", manuali.id); // slow
  await filterBy("Knowledge base", "Procedure", procedure.id); // fast
  expect(await rowOf("Garanzia del prodotto")).toBeInTheDocument();
  release();
  await waitFor(() => expect(count(calls, LIST)).toBe(3));
  await new Promise((resolve) => setTimeout(resolve, 20));

  expect(titles()).toEqual(["Garanzia del prodotto"]);
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: a conversation opens read-only with its messages and sources: no question field and no «Riprova», the excerpt opens", async () => {
  const failedLast = msg(4, "assistant", "", { status: "FAILED", error_code: "generation_timeout" });
  const calls = adminRoutes(
    { conversations: [prezzi, garanzia] },
    {
      [getOf(prezzi)]: () => json(200, detail(prezzi, [prezziQuestion, prezziAnswer])),
      [getOf(garanzia)]: () =>
        json(
          200,
          detail(garanzia, [
            msg(5, "user", "Quanto dura?"),
            msg(6, "assistant", "Due anni."),
            msg(7, "user", "E poi?"),
            failedLast,
          ]),
        ),
      [sourceOf(prezzi, prezziAnswer, 1)]: () =>
        json(200, { ...listino, available: true, content: "Il prezzo comprende IVA al 22%." } satisfies SourceExcerpt),
    },
  );
  await openPage();

  fireEvent.click(await within(await rowOf("Prezzi del listino")).findByRole("button", { name: "Prezzi del listino" }));

  expect(await screen.findByText("Quanto costa il prodotto?")).toBeInTheDocument();
  expect(screen.getByText(/Il prezzo comprende IVA al 22%/)).toBeInTheDocument();
  expect(count(calls, getOf(prezzi))).toBe(1);
  expect(screen.queryByLabelText("Domanda")).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Invia" })).not.toBeInTheDocument();
  const sources = await screen.findByRole("list", { name: "Fonti" });
  expect(sources).toHaveTextContent("listino.pdf");
  expect(sources).toHaveTextContent("p. 3");

  fireEvent.click(screen.getByRole("button", { name: "[1]" }));
  expect(await screen.findByText("Il prezzo comprende IVA al 22%.")).toBeInTheDocument();
  expect(count(calls, sourceOf(prezzi, prezziAnswer, 1))).toBe(1);

  // Another conversation, whose last answer failed: the reason is shown, «Riprova» is not offered
  fireEvent.click(within(await rowOf("Garanzia del prodotto")).getByRole("button", { name: "Garanzia del prodotto" }));
  expect(await screen.findByText("Il provider di generazione non ha finito in tempo.")).toBeInTheDocument();
  expect(screen.getByText("E poi?")).toBeInTheDocument();
  expect(screen.queryByText("Quanto costa il prodotto?")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(keys(calls).filter((key) => /^(POST|PATCH|PUT)/.test(key))).toEqual([]);
  noKeysOnScreen();
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: «Elimina» asks for a confirmation, and «Annulla» leaves the conversation as it is", async () => {
  const calls = adminRoutes({ conversations: [prezzi, garanzia] });
  await openPage();

  fireEvent.click(within(await rowOf("Prezzi del listino")).getByRole("button", { name: "Elimina" }));

  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("Eliminare la conversazione «Prezzi del listino»?");
  fireEvent.click(within(dialog).getByRole("button", { name: "Annulla" }));

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(titles()).toEqual(["Prezzi del listino", "Garanzia del prodotto"]);
  expect(count(calls, deleteOf(prezzi))).toBe(0);
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: confirming deletes the conversation, removes its row and closes it if it was open", async () => {
  const server = { conversations: [prezzi, garanzia] };
  const calls = adminRoutes(server, {
    [getOf(prezzi)]: () => json(200, detail(prezzi, [prezziQuestion, prezziAnswer])),
    [deleteOf(prezzi)]: () => {
      server.conversations = server.conversations.filter((c) => c.id !== prezzi.id);
      return noContent();
    },
  });
  await openPage();
  fireEvent.click(within(await rowOf("Prezzi del listino")).getByRole("button", { name: "Prezzi del listino" }));
  expect(await screen.findByText("Quanto costa il prodotto?")).toBeInTheDocument();

  fireEvent.click(within(await rowOf("Prezzi del listino")).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  await waitFor(() => expect(titles()).toEqual(["Garanzia del prodotto"]));
  expect(count(calls, deleteOf(prezzi))).toBe(1);
  const request = callsOf(calls, deleteOf(prezzi))[0];
  expect(new Headers(request.init?.headers).get("X-Requested-With")).toBe("XMLHttpRequest");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText("Quanto costa il prodotto?")).not.toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "Fonti" })).not.toBeInTheDocument();
  expect(count(calls, deleteOf(garanzia))).toBe(0);
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: a conversation that is already gone shows its translated error, without an alert, and the list is read again", async () => {
  const server = { conversations: [prezzi, garanzia] };
  const calls = adminRoutes(server, {
    [deleteOf(prezzi)]: () => {
      server.conversations = [garanzia]; // someone deleted it first
      return apiError(404, "conversation_not_found")();
    },
  });
  await openPage();

  fireEvent.click(within(await rowOf("Prezzi del listino")).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  expect(await screen.findByText("Conversazione non trovata.")).toBeInTheDocument();
  await waitFor(() => expect(titles()).toEqual(["Garanzia del prodotto"]));
  expect(count(calls, LIST)).toBe(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // the header selector owns the alert
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: a list that cannot be read shows its translated error without an alert", async () => {
  adminRoutes({ conversations: [] }, { [LIST]: apiError(500, "internal_error") });
  await openPage();

  expect(await screen.findByText("Si è verificato un errore. Riprova.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(dataRows()).toHaveLength(0);
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: a conversation that cannot be opened shows its translated error without an alert", async () => {
  adminRoutes({ conversations: [prezzi] }, { [getOf(prezzi)]: apiError(404, "conversation_not_found") });
  await openPage();

  fireEvent.click(within(await rowOf("Prezzi del listino")).getByRole("button", { name: "Prezzi del listino" }));

  expect(await screen.findByText("Conversazione non trovata.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("SPEC-2026-11-17-fase-3-chat/AC-042: the page, its filters and its columns are in English with an English browser", async () => {
  browserLanguages(["en-US"]);
  adminRoutes({ conversations: [prezzi] });
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Conversations", level: 2 })).toBeInTheDocument();
  expect(await screen.findByLabelText("Knowledge base")).toBeInTheDocument();
  expect(await screen.findByLabelText("User")).toBeInTheDocument();
  expect(await screen.findAllByRole("option", { name: "All" })).toHaveLength(2);
  expect(
    screen
      .getAllByRole("columnheader")
      .map((header) => header.textContent)
      .slice(0, 5),
  ).toEqual(["Title", "User", "Knowledge base", "Date", "Messages"]);
  expect(within(await rowOf("Prezzi del listino")).getByRole("button", { name: "Delete" })).toBeInTheDocument();
  noKeysOnScreen();
});

// AC-043 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-17-fase-3-chat/AC-043: a user sees no Conversations menu and the route /admin/conversations takes them to /", async () => {
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Manuali" })).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Conversazioni" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Conversazioni" })).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/");
  expect(keys(calls).filter((key) => key !== "GET /api/auth/me" && key !== "GET /api/kbs")).toEqual([]);
});

it("SPEC-2026-11-17-fase-3-chat/AC-043: a user who has the interface open in English sees no Conversations menu", async () => {
  browserLanguages(["en-US"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Conversations" })).not.toBeInTheDocument();
});

it("SPEC-2026-11-17-fase-3-chat/AC-043: without a session the route goes to the login page", async () => {
  const calls = mockApi({ "GET /api/auth/me": apiError(401, "not_authenticated") });
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Accedi" })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/login");
  expect(keys(calls)).toEqual(["GET /api/auth/me"]);
});

it("SPEC-2026-11-17-fase-3-chat/AC-043: the admin sees the Conversations menu and opens the page from it", async () => {
  adminRoutes({ conversations: [prezzi] });
  render(<App />);

  const banner = await screen.findByRole("banner");
  fireEvent.click(await within(banner).findByRole("link", { name: "Conversazioni" }));

  expect(await screen.findByRole("heading", { name: "Conversazioni", level: 2 })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/admin/conversations");
  expect(await rowOf("Prezzi del listino")).toBeInTheDocument();
});
