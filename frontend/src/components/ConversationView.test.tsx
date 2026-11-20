// SPEC-2026-11-20-fase-4-rerank-rewrite
// The searched question under an answer (REQ-007): «Ricerca: <testo>» in Italian and «Search: <testo>» in English when
// the answer has a `search_query`, nothing when it is empty or null; the same line in the chat of the author and in the
// read-only page «Conversazioni» of the admin, which both show the messages with ConversationView: AC-013.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../app/App";
import { i18n } from "../i18n";
import { ConversationView } from "./ConversationView";
import type { ConversationOut, MessageOut } from "./ConversationView";

type UserOut = { id: string; username: string; role: "admin" | "user"; is_active: boolean; language: null };
type KbOut = { id: string; name: string; description: string | null; created_at: string };
type AdminConversation = ConversationOut & { username: string; kb_name: string; message_count: number };
type Route = () => Response | Promise<Response>;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

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
  created_at: "2026-11-20T09:30:00Z",
};

const pad = (n: number) => String(n).padStart(12, "0");
const conversation: ConversationOut = {
  id: `c0000000-0000-4000-8000-${pad(1)}`,
  kb_id: manuali.id,
  title: "Prezzi del prodotto X",
  created_at: "2026-11-20T10:00:00Z",
  updated_at: "2026-11-20T10:30:00Z",
};
const adminRow: AdminConversation = { ...conversation, username: "mario", kb_name: "Manuali", message_count: 4 };

function msg(n: number, role: "user" | "assistant", content: string, searchQuery: string | null): MessageOut {
  return {
    id: `a0000000-0000-4000-8000-${pad(n)}`,
    role,
    content,
    status: "COMPLETED",
    error_code: null,
    notice: null,
    created_at: "2026-11-20T10:00:00Z",
    sources: [],
    search_query: searchQuery,
  };
}

const REWRITTEN = "Quanto costa la spedizione del prodotto X?";
const firstQuestion = msg(1, "user", "Quanto costa il prodotto X?", null);
const firstAnswer = msg(2, "assistant", "Costa dieci euro.", null);
const followUp = msg(3, "user", "e la spedizione?", null);
const rewrittenAnswer = msg(4, "assistant", "La spedizione costa cinque euro.", REWRITTEN);
const exchange = [firstQuestion, firstAnswer, followUp, rewrittenAnswer];

/** Mock only the network boundary: unknown routes fail the request. */
function mockApi(routes: Record<string, Route>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const key = `${init?.method ?? "GET"} ${new URL(raw, "http://localhost").pathname}`;
      const route = routes[key];
      if (!route) throw new Error(`unexpected request ${key}`);
      return route();
    }),
  );
}

/** A server-sent stream that the test feeds event by event. */
function sseStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    event: (name: string, data: unknown) =>
      controller.enqueue(encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)),
    end: () => controller.close(),
  };
}

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

const messageList = () => screen.getByRole("list", { name: /^(Messaggi|Messages)$/ });
const items = () => within(messageList()).getAllByRole("listitem");
const noKeysOnScreen = () => expect(document.body.textContent).not.toMatch(/\b(chat|errors)\.[A-Za-z_]+/);

/** The line is under the answer: in the item of the answer, after its text. */
function expectLineUnder(item: HTMLElement, answer: string, line: string) {
  expect(within(item).getByText(line)).toBeInTheDocument();
  const text = item.textContent ?? "";
  expect(text.indexOf(answer)).toBeGreaterThanOrEqual(0);
  expect(text.indexOf(line)).toBeGreaterThan(text.indexOf(answer));
}

beforeEach(() => {
  window.history.pushState({}, "", "/");
  browserLanguages(["it-IT"]);
  void i18n.changeLanguage("it");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  void i18n.changeLanguage("it");
});

it("AC-013: an answer with a search_query shows «Ricerca: <testo>» under it, and only under it", () => {
  render(<ConversationView conversation={{ id: conversation.id, messages: exchange }} readOnly={false} />);

  const [question, answer, second, rewritten] = items();
  expectLineUnder(rewritten, "La spedizione costa cinque euro.", `Ricerca: ${REWRITTEN}`);
  expect(screen.getAllByText(/^Ricerca:/)).toHaveLength(1);
  for (const item of [question, answer, second]) expect(item).not.toHaveTextContent("Ricerca:");
  noKeysOnScreen();
});

it("AC-013: in English the line is «Search: <text>»", async () => {
  await act(async () => {
    await i18n.changeLanguage("en");
  });
  render(<ConversationView conversation={{ id: conversation.id, messages: exchange }} readOnly />);

  expectLineUnder(items()[3], "La spedizione costa cinque euro.", `Search: ${REWRITTEN}`);
  expect(screen.queryByText(/Ricerca:/)).not.toBeInTheDocument();
  noKeysOnScreen();
});

it("AC-013: an answer whose search_query is null or empty shows no line", () => {
  const nullQuery = msg(4, "assistant", "La spedizione costa cinque euro.", null);
  const emptyQuery = msg(6, "assistant", "La garanzia dura due anni.", "");
  const messages = [
    firstQuestion,
    firstAnswer,
    followUp,
    nullQuery,
    msg(5, "user", "e la garanzia?", null),
    emptyQuery,
  ];
  render(<ConversationView conversation={{ id: conversation.id, messages }} readOnly={false} />);

  expect(screen.getByText("La spedizione costa cinque euro.")).toBeInTheDocument();
  expect(screen.getByText("La garanzia dura due anni.")).toBeInTheDocument();
  expect(messageList()).not.toHaveTextContent("Ricerca");
  expect(messageList()).not.toHaveTextContent("Search");
  noKeysOnScreen();
});

it("AC-013: the author opening the conversation in the chat sees «Ricerca: <testo>» under the rewritten answer", async () => {
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    [`GET /api/kbs/${manuali.id}`]: () => json(200, manuali),
    [`GET /api/kbs/${manuali.id}/conversations`]: () => json(200, [conversation]),
    [`GET /api/conversations/${conversation.id}`]: () =>
      json(200, { ...conversation, messages: exchange, read_only: false }),
  });
  window.history.pushState({}, "", `/kbs/${manuali.id}`);
  render(<App />);
  const list = await screen.findByRole("navigation", { name: "Conversazioni" });
  fireEvent.click(await within(list).findByRole("button", { name: conversation.title }));

  expect(await screen.findByText(`Ricerca: ${REWRITTEN}`)).toBeInTheDocument();
  expectLineUnder(items()[3], "La spedizione costa cinque euro.", `Ricerca: ${REWRITTEN}`);
  expect(items()[1]).not.toHaveTextContent("Ricerca:");
  noKeysOnScreen();
});

it("AC-013: the answer of a follow-up question shows «Ricerca: <testo>» once the stream ends with done", async () => {
  const stream = sseStream();
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    [`GET /api/kbs/${manuali.id}`]: () => json(200, manuali),
    [`GET /api/kbs/${manuali.id}/conversations`]: () => json(200, [conversation]),
    [`GET /api/conversations/${conversation.id}`]: () =>
      json(200, { ...conversation, messages: [firstQuestion, firstAnswer], read_only: false }),
    [`POST /api/conversations/${conversation.id}/messages`]: () => stream.response,
  });
  window.history.pushState({}, "", `/kbs/${manuali.id}`);
  render(<App />);
  const list = await screen.findByRole("navigation", { name: "Conversazioni" });
  fireEvent.click(await within(list).findByRole("button", { name: conversation.title }));
  expect(await screen.findByText("Costa dieci euro.")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Domanda"), { target: { value: "e la spedizione?" } });
  fireEvent.click(screen.getByRole("button", { name: "Invia" }));
  act(() => stream.event("conversation", { conversation_id: conversation.id, message_id: rewrittenAnswer.id }));
  act(() => stream.event("delta", { text: "La spedizione costa cinque euro." }));
  expect(await screen.findByText("La spedizione costa cinque euro.")).toBeInTheDocument();
  expect(screen.queryByText(/Ricerca:/)).not.toBeInTheDocument(); // not before the end

  act(() => stream.event("done", { message: rewrittenAnswer }));
  act(() => stream.end());

  expect(await screen.findByText(`Ricerca: ${REWRITTEN}`)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Domanda")).toBeEnabled());
  expectLineUnder(items()[3], "La spedizione costa cinque euro.", `Ricerca: ${REWRITTEN}`);
});

it("AC-013: the admin opening the conversation in the page «Conversazioni» sees «Ricerca: <testo>», read-only", async () => {
  mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/kbs": () => json(200, [manuali]),
    "GET /api/admin/users": () => json(200, [anna, mario]),
    "GET /api/admin/conversations": () => json(200, [adminRow]),
    [`GET /api/conversations/${conversation.id}`]: () =>
      json(200, { ...conversation, messages: exchange, read_only: true }),
  });
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);
  await screen.findByRole("heading", { name: "Conversazioni", level: 2 });
  fireEvent.click(await screen.findByRole("button", { name: conversation.title }));

  expect(await screen.findByText(`Ricerca: ${REWRITTEN}`)).toBeInTheDocument();
  expectLineUnder(items()[3], "La spedizione costa cinque euro.", `Ricerca: ${REWRITTEN}`);
  expect(items()[1]).not.toHaveTextContent("Ricerca:");
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  noKeysOnScreen();
});

it("AC-013: the admin with an English browser sees «Search: <text>» in the page Conversations", async () => {
  browserLanguages(["en-US"]);
  mockApi({
    "GET /api/auth/me": () => json(200, anna),
    "GET /api/kbs": () => json(200, [manuali]),
    "GET /api/admin/users": () => json(200, [anna, mario]),
    "GET /api/admin/conversations": () => json(200, [adminRow]),
    [`GET /api/conversations/${conversation.id}`]: () =>
      json(200, { ...conversation, messages: exchange, read_only: true }),
  });
  window.history.pushState({}, "", "/admin/conversations");
  render(<App />);
  await screen.findByRole("heading", { name: "Conversations", level: 2 });
  fireEvent.click(await screen.findByRole("button", { name: conversation.title }));

  expect(await screen.findByText(`Search: ${REWRITTEN}`)).toBeInTheDocument();
  expect(screen.queryByText(/Ricerca:/)).not.toBeInTheDocument();
  noKeysOnScreen();
});
