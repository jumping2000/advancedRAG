// SPEC-2026-11-17-fase-3-chat
// The chat on the page of a KB (REQ-019): the answer appears while it is generated (SSE through fetch), the citations
// [n] open the excerpt of their source, a failed answer shows its reason and «Riprova», the conversations are listed,
// renamed and deleted, and every text exists in Italian and English: AC-038, AC-039, AC-040, AC-041.
// AC-044 (the stream crosses Nginx unbuffered) is the end-to-end test backend/tests/e2e/test_chat_ui.py.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../app/App";
import { ConversationView } from "../components/ConversationView";
import type { ConversationOut, MessageOut, SourceExcerpt, SourceOut } from "../components/ConversationView";
import { i18n } from "../i18n";

type UserOut = {
  id: string;
  username: string;
  role: "admin" | "user";
  is_active: boolean;
  language: "it" | "en" | null;
};
type KbOut = { id: string; name: string; description: string | null; created_at: string };
type Route = (init: RequestInit | undefined) => Response | Promise<Response>;
type Call = { key: string; init: RequestInit | undefined };
type Detail = ConversationOut & { messages: MessageOut[]; read_only: boolean };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const apiError = (status: number, code: string) => () => json(status, { error: { code, message: "English text" } });
const noContent = () => new Response(null, { status: 204 });

const mario: UserOut = {
  id: "8a1c7f9e-0000-4000-8000-0000000000b2",
  username: "mario",
  role: "user",
  is_active: true,
  language: null,
};
const manuali: KbOut = {
  id: "5b2d8e10-0000-4000-8000-0000000000a1",
  name: "Manuali",
  description: "Manuali tecnici",
  created_at: "2026-11-17T09:30:00Z",
};

const pad = (n: number) => String(n).padStart(12, "0");
const conv = (n: number, title: string): ConversationOut => ({
  id: `c0000000-0000-4000-8000-${pad(n)}`,
  kb_id: manuali.id,
  title,
  created_at: "2026-11-17T10:00:00Z",
  updated_at: "2026-11-17T10:00:00Z",
});
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
const failed = (n: number, code: string) => msg(n, "assistant", "", { status: "FAILED", error_code: code });
const detail = (c: ConversationOut, messages: MessageOut[]): Detail => ({ ...c, messages, read_only: false });

const listino: SourceOut = {
  number: 1,
  document_name: "listino.pdf",
  section_title: "Prezzi",
  page_start: 3,
  page_end: 3,
};
const garanzia: SourceOut = {
  number: 2,
  document_name: "garanzia.docx",
  section_title: null,
  page_start: 4,
  page_end: 5,
};

const listOf = `GET /api/kbs/${manuali.id}/conversations`;
const startOf = `POST /api/kbs/${manuali.id}/conversations`;
const getOf = (c: ConversationOut) => `GET /api/conversations/${c.id}`;
const askOf = (c: ConversationOut) => `POST /api/conversations/${c.id}/messages`;
const retryOf = (c: ConversationOut, m: MessageOut) => `POST /api/conversations/${c.id}/messages/${m.id}/retry`;
const patchOf = (c: ConversationOut) => `PATCH /api/conversations/${c.id}`;
const deleteOf = (c: ConversationOut) => `DELETE /api/conversations/${c.id}`;
const sourceOf = (c: ConversationOut, m: MessageOut, n: number) =>
  `GET /api/conversations/${c.id}/messages/${m.id}/sources/${n}`;

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

const count = (calls: Call[], key: string) => calls.filter((call) => call.key === key).length;
const callsOf = (calls: Call[], key: string) => calls.filter((call) => call.key === key);
const headersOf = (call: Call) => new Headers(call.init?.headers);
const bodyOf = (call: Call) => JSON.parse(call.init?.body as string) as unknown;

/** A server-sent stream that the test feeds event by event, to see what the page shows in between. */
function sseStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const raw = (chunk: string | Uint8Array) =>
    controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    raw,
    event: (name: string, data: unknown) => raw(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`),
    keepalive: () => raw(": keepalive\n\n"),
    end: () => controller.close(),
  };
}

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

/** The user mario is signed in and the backend holds the conversations of the KB (`server.conversations`). */
function chatRoutes(server: { conversations: ConversationOut[] }, extra: Record<string, Route> = {}) {
  return mockApi({
    "GET /api/auth/me": () => json(200, mario),
    [`GET /api/kbs/${manuali.id}`]: () => json(200, manuali),
    [listOf]: () => json(200, server.conversations),
    ...extra,
  });
}

async function openChat() {
  window.history.pushState({}, "", `/kbs/${manuali.id}`);
  render(<App />);
  await screen.findByRole("heading", { name: "Manuali", level: 2 });
}

const conversationList = () => screen.findByRole("navigation", { name: /^(Conversazioni|Conversations)$/ });

async function openConversation(title: string) {
  fireEvent.click(await within(await conversationList()).findByRole("button", { name: title }));
}

function ask(question: string, field = "Domanda", send = "Invia") {
  fireEvent.change(screen.getByLabelText(field), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: send }));
}

const messages = () => screen.getByRole("list", { name: /^(Messaggi|Messages)$/ });
const noKeysOnScreen = () => expect(document.body.textContent).not.toMatch(/\b(chat|errors|kbs)\.[A-Za-z_]+/);

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
  void i18n.changeLanguage("it");
});

// AC-038 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-17-fase-3-chat/AC-038: the KB name and description stay in the header and the conversations are listed on the left", async () => {
  chatRoutes({ conversations: [conv(2, "Garanzia"), conv(1, "Prezzi")] });
  await openChat();

  expect(screen.getByText("Manuali tecnici")).toBeInTheDocument();
  const items = within(await conversationList()).getAllByRole("listitem");
  expect(items.map((item) => within(item).getAllByRole("button")[0].textContent)).toEqual(["Garanzia", "Prezzi"]);
  expect(screen.getByRole("button", { name: "Nuova conversazione" })).toBeEnabled();
  expect(screen.getByLabelText("Domanda")).toBeEnabled();
  expect(screen.getByRole("link", { name: "Torna all'elenco" })).toHaveAttribute("href", "/");
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: the text appears before the end, the field stays disabled until the end, then the numbered sources appear", async () => {
  const server = { conversations: [] as ConversationOut[] };
  const stream = sseStream();
  const c = conv(1, "Quanto costa il prodotto?");
  const calls = chatRoutes(server, { [startOf]: () => stream.response });
  await openChat();
  expect(await screen.findByText("Nessuna conversazione.")).toBeInTheDocument();

  ask("  Quanto costa il prodotto?  ");

  const request = callsOf(calls, startOf)[0];
  expect(bodyOf(request)).toEqual({ question: "Quanto costa il prodotto?" });
  expect(headersOf(request).get("Content-Type")).toBe("application/json");
  expect(headersOf(request).get("X-Requested-With")).toBe("XMLHttpRequest");
  expect(within(messages()).getByText("Quanto costa il prodotto?")).toBeInTheDocument();
  expect(screen.getByLabelText("Domanda")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Invia" })).toBeDisabled();

  server.conversations = [c];
  const answer = msg(2, "assistant", "Il prezzo comprende IVA al 22% [1].", { sources: [listino] });
  act(() => stream.event("conversation", { conversation_id: c.id, message_id: answer.id }));
  act(() => stream.keepalive());
  act(() => stream.event("delta", { text: "Il prezzo " }));
  expect(await screen.findByText("Il prezzo")).toBeInTheDocument(); // before the end
  expect(screen.getByLabelText("Domanda")).toBeDisabled();
  act(() => stream.event("delta", { text: "comprende IVA al 22% [1]." }));
  expect(await screen.findByText("Il prezzo comprende IVA al 22% [1].")).toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "Fonti" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "[1]" })).not.toBeInTheDocument(); // not a source yet
  expect(screen.getByLabelText("Domanda")).toBeDisabled();

  act(() => stream.event("done", { message: answer }));
  act(() => stream.end());

  const sources = await screen.findByRole("list", { name: "Fonti" });
  const first = within(sources).getAllByRole("listitem")[0];
  expect(first).toHaveTextContent("[1]");
  expect(first).toHaveTextContent("listino.pdf");
  expect(first).toHaveTextContent("Prezzi");
  expect(first).toHaveTextContent("p. 3");
  expect(screen.getByRole("button", { name: "[1]" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
  expect(
    within(await conversationList()).getByRole("button", { name: "Quanto costa il prodotto?" }),
  ).toBeInTheDocument();
  noKeysOnScreen();
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: events cut anywhere between packets, a multibyte character cut in two, and a keepalive are read as one stream", async () => {
  const stream = sseStream();
  const c = conv(1, "Domanda");
  chatRoutes({ conversations: [c] }, { [startOf]: () => stream.response });
  await openChat();
  ask("Domanda");

  const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"Perché "}\n\n');
  const cut = bytes.indexOf(0xc3) + 1; // between the two bytes of the "é"
  act(() => stream.raw(": keepalive\n\nevent: conversation\nda"));
  act(() => stream.raw(`ta: {"conversation_id":"${c.id}","message_id":"${msg(2, "assistant", "").id}"}\n\nevent: del`));
  act(() => stream.raw(bytes.slice(0, cut)));
  act(() => stream.raw(bytes.slice(cut)));
  act(() => stream.raw('event: delta\r\ndata: {"text":"no"}\r\n\r\n'));

  expect(await screen.findByText("Perché no")).toBeInTheDocument();
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: clicking a citation [n] opens the excerpt of its source and the button of the source closes it", async () => {
  const c = conv(1, "Prezzi");
  const answer = msg(2, "assistant", "Il prezzo comprende IVA [1]. La garanzia dura due anni [2].", {
    sources: [listino, garanzia],
  });
  const calls = chatRoutes(
    { conversations: [c] },
    {
      [getOf(c)]: () => json(200, detail(c, [msg(1, "user", "Quanto costa?"), answer])),
      [sourceOf(c, answer, 1)]: () =>
        json(200, { ...listino, available: true, content: "Il prezzo comprende IVA al 22%." } satisfies SourceExcerpt),
      [sourceOf(c, answer, 2)]: () =>
        json(200, { ...garanzia, available: false, content: null } satisfies SourceExcerpt),
    },
  );
  await openChat();
  await openConversation("Prezzi");
  expect(await screen.findByText(/Il prezzo comprende IVA \[1\]/)).toBeInTheDocument();
  expect(screen.queryByText("Il prezzo comprende IVA al 22%.")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "[1]" }));

  expect(await screen.findByText("Il prezzo comprende IVA al 22%.")).toBeInTheDocument();
  expect(count(calls, sourceOf(c, answer, 1))).toBe(1);
  const sources = within(screen.getByRole("list", { name: "Fonti" })).getAllByRole("listitem");
  expect(sources[1]).toHaveTextContent("garanzia.docx");
  expect(sources[1]).toHaveTextContent("pp. 4–5");

  fireEvent.click(within(sources[1]).getByRole("button", { name: "Mostra estratto" }));
  expect(await within(sources[1]).findByText("Fonte non più disponibile")).toBeInTheDocument();
  expect(sources[1]).toHaveTextContent("garanzia.docx"); // the name saved with the answer is still shown

  fireEvent.click(within(sources[0]).getByRole("button", { name: "Nascondi estratto" }));
  expect(screen.queryByText("Il prezzo comprende IVA al 22%.")).not.toBeInTheDocument();
  noKeysOnScreen();
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: an excerpt that cannot be read shows its translated error", async () => {
  const c = conv(1, "Prezzi");
  const answer = msg(2, "assistant", "Il prezzo [1].", { sources: [listino] });
  chatRoutes(
    { conversations: [c] },
    {
      [getOf(c)]: () => json(200, detail(c, [msg(1, "user", "Quanto costa?"), answer])),
      [sourceOf(c, answer, 1)]: apiError(404, "conversation_not_found"),
    },
  );
  await openChat();
  await openConversation("Prezzi");

  fireEvent.click(await screen.findByRole("button", { name: "[1]" }));

  expect(await screen.findByText("Conversazione non trovata.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // the header selector owns the alert
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: HTML, markdown and a [n] that is not a source in the answer are text, and only a valid [n] is a button", async () => {
  const c = conv(1, "Codice");
  const text = "<b>grassetto</b> e **markdown** `[1]` [9] <img src=x onerror=alert(1)>";
  chatRoutes(
    { conversations: [c] },
    {
      [getOf(c)]: () =>
        json(200, detail(c, [msg(1, "user", "Come?"), msg(2, "assistant", text, { sources: [listino] })])),
    },
  );
  await openChat();
  await openConversation("Codice");

  const answer = await screen.findByText(/grassetto/);
  expect(answer.closest("li")).toHaveTextContent(
    "<b>grassetto</b> e **markdown** `[1]` [9] <img src=x onerror=alert(1)>",
  );
  expect(messages().querySelector("b, img, strong > b, em")).toBeNull();
  expect(screen.queryByRole("button", { name: "[9]" })).not.toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "[1]" }).length).toBeGreaterThan(0);
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: a second question goes to the open conversation and the first answer stays", async () => {
  const c = conv(1, "Prezzi");
  const stream = sseStream();
  const calls = chatRoutes(
    { conversations: [c] },
    {
      [getOf(c)]: () => json(200, detail(c, [msg(1, "user", "Quanto costa?"), msg(2, "assistant", "Dieci euro.")])),
      [askOf(c)]: () => stream.response,
    },
  );
  await openChat();
  await openConversation("Prezzi");
  expect(await screen.findByText("Dieci euro.")).toBeInTheDocument();

  ask("E la garanzia?");

  expect(bodyOf(callsOf(calls, askOf(c))[0])).toEqual({ question: "E la garanzia?" });
  expect(count(calls, startOf)).toBe(0);
  const answer = msg(4, "assistant", "Due anni.");
  act(() => stream.event("conversation", { conversation_id: c.id, message_id: answer.id }));
  act(() => stream.event("delta", { text: "Due anni." }));
  act(() => stream.event("done", { message: answer }));
  act(() => stream.end());
  expect(await screen.findByText("Due anni.")).toBeInTheDocument();
  expect(screen.getByText("Dieci euro.")).toBeInTheDocument();
  expect(within(messages()).getAllByRole("listitem")).toHaveLength(4);
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
  expect(screen.getByLabelText("Domanda")).toHaveValue("");
});

it.each([
  [503, "generation_not_configured", "Il provider di generazione non è configurato: contatta l'amministratore."],
  [409, "answer_in_progress", "Una risposta è già in corso in questa conversazione."],
  [422, "validation_error", "Il testo non è valido: controlla la lunghezza."],
])(
  "SPEC-2026-11-17-fase-3-chat/AC-038: a question refused before the stream (%i %s) shows its message, keeps the question in the field and adds no message",
  async (status, code, text) => {
    const calls = chatRoutes({ conversations: [] }, { [startOf]: apiError(status, code) });
    await openChat();

    ask("Quanto costa?");

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
    expect(screen.getByLabelText("Domanda")).toHaveValue("Quanto costa?");
    expect(screen.queryByRole("list", { name: "Messaggi" })).not.toBeInTheDocument();
    expect(count(calls, startOf)).toBe(1);
  },
);

it("SPEC-2026-11-17-fase-3-chat/AC-038: a question with only spaces cannot be sent", async () => {
  const calls = chatRoutes({ conversations: [] });
  await openChat();

  fireEvent.change(screen.getByLabelText("Domanda"), { target: { value: "   " } });

  expect(screen.getByRole("button", { name: "Invia" })).toBeDisabled();
  expect(count(calls, startOf)).toBe(0);
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: an answer still in progress when the conversation is opened disables the field until it is completed", async () => {
  const c = conv(1, "Prezzi");
  let current = detail(c, [msg(1, "user", "Quanto costa?"), msg(2, "assistant", "", { status: "PENDING" })]);
  const calls = chatRoutes({ conversations: [c] }, { [getOf(c)]: () => json(200, current) });
  vi.useFakeTimers({ shouldAdvanceTime: true }); // before the page, so that its timers are the fake ones
  await openChat();
  await openConversation("Prezzi");
  expect(await screen.findByText("Risposta in corso…")).toBeInTheDocument();
  expect(screen.getByLabelText("Domanda")).toBeDisabled();

  current = detail(c, [msg(1, "user", "Quanto costa?"), msg(2, "assistant", "Dieci euro.")]);
  await tick(3000);

  expect(await screen.findByText("Dieci euro.")).toBeInTheDocument();
  expect(screen.queryByText("Risposta in corso…")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Domanda")).toBeEnabled();
  const reads = count(calls, getOf(c));
  await tick(30000);
  expect(count(calls, getOf(c))).toBe(reads); // the polling stops
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: a KB that does not exist shows its message and no chat", async () => {
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    [`GET /api/kbs/${manuali.id}`]: apiError(404, "kb_not_found"),
    [listOf]: apiError(404, "kb_not_found"),
  });
  window.history.pushState({}, "", `/kbs/${manuali.id}`);
  render(<App />);

  expect(await screen.findByText("Knowledge base non trovata.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Domanda")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

// AC-039 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-17-fase-3-chat/AC-039: a failed last answer shows its translated reason and «Riprova», which leads to a completed answer", async () => {
  const c = conv(1, "Prezzi");
  const question = msg(1, "user", "Quanto costa?");
  const stream = sseStream();
  const calls = chatRoutes(
    { conversations: [c] },
    {
      [getOf(c)]: () => json(200, detail(c, [question, failed(2, "generation_timeout")])),
      [retryOf(c, failed(2, ""))]: () => stream.response,
    },
  );
  await openChat();
  await openConversation("Prezzi");
  expect(await screen.findByText("Il provider di generazione non ha finito in tempo.")).toBeInTheDocument();
  noKeysOnScreen();

  fireEvent.click(screen.getByRole("button", { name: "Riprova" }));

  const request = callsOf(calls, retryOf(c, failed(2, "")))[0];
  expect(request.init?.body).toBeUndefined();
  expect(headersOf(request).has("Content-Type")).toBe(false); // no body
  expect(headersOf(request).get("X-Requested-With")).toBe("XMLHttpRequest");
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeDisabled());
  expect(screen.queryByText("Il provider di generazione non ha finito in tempo.")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  act(() => stream.event("conversation", { conversation_id: c.id, message_id: failed(2, "").id }));
  act(() => stream.event("delta", { text: "Dieci " }));
  expect(await screen.findByText("Dieci")).toBeInTheDocument();
  act(() => stream.event("delta", { text: "euro." }));
  act(() => stream.event("done", { message: msg(2, "assistant", "Dieci euro.") }));
  act(() => stream.end());

  expect(await screen.findByText("Dieci euro.")).toBeInTheDocument();
  expect(within(messages()).getAllByRole("listitem")).toHaveLength(2); // the failed answer is replaced, not added to
  expect(screen.queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
});

it("SPEC-2026-11-17-fase-3-chat/AC-039: a failed answer followed by a new question stays failed and has no «Riprova»", async () => {
  const c = conv(1, "Prezzi");
  chatRoutes(
    { conversations: [c] },
    {
      [getOf(c)]: () =>
        json(
          200,
          detail(c, [
            msg(1, "user", "Quanto costa?"),
            failed(2, "embedding_error"),
            msg(3, "user", "E la garanzia?"),
            msg(4, "assistant", "Due anni."),
          ]),
        ),
    },
  );
  await openChat();
  await openConversation("Prezzi");

  expect(await screen.findByText("Due anni.")).toBeInTheDocument();
  expect(screen.getByText("Il servizio di embedding (Ollama) non ha elaborato la domanda.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
});

it("SPEC-2026-11-17-fase-3-chat/AC-039: an error event during the answer shows the reason and «Riprova»; a retry the backend refuses shows its message", async () => {
  const stream = sseStream();
  const c = conv(1, "Domanda");
  const answer = failed(2, "generation_error");
  const calls = chatRoutes(
    { conversations: [c] },
    { [startOf]: () => stream.response, [retryOf(c, answer)]: apiError(409, "not_retryable") },
  );
  await openChat();
  ask("Domanda");

  act(() => stream.event("conversation", { conversation_id: c.id, message_id: answer.id }));
  act(() => stream.event("delta", { text: "testo parziale" }));
  expect(await screen.findByText("testo parziale")).toBeInTheDocument();
  act(() => stream.event("error", { error: { code: "generation_error", message: "English text" } }));
  act(() => stream.end());

  expect(
    await screen.findByText("Il provider di generazione ha avuto un errore o ha inviato una risposta non valida."),
  ).toBeInTheDocument();
  expect(screen.queryByText("testo parziale")).not.toBeInTheDocument(); // the partial text is discarded (REQ-015)
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());

  fireEvent.click(screen.getByRole("button", { name: "Riprova" }));

  expect(await screen.findByText(/Non si può riprovare/)).toBeInTheDocument();
  expect(count(calls, retryOf(c, answer))).toBe(1);
  expect(screen.getByRole("button", { name: "Riprova" })).toBeInTheDocument();
  expect(
    screen.getByText("Il provider di generazione ha avuto un errore o ha inviato una risposta non valida."),
  ).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
});

it("SPEC-2026-11-17-fase-3-chat/AC-039: a stream that ends without done or error shows what the server saved (interrupted)", async () => {
  const stream = sseStream();
  const c = conv(1, "Domanda");
  const answer = failed(2, "interrupted");
  const calls = chatRoutes(
    { conversations: [c] },
    {
      [startOf]: () => stream.response,
      [getOf(c)]: () => json(200, detail(c, [msg(1, "user", "Domanda"), answer])),
    },
  );
  await openChat();
  ask("Domanda");

  act(() => stream.event("conversation", { conversation_id: c.id, message_id: answer.id }));
  act(() => stream.event("delta", { text: "testo parziale" }));
  expect(await screen.findByText("testo parziale")).toBeInTheDocument();
  act(() => stream.end());

  expect(await screen.findByText("La risposta è stata interrotta.")).toBeInTheDocument();
  expect(screen.queryByText("testo parziale")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Riprova" })).toBeInTheDocument();
  expect(count(calls, getOf(c))).toBe(1);
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
});

it("SPEC-2026-11-17-fase-3-chat/AC-039: the connection lost before the stream starts shows the network message and keeps the question", async () => {
  const calls = chatRoutes(
    { conversations: [] },
    { [startOf]: () => Promise.reject(new TypeError("Failed to fetch")) },
  );
  await openChat();

  ask("Quanto costa?");

  expect(await screen.findByText("Il server non è raggiungibile. Riprova.")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
  expect(screen.getByLabelText("Domanda")).toHaveValue("Quanto costa?");
  expect(count(calls, startOf)).toBe(1);
});

it("SPEC-2026-11-17-fase-3-chat/AC-039: the ConversationView offers «Riprova» only on the last answer and not when it is read only", () => {
  const retry = vi.fn();
  const last = failed(4, "generation_timeout");
  const conversation = {
    id: conv(1, "Prezzi").id,
    messages: [msg(1, "user", "Uno?"), failed(2, "interrupted"), msg(3, "user", "Due?"), last],
  };

  const { rerender } = render(<ConversationView conversation={conversation} readOnly={false} onRetry={retry} />);
  const buttons = screen.getAllByRole("button", { name: "Riprova" });
  expect(buttons).toHaveLength(1);
  fireEvent.click(buttons[0]);
  expect(retry).toHaveBeenCalledTimes(1);
  expect(retry.mock.calls[0][0]).toMatchObject({ id: last.id });

  rerender(<ConversationView conversation={conversation} readOnly onRetry={retry} />);
  expect(screen.queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  expect(screen.getByText("Il provider di generazione non ha finito in tempo.")).toBeInTheDocument(); // the reason stays
});

it("SPEC-2026-11-17-fase-3-chat/AC-038: a read only ConversationView still opens the excerpts of the sources", async () => {
  const c = conv(1, "Prezzi");
  const answer = msg(2, "assistant", "Il prezzo [1].", { sources: [listino] });
  mockApi({
    [sourceOf(c, answer, 1)]: () => json(200, { ...listino, available: true, content: "Estratto del listino." }),
  });

  render(<ConversationView conversation={{ id: c.id, messages: [msg(1, "user", "Quanto?"), answer] }} readOnly />);
  fireEvent.click(screen.getByRole("button", { name: "[1]" }));

  expect(await screen.findByText("Estratto del listino.")).toBeInTheDocument();
});

// AC-040 ---------------------------------------------------------------------------------------------------------------

it("SPEC-2026-11-17-fase-3-chat/AC-040: «Nuova conversazione» empties the view, and the new conversation appears in the list after its first question", async () => {
  const old = conv(1, "Prezzi");
  const created = conv(2, "Che garanzia c'è?");
  const server = { conversations: [old] };
  const stream = sseStream();
  const calls = chatRoutes(server, {
    [getOf(old)]: () => json(200, detail(old, [msg(1, "user", "Quanto costa?"), msg(2, "assistant", "Dieci euro.")])),
    [startOf]: () => stream.response,
  });
  await openChat();
  await openConversation("Prezzi");
  expect(await screen.findByText("Dieci euro.")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Nuova conversazione" }));

  expect(screen.queryByText("Dieci euro.")).not.toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "Messaggi" })).not.toBeInTheDocument();
  ask("Che garanzia c'è?");
  expect(count(calls, askOf(old))).toBe(0);
  expect(within(await conversationList()).getAllByRole("listitem")).toHaveLength(1); // not yet: no question has been answered
  server.conversations = [created, old];
  const answer = msg(4, "assistant", "Due anni.");
  act(() => stream.event("conversation", { conversation_id: created.id, message_id: answer.id }));
  act(() => stream.event("delta", { text: "Due anni." }));
  act(() => stream.event("done", { message: answer }));
  act(() => stream.end());

  await waitFor(() =>
    expect(within(screen.getByRole("navigation", { name: "Conversazioni" })).getAllByRole("listitem")).toHaveLength(2),
  );
  const titles = within(screen.getByRole("navigation", { name: "Conversazioni" }))
    .getAllByRole("listitem")
    .map((item) => within(item).getAllByRole("button")[0].textContent);
  expect(titles).toEqual(["Che garanzia c'è?", "Prezzi"]);
  expect(count(calls, startOf)).toBe(1);
});

it("SPEC-2026-11-17-fase-3-chat/AC-040: «Rinomina» changes the title and «Annulla» leaves it", async () => {
  const c = conv(1, "Prezzi");
  const server = { conversations: [c] };
  const calls = chatRoutes(server, {
    [patchOf(c)]: (init) => {
      const renamed = { ...c, title: (JSON.parse(init?.body as string) as { title: string }).title };
      server.conversations = [renamed];
      return json(200, renamed);
    },
  });
  await openChat();
  const item = () => within(screen.getByRole("navigation", { name: "Conversazioni" })).getAllByRole("listitem")[0];
  await within(await conversationList()).findByRole("button", { name: "Prezzi" });

  fireEvent.click(within(item()).getByRole("button", { name: "Rinomina" }));
  expect(screen.getByLabelText("Nuovo titolo")).toHaveValue("Prezzi");
  fireEvent.change(screen.getByLabelText("Nuovo titolo"), { target: { value: "Altro titolo" } });
  fireEvent.click(screen.getByRole("button", { name: "Annulla" }));

  expect(screen.queryByLabelText("Nuovo titolo")).not.toBeInTheDocument();
  expect(within(item()).getByRole("button", { name: "Prezzi" })).toBeInTheDocument();
  expect(count(calls, patchOf(c))).toBe(0);

  fireEvent.click(within(item()).getByRole("button", { name: "Rinomina" }));
  fireEvent.change(screen.getByLabelText("Nuovo titolo"), { target: { value: "  Budget 2026  " } });
  fireEvent.click(screen.getByRole("button", { name: "Salva" }));

  expect(await within(item()).findByRole("button", { name: "Budget 2026" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Nuovo titolo")).not.toBeInTheDocument();
  expect(bodyOf(callsOf(calls, patchOf(c))[0])).toEqual({ title: "Budget 2026" });
  expect(headersOf(callsOf(calls, patchOf(c))[0]).get("Content-Type")).toBe("application/json");
});

it("SPEC-2026-11-17-fase-3-chat/AC-040: a title the backend refuses shows its message and keeps the form", async () => {
  const c = conv(1, "Prezzi");
  chatRoutes({ conversations: [c] }, { [patchOf(c)]: apiError(422, "validation_error") });
  await openChat();
  const item = within(await conversationList()).getAllByRole("listitem")[0];

  fireEvent.click(within(item).getByRole("button", { name: "Rinomina" }));
  fireEvent.change(screen.getByLabelText("Nuovo titolo"), { target: { value: "x".repeat(201) } });
  fireEvent.click(screen.getByRole("button", { name: "Salva" }));

  expect(await screen.findByText("Il testo non è valido: controlla la lunghezza.")).toBeInTheDocument();
  expect(screen.getByLabelText("Nuovo titolo")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("SPEC-2026-11-17-fase-3-chat/AC-040: «Elimina» asks for a confirmation: «Annulla» leaves the conversation, «Conferma» removes it", async () => {
  const keep = conv(2, "Garanzia");
  const c = conv(1, "Prezzi");
  const server = { conversations: [keep, c] };
  const calls = chatRoutes(server, {
    [getOf(c)]: () => json(200, detail(c, [msg(1, "user", "Quanto costa?"), msg(2, "assistant", "Dieci euro.")])),
    [deleteOf(c)]: () => {
      server.conversations = [keep];
      return noContent();
    },
  });
  await openChat();
  await openConversation("Prezzi");
  expect(await screen.findByText("Dieci euro.")).toBeInTheDocument();
  const itemOf = (title: string) =>
    within(screen.getByRole("navigation", { name: "Conversazioni" }))
      .getAllByRole("listitem")
      .find((item) => within(item).queryByRole("button", { name: title }))!;

  fireEvent.click(within(itemOf("Prezzi")).getByRole("button", { name: "Elimina" }));
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent("Prezzi");
  fireEvent.click(within(dialog).getByRole("button", { name: "Annulla" }));

  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(count(calls, deleteOf(c))).toBe(0);
  expect(itemOf("Prezzi")).toBeInTheDocument();
  expect(screen.getByText("Dieci euro.")).toBeInTheDocument();

  fireEvent.click(within(itemOf("Prezzi")).getByRole("button", { name: "Elimina" }));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Conferma" }));

  await waitFor(() => expect(screen.queryByRole("button", { name: "Prezzi" })).not.toBeInTheDocument());
  expect(count(calls, deleteOf(c))).toBe(1);
  expect(screen.getByRole("button", { name: "Garanzia" })).toBeInTheDocument();
  expect(screen.queryByText("Dieci euro.")).not.toBeInTheDocument(); // the open conversation was the deleted one
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("SPEC-2026-11-17-fase-3-chat/AC-040: while an answer is generated the conversations cannot be switched, renamed or deleted", async () => {
  const c = conv(1, "Prezzi");
  const stream = sseStream();
  chatRoutes({ conversations: [c] }, { [startOf]: () => stream.response });
  await openChat();
  await within(await conversationList()).findByRole("button", { name: "Prezzi" });

  ask("Domanda");

  const list = within(screen.getByRole("navigation", { name: "Conversazioni" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Nuova conversazione" })).toBeDisabled());
  for (const button of list.getAllByRole("button")) expect(button).toBeDisabled();
  act(() => stream.event("conversation", { conversation_id: c.id, message_id: msg(2, "assistant", "").id }));
  act(() => stream.event("done", { message: msg(2, "assistant", "Ok.") }));
  act(() => stream.end());
  await waitFor(() => expect(screen.getByRole("button", { name: "Nuova conversazione" })).toBeEnabled());
});

// AC-041 ---------------------------------------------------------------------------------------------------------------

const failedTexts: [string, string, string][] = [
  [
    "embedding_error",
    "Il servizio di embedding (Ollama) non ha elaborato la domanda.",
    "The embedding service (Ollama) could not process the question.",
  ],
  [
    "generation_error",
    "Il provider di generazione ha avuto un errore o ha inviato una risposta non valida.",
    "The generation provider failed or sent an invalid answer.",
  ],
  [
    "generation_timeout",
    "Il provider di generazione non ha finito in tempo.",
    "The generation provider did not finish in time.",
  ],
  [
    "internal_error",
    "La risposta non è stata completata per un errore interno.",
    "The answer could not be completed because of an internal error.",
  ],
  ["interrupted", "La risposta è stata interrotta.", "The answer was interrupted."],
];

it.each(failedTexts)(
  "SPEC-2026-11-17-fase-3-chat/AC-041: the reason %s of a failed answer is translated in Italian and in English",
  async (code, italian, english) => {
    const c = conv(1, "Prezzi");
    chatRoutes(
      { conversations: [c] },
      { [getOf(c)]: () => json(200, detail(c, [msg(1, "user", "Quanto costa?"), failed(2, code)])) },
    );
    await openChat();
    await openConversation("Prezzi");
    expect(await screen.findByText(italian)).toBeInTheDocument();
    noKeysOnScreen();

    await act(() => i18n.changeLanguage("en"));

    expect(await screen.findByText(english)).toBeInTheDocument();
    expect(screen.queryByText(italian)).not.toBeInTheDocument();
    noKeysOnScreen();
  },
);

it("SPEC-2026-11-17-fase-3-chat/AC-041: an English browser shows the page with a failed answer, one with sources and the notice of a KB without documents in English, and no translation key", async () => {
  browserLanguages(["en-US"]);
  const failedConversation = conv(1, "Failed one");
  const sourcesConversation = conv(2, "With sources");
  const emptyConversation = conv(3, "No documents");
  const answer = msg(4, "assistant", "The price includes VAT [1].", { sources: [listino, garanzia] });
  chatRoutes(
    { conversations: [failedConversation, sourcesConversation, emptyConversation] },
    {
      [getOf(failedConversation)]: () =>
        json(200, detail(failedConversation, [msg(1, "user", "How much?"), failed(2, "generation_timeout")])),
      [getOf(sourcesConversation)]: () => json(200, detail(sourcesConversation, [msg(3, "user", "How much?"), answer])),
      [getOf(emptyConversation)]: () =>
        json(
          200,
          detail(emptyConversation, [msg(5, "user", "How much?"), msg(6, "assistant", "", { notice: "no_documents" })]),
        ),
      [sourceOf(sourcesConversation, answer, 2)]: () => json(200, { ...garanzia, available: false, content: null }),
    },
  );
  await openChat();

  expect(screen.getByRole("heading", { name: "Conversations" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "New conversation" })).toBeInTheDocument();
  expect(screen.getByLabelText("Question")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Back to the list" })).toBeInTheDocument();
  const item = (title: string) =>
    within(screen.getByRole("navigation", { name: "Conversations" }))
      .getAllByRole("listitem")
      .find((entry) => within(entry).queryByRole("button", { name: title }))!;
  expect(within(item("Failed one")).getByRole("button", { name: "Rename" })).toBeInTheDocument();
  fireEvent.click(within(item("Failed one")).getByRole("button", { name: "Delete" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("button", { name: "Confirm" })).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  fireEvent.click(within(item("Failed one")).getByRole("button", { name: "Rename" }));
  expect(screen.getByLabelText("New title")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  noKeysOnScreen();

  await openConversation("Failed one");
  expect(await screen.findByText("The generation provider did not finish in time.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Messages" })).toBeInTheDocument();
  noKeysOnScreen();

  await openConversation("With sources");
  const sources = await screen.findByRole("list", { name: "Sources" });
  expect(within(sources).getAllByRole("button", { name: "Show excerpt" })).toHaveLength(2);
  expect(sources).toHaveTextContent("pp. 4–5");
  fireEvent.click(within(sources).getAllByRole("button", { name: "Show excerpt" })[1]);
  expect(await within(sources).findByText("Source no longer available")).toBeInTheDocument();
  expect(within(sources).getByRole("button", { name: "Hide excerpt" })).toBeInTheDocument();
  noKeysOnScreen();

  await openConversation("No documents");
  expect(await screen.findByText("No document available in this knowledge base")).toBeInTheDocument();
  noKeysOnScreen();
});

it("SPEC-2026-11-17-fase-3-chat/AC-041: the language selector switches the open chat from English to Italian and back, with a streaming answer in it", async () => {
  browserLanguages(["en-US"]);
  const c = conv(1, "Prezzi");
  const stream = sseStream();
  chatRoutes(
    { conversations: [c] },
    {
      [startOf]: () => stream.response,
      "PATCH /api/auth/me": (init) =>
        json(200, { ...mario, language: (JSON.parse(init?.body as string) as { language: "it" | "en" }).language }),
    },
  );
  await openChat();
  ask("Question", "Question", "Send");
  act(() => stream.event("conversation", { conversation_id: c.id, message_id: msg(2, "assistant", "").id }));
  act(() => stream.event("delta", { text: "Ten euros." }));
  expect(await screen.findByText("Ten euros.")).toBeInTheDocument();
  expect(screen.getByText("Generating the answer…")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Language"), { target: { value: "it" } });

  expect(await screen.findByRole("button", { name: "Nuova conversazione" })).toBeInTheDocument();
  expect(screen.getByText("Risposta in corso…")).toBeInTheDocument();
  expect(screen.getByText("Ten euros.")).toBeInTheDocument(); // the text of the answer is not translated, and stays
  expect(screen.getByRole("link", { name: "Torna all'elenco" })).toBeInTheDocument();
  noKeysOnScreen();

  act(() => stream.event("error", { error: { code: "generation_timeout", message: "English text" } }));
  act(() => stream.end());
  expect(await screen.findByText("Il provider di generazione non ha finito in tempo.")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Lingua"), { target: { value: "en" } });
  expect(await screen.findByText("The generation provider did not finish in time.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  noKeysOnScreen();
});
