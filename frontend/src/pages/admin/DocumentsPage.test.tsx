// SPEC-2026-11-07-fase-2-ingestion
// The Documents page of the admin (REQ-020): choose a KB, upload several files (one request each), see the state of
// every document update by itself, retry a FAILED document, delete after a confirmation, and keep the page away from a
// `user`: AC-040, AC-041, AC-042, AC-043, AC-044.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../../app/App";

type DocStatus = "QUEUED" | "PROCESSING" | "READY" | "FAILED" | "UNSUPPORTED";
type DocOut = {
  id: string;
  kb_id: string;
  filename: string;
  size_bytes: number;
  status: DocStatus;
  status_reason: { code: string; message: string } | null;
  chunk_count: number;
  created_at: string;
  updated_at: string;
};
type UserOut = { id: string; username: string; role: "admin" | "user"; is_active: boolean; language: null };
type KbOut = { id: string; name: string; description: string | null; created_at: string };
type Route = (init: RequestInit | undefined) => Response | Promise<Response>;
type Call = { key: string; init: RequestInit | undefined };

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

const manuali: KbOut = {
  id: "5b2d8e10-0000-4000-8000-0000000000a1",
  name: "Manuali",
  description: null,
  created_at: "2026-11-07T09:30:00Z",
};
const procedure: KbOut = { ...manuali, id: "5b2d8e10-0000-4000-8000-0000000000b2", name: "Procedure" };

function doc(n: number, kb: KbOut, filename: string, status: DocStatus, extra: Partial<DocOut> = {}): DocOut {
  return {
    id: `d0c00000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    kb_id: kb.id,
    filename,
    size_bytes: 2048,
    status,
    status_reason: null,
    chunk_count: status === "READY" ? 3 : 0,
    created_at: "2026-11-07T10:00:00Z",
    updated_at: "2026-11-07T10:00:00Z",
    ...extra,
  };
}

const failedWith = (code: string) => ({ status_reason: { code, message: "English text from the backend" } });
const list = (kb: KbOut) => `GET /api/kbs/${kb.id}/documents`;
const upload = (kb: KbOut) => `POST /api/kbs/${kb.id}/documents`;
const retry = (kb: KbOut, d: DocOut) => `POST /api/kbs/${kb.id}/documents/${d.id}/retry`;
const remove = (kb: KbOut, d: DocOut) => `DELETE /api/kbs/${kb.id}/documents/${d.id}`;

/** Mock only the network boundary: every call is recorded, unknown routes fail the request. */
function mockApi(routes: Record<string, Route>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const key = `${init?.method ?? "GET"} ${new URL(url, "http://localhost").pathname}`;
      calls.push({ key, init });
      const route = routes[key];
      if (!route) throw new Error(`unexpected request ${key}`);
      return route(init);
    }),
  );
  return calls;
}

const keys = (calls: Call[]) => calls.map((call) => call.key);
const count = (calls: Call[], key: string) => calls.filter((call) => call.key === key).length;
const headersOf = (call: Call) => new Headers(call.init?.headers);
const fileOf = (init: RequestInit | undefined) => (init?.body as FormData).get("file") as File;

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

const file = (name: string, content = "contenuto") => new File([content], name, { type: "text/plain" });

/** The admin is signed in and the backend holds the KBs; `server.documents` is what the list route answers. */
function adminRoutes(server: { documents: DocOut[] }, extra: Record<string, Route> = {}, kbs = [manuali, procedure]) {
  return mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/kbs": () => json(200, kbs),
    [list(manuali)]: () => json(200, server.documents),
    ...extra,
  });
}

async function openDocumentsPage(heading = "Documenti") {
  window.history.pushState({}, "", "/admin/documents");
  render(<App />);
  await screen.findByRole("heading", { name: heading, level: 2 });
}

async function chooseKb(kb: KbOut, label = "Knowledge base") {
  const select = await screen.findByLabelText(label);
  await screen.findByRole("option", { name: kb.name });
  fireEvent.change(select, { target: { value: kb.id } });
}

function selectFiles(files: File[], label = "File da caricare") {
  fireEvent.change(screen.getByLabelText(label), { target: { files } });
}

async function rowOf(name: string): Promise<HTMLElement> {
  const cell = await screen.findByRole("cell", { name });
  const row = cell.closest("tr");
  if (!row) throw new Error(`no row for ${name}`);
  return row;
}

const dataRows = () => screen.queryAllByRole("row").slice(1);

/** Let the fake clock run for `ms` and the requests it triggers finish. */
async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  window.history.pushState({}, "", "/");
  browserLanguages(["it-IT"]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// AC-044 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-07-fase-2-ingestion/AC-044: a user sees no Documents menu and the route /admin/documents takes them to /", async () => {
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  window.history.pushState({}, "", "/admin/documents");
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Manuali" })).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Documenti" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Documenti" })).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/");
  expect(keys(calls).filter((key) => key !== "GET /api/auth/me" && key !== "GET /api/kbs")).toEqual([]);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-044: a user who has the interface open sees no Documents menu", async () => {
  browserLanguages(["en-US"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Documents" })).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-044: without a session the route goes to the login page", async () => {
  const calls = mockApi({ "GET /api/auth/me": apiError(401, "not_authenticated") });
  window.history.pushState({}, "", "/admin/documents");
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Accedi" })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/login");
  expect(keys(calls)).toEqual(["GET /api/auth/me"]);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-044: the admin sees the Documents menu in both languages and opens the page", async () => {
  adminRoutes({ documents: [] });
  render(<App />);

  const banner = await screen.findByRole("banner");
  fireEvent.click(await within(banner).findByRole("link", { name: "Documenti" }));

  expect(await screen.findByRole("heading", { name: "Documenti", level: 2 })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/admin/documents");
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-044: the page and its menu are in English with an English browser", async () => {
  browserLanguages(["en-US"]);
  adminRoutes({ documents: [] });
  render(<App />);

  const banner = await screen.findByRole("banner");
  fireEvent.click(await within(banner).findByRole("link", { name: "Documents" }));

  expect(await screen.findByRole("heading", { name: "Documents", level: 2 })).toBeInTheDocument();
  expect(await screen.findByLabelText("Knowledge base")).toBeInTheDocument();
  expect(screen.getByLabelText("Files to upload")).toBeInTheDocument();
});

// AC-040 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: the page lists the KBs and loads the documents of the chosen one, newest first", async () => {
  const server = {
    documents: [doc(2, manuali, "nuovo.pdf", "READY", { size_bytes: 1536 }), doc(1, manuali, "vecchio.txt", "QUEUED")],
  };
  const calls = adminRoutes(server);
  await openDocumentsPage();
  expect(keys(calls)).not.toContain(list(manuali));

  await chooseKb(manuali);

  const first = await rowOf("nuovo.pdf");
  expect(within(first).getByText("1,5 KB")).toBeInTheDocument();
  expect(within(first).getByText("Pronto")).toBeInTheDocument();
  const second = await rowOf("vecchio.txt");
  expect(within(second).getByText("2 KB")).toBeInTheDocument();
  expect(within(second).getByText("In coda")).toBeInTheDocument();
  expect(dataRows()).toEqual([first, second]);
  expect(count(calls, list(manuali))).toBe(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: the upload control stays disabled until a KB is chosen", async () => {
  adminRoutes({ documents: [] });
  await openDocumentsPage();

  expect(await screen.findByLabelText("File da caricare")).toBeDisabled();
  await chooseKb(manuali);
  await waitFor(() => expect(screen.getByLabelText("File da caricare")).toBeEnabled());
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: two valid files are sent in two requests of their own and appear as two rows 'In coda'", async () => {
  const server: { documents: DocOut[] } = { documents: [] };
  let next = 0;
  const calls = adminRoutes(server, {
    [upload(manuali)]: (init) => {
      next += 1;
      const created = doc(next, manuali, fileOf(init).name, "QUEUED", { size_bytes: fileOf(init).size });
      server.documents = [created, ...server.documents];
      return json(202, created);
    },
  });
  await openDocumentsPage();
  await chooseKb(manuali);
  await screen.findByText("Nessun documento in questa knowledge base.");

  selectFiles([file("manuale.txt", "x".repeat(12)), file("guida.md", "y".repeat(3000))]);

  const first = await rowOf("manuale.txt");
  const second = await rowOf("guida.md");
  expect(within(first).getByText("12 B")).toBeInTheDocument();
  expect(within(first).getByText("In coda")).toBeInTheDocument();
  expect(within(second).getByText("2,9 KB")).toBeInTheDocument();
  expect(within(second).getByText("In coda")).toBeInTheDocument();
  expect(dataRows()).toHaveLength(2);

  const uploads = calls.filter((call) => call.key === upload(manuali));
  expect(uploads.map((call) => fileOf(call.init).name).sort()).toEqual(["guida.md", "manuale.txt"]);
  for (const call of uploads) {
    expect(call.init?.body).toBeInstanceOf(FormData);
    expect(headersOf(call).get("X-Requested-With")).toBe("XMLHttpRequest");
    expect(headersOf(call).has("Content-Type")).toBe(false); // the browser adds the multipart boundary
    expect((call.init?.body as FormData).getAll("file")).toHaveLength(1);
  }
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: each row reaches 'Pronto' by itself, the list is read every 3 seconds and the polling stops", async () => {
  const server = { documents: [doc(2, manuali, "b.txt", "QUEUED"), doc(1, manuali, "a.txt", "QUEUED")] };
  const calls = adminRoutes(server);
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("a.txt");
  expect(count(calls, list(manuali))).toBe(1);

  await tick(2500);
  expect(count(calls, list(manuali))).toBe(1);

  server.documents = [doc(2, manuali, "b.txt", "PROCESSING"), doc(1, manuali, "a.txt", "READY")];
  await tick(1000);
  expect(count(calls, list(manuali))).toBe(2);
  expect(within(await rowOf("a.txt")).getByText("Pronto")).toBeInTheDocument();
  expect(within(await rowOf("b.txt")).getByText("In elaborazione")).toBeInTheDocument();

  server.documents = [doc(2, manuali, "b.txt", "READY"), doc(1, manuali, "a.txt", "READY")];
  await tick(3000);
  expect(count(calls, list(manuali))).toBe(3);
  expect(within(await rowOf("b.txt")).getByText("Pronto")).toBeInTheDocument();
  expect(screen.queryByText("In coda")).not.toBeInTheDocument();
  expect(screen.queryByText("In elaborazione")).not.toBeInTheDocument();

  await tick(30000);
  expect(count(calls, list(manuali))).toBe(3);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: a document that fails or is unsupported also ends the polling", async () => {
  const server = { documents: [doc(2, manuali, "b.pdf", "PROCESSING"), doc(1, manuali, "a.pdf", "QUEUED")] };
  const calls = adminRoutes(server);
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("a.pdf");

  server.documents = [
    doc(2, manuali, "b.pdf", "UNSUPPORTED", { status_reason: { code: "no_text_extracted", message: "English" } }),
    doc(1, manuali, "a.pdf", "FAILED", failedWith("parse_failed")),
  ];
  await tick(3000);
  expect(within(await rowOf("a.pdf")).getByText("Fallito")).toBeInTheDocument();
  expect(within(await rowOf("b.pdf")).getByText("Non supportato")).toBeInTheDocument();

  await tick(30000);
  expect(count(calls, list(manuali))).toBe(2);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: a list with only finished documents is read once and never again", async () => {
  const server = {
    documents: [doc(1, manuali, "a.txt", "READY"), doc(2, manuali, "b.txt", "FAILED", failedWith("parse_failed"))],
  };
  const calls = adminRoutes(server);
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("a.txt");

  await tick(60000);

  expect(count(calls, list(manuali))).toBe(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: the polling starts when an upload adds a document, with no page reload", async () => {
  const server: { documents: DocOut[] } = { documents: [] };
  const calls = adminRoutes(server, {
    [upload(manuali)]: (init) => {
      const created = doc(1, manuali, fileOf(init).name, "QUEUED");
      server.documents = [created];
      return json(202, created);
    },
  });
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await screen.findByText("Nessun documento in questa knowledge base.");

  selectFiles([file("nuovo.txt")]);
  await tick(100);
  expect(within(await rowOf("nuovo.txt")).getByText("In coda")).toBeInTheDocument();
  expect(count(calls, list(manuali))).toBe(1);

  server.documents = [doc(1, manuali, "nuovo.txt", "READY")];
  await tick(3100);

  expect(count(calls, list(manuali))).toBe(2);
  expect(within(await rowOf("nuovo.txt")).getByText("Pronto")).toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: changing KB loads its list and the polling follows the new KB only", async () => {
  const server = { documents: [doc(1, manuali, "manuale.txt", "QUEUED")] };
  const calls = adminRoutes(server, {
    [list(procedure)]: () => json(200, [doc(5, procedure, "procedura.txt", "PROCESSING", { kb_id: procedure.id })]),
  });
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("manuale.txt");

  fireEvent.change(screen.getByLabelText("Knowledge base"), { target: { value: procedure.id } });
  expect(await rowOf("procedura.txt")).toBeInTheDocument();
  expect(screen.queryByRole("cell", { name: "manuale.txt" })).not.toBeInTheDocument();
  await tick(3100);

  expect(count(calls, list(procedure))).toBe(2);
  expect(count(calls, list(manuali))).toBe(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: leaving the page stops the polling (the timer is cleaned up)", async () => {
  const server = { documents: [doc(1, manuali, "manuale.txt", "QUEUED")] };
  const calls = adminRoutes(server);
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("manuale.txt");

  fireEvent.click(within(screen.getByRole("banner")).getByRole("link", { name: "Home" }));
  expect(await screen.findByRole("heading", { name: "Tutte le knowledge base" })).toBeInTheDocument();
  await tick(20000);

  expect(count(calls, list(manuali))).toBe(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: a failed refresh keeps the rows and shows the error without role alert, then recovers", async () => {
  const server = { documents: [doc(1, manuali, "manuale.txt", "QUEUED")] };
  let failing = false;
  const calls = adminRoutes(server, {
    [list(manuali)]: () =>
      failing ? json(500, { error: { code: "internal_error", message: "x" } }) : json(200, server.documents),
  });
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("manuale.txt");

  failing = true;
  await tick(3100);
  expect(screen.getByText("Errore interno del server. Riprova.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(await rowOf("manuale.txt")).toBeInTheDocument();

  failing = false;
  server.documents = [doc(1, manuali, "manuale.txt", "READY")];
  await tick(3100);

  expect(within(await rowOf("manuale.txt")).getByText("Pronto")).toBeInTheDocument();
  expect(screen.queryByText("Errore interno del server. Riprova.")).not.toBeInTheDocument();
  expect(count(calls, list(manuali))).toBe(3);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: a load error of the KBs or of the documents is shown without role alert", async () => {
  mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/kbs": () => json(200, [manuali]),
    [list(manuali)]: apiError(404, "kb_not_found"),
  });
  await openDocumentsPage();
  await chooseKb(manuali);

  expect(await screen.findByText("Knowledge base non trovata.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: a failed KB list is shown without role alert", async () => {
  mockApi({ "GET /api/auth/me": () => json(200, anna), "GET /api/kbs": apiError(500, "internal_error") });
  await openDocumentsPage();

  expect(await screen.findByText("Errore interno del server. Riprova.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: the rows show name, size, status and the date, with the headings of the columns", async () => {
  adminRoutes({ documents: [doc(1, manuali, "manuale.pdf", "READY", { created_at: "2026-11-07T10:30:00Z" })] });
  await openDocumentsPage();
  await chooseKb(manuali);

  const row = await rowOf("manuale.pdf");
  const headings = screen.getAllByRole("columnheader").map((cell) => cell.textContent);
  expect(headings).toEqual(["Nome", "Dimensione", "Stato", "Motivo", "Data", "Azioni"]);
  const date = within(row).getAllByRole("cell")[4];
  expect(date.textContent).toBe(new Date("2026-11-07T10:30:00Z").toLocaleString("it"));
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-040: the statuses are translated into English too", async () => {
  browserLanguages(["en-US"]);
  adminRoutes({
    documents: [
      doc(5, manuali, "e.txt", "UNSUPPORTED", { status_reason: { code: "no_text_extracted", message: "x" } }),
      doc(4, manuali, "d.txt", "FAILED", failedWith("parse_failed")),
      doc(3, manuali, "c.txt", "READY"),
      doc(2, manuali, "b.txt", "PROCESSING"),
      doc(1, manuali, "a.txt", "QUEUED"),
    ],
  });
  await openDocumentsPage("Documents");
  await chooseKb(manuali);

  expect(within(await rowOf("a.txt")).getByText("Queued")).toBeInTheDocument();
  expect(within(await rowOf("b.txt")).getByText("Processing")).toBeInTheDocument();
  expect(within(await rowOf("c.txt")).getByText("Ready")).toBeInTheDocument();
  expect(within(await rowOf("d.txt")).getByText("Failed")).toBeInTheDocument();
  expect(within(await rowOf("e.txt")).getByText("Unsupported")).toBeInTheDocument();
});

// AC-041 ---------------------------------------------------------------------------------------------------------------

const UPLOAD_CODES: Record<string, [number, string, string]> = {
  "doppione.pdf": [409, "duplicate_document", "Questo file è già presente nella knowledge base."],
  "programma.exe": [
    415,
    "unsupported_type",
    "Tipo di file non supportato. Sono ammessi PDF, DOCX, PPTX, XLSX, TXT e MD.",
  ],
  "enorme.pdf": [413, "file_too_large", "Il file supera la dimensione massima consentita."],
};

it("SPEC-2026-11-07-fase-2-ingestion/AC-041: a duplicate, a type not allowed and a file over the limit each show a translated message next to their name and add no row", async () => {
  const calls = adminRoutes(
    { documents: [] },
    {
      [upload(manuali)]: (init) => {
        const [status, code] = UPLOAD_CODES[fileOf(init).name];
        return apiError(status, code)();
      },
    },
  );
  await openDocumentsPage();
  await chooseKb(manuali);
  await screen.findByText("Nessun documento in questa knowledge base.");

  selectFiles(Object.keys(UPLOAD_CODES).map((name) => file(name)));

  for (const [name, [, , message]] of Object.entries(UPLOAD_CODES)) {
    const item = (await screen.findByText(name, { exact: false, selector: "li, li *" })).closest("li");
    expect(item).not.toBeNull();
    expect(within(item as HTMLElement).getByText(message)).toBeInTheDocument();
  }
  expect(dataRows()).toHaveLength(0);
  expect(screen.queryByRole("cell")).not.toBeInTheDocument();
  expect(count(calls, upload(manuali))).toBe(3);
  expect(count(calls, list(manuali))).toBe(1);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-041: the same three messages in English", async () => {
  browserLanguages(["en-US"]);
  adminRoutes(
    { documents: [] },
    {
      [upload(manuali)]: (init) => {
        const [status, code] = UPLOAD_CODES[fileOf(init).name];
        return apiError(status, code)();
      },
    },
  );
  await openDocumentsPage("Documents");
  await chooseKb(manuali);
  await screen.findByText("No document in this knowledge base.");

  selectFiles(
    Object.keys(UPLOAD_CODES).map((name) => file(name)),
    "Files to upload",
  );

  const expected: Record<string, string> = {
    "doppione.pdf": "This file is already in the knowledge base.",
    "programma.exe": "File type not supported. Allowed: PDF, DOCX, PPTX, XLSX, TXT and MD.",
    "enorme.pdf": "The file exceeds the maximum allowed size.",
  };
  for (const [name, message] of Object.entries(expected)) {
    const item = (await screen.findByText(name, { exact: false, selector: "li, li *" })).closest("li");
    expect(within(item as HTMLElement).getByText(message)).toBeInTheDocument();
  }
  expect(dataRows()).toHaveLength(0);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-041: the files that are accepted get a row, the refused ones only a message, and a new selection clears the old messages", async () => {
  const server: { documents: DocOut[] } = { documents: [] };
  let next = 0;
  adminRoutes(server, {
    [upload(manuali)]: (init) => {
      const name = fileOf(init).name;
      if (name === "doppione.pdf") return apiError(409, "duplicate_document")();
      next += 1;
      const created = doc(next, manuali, name, "QUEUED");
      server.documents = [created, ...server.documents];
      return json(202, created);
    },
  });
  await openDocumentsPage();
  await chooseKb(manuali);
  await screen.findByText("Nessun documento in questa knowledge base.");

  selectFiles([file("doppione.pdf"), file("buono.txt")]);

  expect(await rowOf("buono.txt")).toBeInTheDocument();
  const item = (await screen.findByText("doppione.pdf", { selector: "li, li *" })).closest("li");
  expect(within(item as HTMLElement).getByText("Questo file è già presente nella knowledge base.")).toBeInTheDocument();
  expect(dataRows()).toHaveLength(1);
  expect(screen.queryByRole("cell", { name: "doppione.pdf" })).not.toBeInTheDocument();

  selectFiles([file("altro.txt")]);

  expect(await rowOf("altro.txt")).toBeInTheDocument();
  expect(screen.queryByText("Questo file è già presente nella knowledge base.")).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-041: every error code of the upload has its own translated message, in Italian and in English", async () => {
  const codes: [number, string][] = [
    [409, "duplicate_document"],
    [415, "unsupported_type"],
    [413, "file_too_large"],
    [422, "empty_file"],
    [422, "validation_error"],
    [404, "kb_not_found"],
    [401, "not_authenticated"],
    [403, "forbidden"],
    [500, "internal_error"],
  ];
  const seen = new Set<string>();
  for (const language of ["it-IT", "en-US"]) {
    for (const [status, code] of codes) {
      browserLanguages([language]);
      adminRoutes({ documents: [] }, { [upload(manuali)]: apiError(status, code) });
      const english = language === "en-US";
      await openDocumentsPage(english ? "Documents" : "Documenti");
      await chooseKb(manuali);
      selectFiles([file("prova.txt")], english ? "Files to upload" : "File da caricare");

      const item = (await screen.findByText("prova.txt", { selector: "li, li *" })).closest("li") as HTMLElement;
      const message = (item.textContent ?? "").replace("prova.txt", "");
      expect(message, `${language} ${code}`).not.toContain(code);
      expect(message, `${language} ${code}`).not.toContain("English text");
      expect(message.trim().length, `${language} ${code}`).toBeGreaterThan(8);
      seen.add(`${language} ${message}`);
      cleanup();
      vi.unstubAllGlobals();
    }
  }
  // 9 codes, 2 languages: the generic fallback would make several of them equal
  expect(seen.size).toBeGreaterThanOrEqual(16);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-041: a network error is shown next to the file and no row is added", async () => {
  adminRoutes(
    { documents: [] },
    {
      [upload(manuali)]: () => {
        throw new TypeError("Failed to fetch");
      },
    },
  );
  await openDocumentsPage();
  await chooseKb(manuali);
  selectFiles([file("prova.txt")]);

  const item = (await screen.findByText("prova.txt", { selector: "li, li *" })).closest("li") as HTMLElement;
  expect(within(item).getByText("Il server non è raggiungibile. Riprova.")).toBeInTheDocument();
  expect(dataRows()).toHaveLength(0);
});

// AC-042 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: a FAILED document shows the translated reason and 'Riprova', READY and UNSUPPORTED have no button", async () => {
  adminRoutes({
    documents: [
      doc(3, manuali, "vuoto.pdf", "UNSUPPORTED", {
        status_reason: { code: "no_text_extracted", message: "English text" },
      }),
      doc(2, manuali, "pronto.txt", "READY"),
      doc(1, manuali, "rotto.docx", "FAILED", failedWith("embedding_unreachable")),
    ],
  });
  await openDocumentsPage();
  await chooseKb(manuali);

  const failed = await rowOf("rotto.docx");
  expect(within(failed).getByText("Il servizio degli embedding (Ollama) non è raggiungibile.")).toBeInTheDocument();
  expect(within(failed).queryByText("English text from the backend")).not.toBeInTheDocument();
  expect(within(failed).getByRole("button", { name: "Riprova" })).toBeInTheDocument();
  const ready = await rowOf("pronto.txt");
  expect(within(ready).queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  const unsupported = await rowOf("vuoto.pdf");
  expect(within(unsupported).queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  expect(
    within(unsupported).getByText(
      "Dal file non è stato estratto abbastanza testo (le scansioni senza testo non sono supportate).",
    ),
  ).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Riprova" })).toHaveLength(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: a document that is QUEUED or PROCESSING has no 'Riprova' either", async () => {
  adminRoutes({ documents: [doc(2, manuali, "b.txt", "PROCESSING"), doc(1, manuali, "a.txt", "QUEUED")] });
  await openDocumentsPage();
  await chooseKb(manuali);
  await rowOf("a.txt");

  expect(screen.queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: 'Riprova' calls the retry route without a body and the row goes to 'In coda', then to 'Pronto' by itself", async () => {
  const failed = doc(1, manuali, "rotto.docx", "FAILED", failedWith("docling_unreachable"));
  const server = { documents: [failed] };
  const calls = adminRoutes(server, {
    [retry(manuali, failed)]: () => {
      server.documents = [{ ...failed, status: "QUEUED", status_reason: null }];
      return json(202, server.documents[0]);
    },
  });
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("rotto.docx");

  fireEvent.click(within(row).getByRole("button", { name: "Riprova" }));
  await tick(100);

  const queued = await rowOf("rotto.docx");
  expect(within(queued).getByText("In coda")).toBeInTheDocument();
  expect(within(queued).queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  expect(
    within(queued).queryByText("Il servizio di lettura dei documenti (Docling) non è raggiungibile."),
  ).not.toBeInTheDocument();
  const retries = calls.filter((call) => call.key === retry(manuali, failed));
  expect(retries).toHaveLength(1);
  expect(retries[0].init?.body).toBeUndefined();
  expect(headersOf(retries[0]).has("Content-Type")).toBe(false);

  server.documents = [{ ...failed, status: "READY", status_reason: null, chunk_count: 4 }];
  await tick(3100);
  expect(within(await rowOf("rotto.docx")).getByText("Pronto")).toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: a retry refused with original_missing shows a translated message in the row and removes the button", async () => {
  const failed = doc(1, manuali, "rotto.docx", "FAILED", failedWith("docling_error"));
  adminRoutes({ documents: [failed] }, { [retry(manuali, failed)]: apiError(409, "original_missing") });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("rotto.docx");

  fireEvent.click(within(row).getByRole("button", { name: "Riprova" }));

  expect(
    await within(row).findByText("Il file originale non esiste più: carica di nuovo il documento."),
  ).toBeInTheDocument();
  expect(within(row).getByText("Fallito")).toBeInTheDocument();
  expect(within(row).queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: a retry refused with not_retryable shows the message and reads the list again", async () => {
  const failed = doc(1, manuali, "rotto.docx", "FAILED", failedWith("docling_error"));
  const server = { documents: [failed] };
  const calls = adminRoutes(server, {
    [retry(manuali, failed)]: () => {
      server.documents = [{ ...failed, status: "PROCESSING", status_reason: null }];
      return apiError(409, "not_retryable")();
    },
  });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("rotto.docx");

  fireEvent.click(within(row).getByRole("button", { name: "Riprova" }));

  expect(
    await screen.findByText("Il documento non può essere riprovato: non è più nello stato Fallito."),
  ).toBeInTheDocument();
  expect(within(await rowOf("rotto.docx")).getByText("In elaborazione")).toBeInTheDocument();
  expect(count(calls, list(manuali))).toBe(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: every reason code has a translated text, in Italian and in English", async () => {
  const codes = [
    "docling_unreachable",
    "docling_error",
    "parse_failed",
    "embedding_unreachable",
    "embedding_error",
    "embedding_invalid_response",
    "embedding_dimension_mismatch",
    "verification_failed",
    "unexpected_error",
  ];
  const texts = new Set<string>();
  for (const language of ["it-IT", "en-US"]) {
    browserLanguages([language]);
    const english = language === "en-US";
    adminRoutes({
      documents: codes.map((code, index) => doc(index + 1, manuali, `${code}.pdf`, "FAILED", failedWith(code))),
    });
    await openDocumentsPage(english ? "Documents" : "Documenti");
    await chooseKb(manuali);
    for (const code of codes) {
      const row = await rowOf(`${code}.pdf`);
      const reason = within(row).getAllByRole("cell")[3].textContent ?? "";
      expect(reason, `${language} ${code}`).not.toBe("");
      expect(reason, `${language} ${code}`).not.toContain(code);
      expect(reason, `${language} ${code}`).not.toContain("English text from the backend");
      texts.add(`${language} ${reason}`);
    }
    cleanup();
    vi.unstubAllGlobals();
  }
  expect(texts.size).toBe(codes.length * 2);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: the English reasons of no_text_extracted and of the other states read well", async () => {
  browserLanguages(["en-US"]);
  adminRoutes({
    documents: [
      doc(2, manuali, "vuoto.pdf", "UNSUPPORTED", { status_reason: { code: "no_text_extracted", message: "x" } }),
      doc(1, manuali, "rotto.pdf", "FAILED", failedWith("parse_failed")),
    ],
  });
  await openDocumentsPage("Documents");
  await chooseKb(manuali);

  expect(
    within(await rowOf("vuoto.pdf")).getByText(
      "Not enough text could be extracted from the file (scans without text are not supported).",
    ),
  ).toBeInTheDocument();
  const failed = await rowOf("rotto.pdf");
  expect(within(failed).getByText("The file could not be read.")).toBeInTheDocument();
  expect(within(failed).getByRole("button", { name: "Retry" })).toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: a reason code the interface does not know shows the message of the backend", async () => {
  adminRoutes({
    documents: [
      doc(1, manuali, "strano.pdf", "FAILED", {
        status_reason: { code: "new_code", message: "Something new happened" },
      }),
    ],
  });
  await openDocumentsPage();
  await chooseKb(manuali);

  expect(within(await rowOf("strano.pdf")).getByText("Something new happened")).toBeInTheDocument();
});

// AC-043 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: 'Elimina' asks for a confirmation and 'Annulla' leaves the row and sends no request", async () => {
  const ready = doc(1, manuali, "manuale.txt", "READY");
  const calls = adminRoutes({ documents: [ready] }, { [remove(manuali, ready)]: noContent });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("manuale.txt");

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(within(row).getByRole("button", { name: "Elimina" }));

  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/manuale\.txt/)).toBeInTheDocument();
  expect(keys(calls)).not.toContain(remove(manuali, ready));
  fireEvent.click(within(dialog).getByRole("button", { name: "Annulla" }));

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await rowOf("manuale.txt")).toBeInTheDocument();
  expect(keys(calls)).not.toContain(remove(manuali, ready));
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: 'Conferma' deletes the document and removes only its row", async () => {
  const ready = doc(1, manuali, "manuale.txt", "READY");
  const other = doc(2, manuali, "altro.txt", "FAILED", failedWith("parse_failed"));
  const calls = adminRoutes({ documents: [other, ready] }, { [remove(manuali, ready)]: noContent });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("manuale.txt");

  fireEvent.click(within(row).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  await waitFor(() => expect(screen.queryByRole("cell", { name: "manuale.txt" })).not.toBeInTheDocument());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await rowOf("altro.txt")).toBeInTheDocument();
  expect(count(calls, remove(manuali, ready))).toBe(1);
  expect(dataRows()).toHaveLength(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: every document can be deleted, also one that is being processed", async () => {
  const processing = doc(1, manuali, "lungo.pdf", "PROCESSING");
  const calls = adminRoutes({ documents: [processing] }, { [remove(manuali, processing)]: noContent });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("lungo.pdf");

  fireEvent.click(within(row).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  await waitFor(() => expect(screen.queryByRole("cell", { name: "lungo.pdf" })).not.toBeInTheDocument());
  expect(count(calls, remove(manuali, processing))).toBe(1);
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: the confirmation and its buttons are in English too", async () => {
  browserLanguages(["en-US"]);
  const ready = doc(1, manuali, "manual.txt", "READY");
  adminRoutes({ documents: [ready] }, { [remove(manuali, ready)]: noContent });
  await openDocumentsPage("Documents");
  await chooseKb(manuali);
  const row = await rowOf("manual.txt");

  fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm" }));

  await waitFor(() => expect(screen.queryByRole("cell", { name: "manual.txt" })).not.toBeInTheDocument());
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: a delete that fails keeps the row and shows a translated message without role alert", async () => {
  const ready = doc(1, manuali, "manuale.txt", "READY");
  adminRoutes({ documents: [ready] }, { [remove(manuali, ready)]: apiError(500, "internal_error") });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("manuale.txt");

  fireEvent.click(within(row).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  expect(await screen.findByText("Errore interno del server. Riprova.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await rowOf("manuale.txt")).toBeInTheDocument();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: a document already gone (404) leaves the list after the delete", async () => {
  const ready = doc(1, manuali, "manuale.txt", "READY");
  const server = { documents: [ready] };
  adminRoutes(server, {
    [remove(manuali, ready)]: () => {
      server.documents = [];
      return apiError(404, "document_not_found")();
    },
  });
  await openDocumentsPage();
  await chooseKb(manuali);
  const row = await rowOf("manuale.txt");

  fireEvent.click(within(row).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  await waitFor(() => expect(screen.queryByRole("cell", { name: "manuale.txt" })).not.toBeInTheDocument());
  expect(await screen.findByText("Documento non trovato.")).toBeInTheDocument();
});

// Switching KB while a retry or a delete is in flight -------------------------------------------------------------------

/** A response that the test lets through when it wants, to put a request of KB A and the list of KB B in a given order. */
function gate<T>() {
  let open: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    open = resolve;
  });
  return { promise, open };
}

it("SPEC-2026-11-07-fase-2-ingestion/AC-042: a retry still in flight when the admin switches KB does not blank the list of the new KB", async () => {
  const failed = doc(1, manuali, "rotto.docx", "FAILED", failedWith("docling_unreachable"));
  const other = doc(2, procedure, "altro.txt", "READY");
  const retryAnswer = gate<Response>();
  const listOfProcedure = gate<Response>();
  adminRoutes(
    { documents: [failed] },
    {
      [retry(manuali, failed)]: () => retryAnswer.promise,
      [list(procedure)]: () => listOfProcedure.promise,
    },
  );
  await openDocumentsPage();
  await chooseKb(manuali);
  fireEvent.click(within(await rowOf("rotto.docx")).getByRole("button", { name: "Riprova" }));

  await chooseKb(procedure); // while the retry of "Manuali" has not been answered yet
  await act(async () => {
    retryAnswer.open(json(202, { ...failed, status: "QUEUED", status_reason: null }));
  });
  await act(async () => {
    listOfProcedure.open(json(200, [other]));
  });

  expect(await rowOf("altro.txt")).toBeInTheDocument();
  expect(screen.queryByRole("cell", { name: "rotto.docx" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("File da caricare")).toBeEnabled();
});

it("SPEC-2026-11-07-fase-2-ingestion/AC-043: a delete still in flight when the admin switches KB does not blank the list of the new KB", async () => {
  const ready = doc(1, manuali, "manuale.txt", "READY");
  const other = doc(2, procedure, "altro.txt", "READY");
  const deleteAnswer = gate<Response>();
  const listOfProcedure = gate<Response>();
  adminRoutes(
    { documents: [ready] },
    {
      [remove(manuali, ready)]: () => deleteAnswer.promise,
      [list(procedure)]: () => listOfProcedure.promise,
    },
  );
  await openDocumentsPage();
  await chooseKb(manuali);
  fireEvent.click(within(await rowOf("manuale.txt")).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Conferma" }));

  await chooseKb(procedure); // while the delete in "Manuali" has not been answered yet
  await act(async () => {
    deleteAnswer.open(noContent());
  });
  await act(async () => {
    listOfProcedure.open(json(200, [other]));
  });

  expect(await rowOf("altro.txt")).toBeInTheDocument();
  expect(screen.queryByRole("cell", { name: "manuale.txt" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("File da caricare")).toBeEnabled();
});
