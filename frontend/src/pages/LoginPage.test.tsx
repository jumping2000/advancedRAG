// SPEC-2026-11-06-fase-1-fondazioni
import { fireEvent, render, screen, within } from "@testing-library/react";

import { App } from "../app/App";

type Route = (init: RequestInit | undefined) => Response;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const notAuthenticated = () => json(401, { error: { code: "not_authenticated", message: "Authentication required" } });
const invalidCredentials = () =>
  json(401, { error: { code: "invalid_credentials", message: "Invalid username or password" } });
const mario = {
  id: "8a1c7f9e-0000-4000-8000-000000000001",
  username: "mario",
  role: "user",
  is_active: true,
  language: null,
};

/** Mock only the network boundary: every call is recorded, unknown routes fail the test. */
function mockApi(routes: Record<string, Route>) {
  const calls: { key: string; init: RequestInit | undefined }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const key = `${init?.method ?? "GET"} ${new URL(url, "http://localhost").pathname}`;
    calls.push({ key, init });
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

function fillAndSubmit(username: string, password: string, labels: { username: string; submit: string }) {
  fireEvent.change(screen.getByLabelText(labels.username), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: labels.submit }));
}

beforeEach(() => {
  window.history.pushState({}, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-027: with browser language it-IT the login page is in Italian", async () => {
  browserLanguages(["it-IT", "en-US"]);
  mockApi({ "GET /api/auth/me": notAuthenticated });
  render(<App />);

  expect(await screen.findByLabelText("Nome utente")).toBeInTheDocument();
  expect(screen.getByLabelText("Password")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Accedi" })).toBeInTheDocument();
  expect(screen.queryByText("Username")).not.toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-027: with browser language de-DE the login page is in English", async () => {
  browserLanguages(["de-DE", "it-IT"]);
  mockApi({ "GET /api/auth/me": notAuthenticated });
  render(<App />);

  expect(await screen.findByLabelText("Username")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  expect(screen.queryByText("Nome utente")).not.toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-027: only the first browser language counts (en-US before it-IT is English)", async () => {
  browserLanguages(["en-US", "it-IT"]);
  mockApi({ "GET /api/auth/me": notAuthenticated });
  render(<App />);

  expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-027: a bare 'it' browser language is Italian", async () => {
  browserLanguages(["it"]);
  mockApi({ "GET /api/auth/me": notAuthenticated });
  render(<App />);

  expect(await screen.findByRole("button", { name: "Accedi" })).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-011: wrong credentials show the generic message in the UI language", async () => {
  browserLanguages(["it-IT"]);
  mockApi({ "GET /api/auth/me": notAuthenticated, "POST /api/auth/login": invalidCredentials });
  render(<App />);

  await screen.findByLabelText("Nome utente");
  fillAndSubmit("mario", "wrong-password-1", { username: "Nome utente", submit: "Accedi" });

  expect(await screen.findByRole("alert")).toHaveTextContent("Nome utente o password non validi.");
  expect(screen.getByRole("button", { name: "Accedi" })).toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-011: in English the same error is shown in English", async () => {
  browserLanguages(["en-GB"]);
  mockApi({ "GET /api/auth/me": notAuthenticated, "POST /api/auth/login": invalidCredentials });
  render(<App />);

  await screen.findByLabelText("Username");
  fillAndSubmit("nobody", "wrong-password-1", { username: "Username", submit: "Sign in" });

  expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password.");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-010: a successful login sends JSON and shows the signed-in user", async () => {
  browserLanguages(["it-IT"]);
  const calls = mockApi({
    "GET /api/auth/me": notAuthenticated,
    "POST /api/auth/login": () => json(200, mario),
  });
  render(<App />);

  await screen.findByLabelText("Nome utente");
  fillAndSubmit("mario", "correct-horse-battery", { username: "Nome utente", submit: "Accedi" });

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(screen.queryByLabelText("Nome utente")).not.toBeInTheDocument();

  const login = calls.find((call) => call.key === "POST /api/auth/login");
  expect(new Headers(login?.init?.headers).get("Content-Type")).toBe("application/json");
  expect(JSON.parse(String(login?.init?.body))).toEqual({ username: "mario", password: "correct-horse-battery" });
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-010: an existing session opens the app without the login page", async () => {
  browserLanguages(["en-US"]);
  mockApi({ "GET /api/auth/me": () => json(200, mario) });
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-012: logout calls the API and returns to the login page", async () => {
  browserLanguages(["it-IT"]);
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "POST /api/auth/logout": () => new Response(null, { status: 204 }),
  });
  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "Esci" }));

  expect(await screen.findByLabelText("Nome utente")).toBeInTheDocument();
  expect(calls.map((call) => call.key)).toContain("POST /api/auth/logout");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-011: an unexpected server error shows a generic message, not the raw text", async () => {
  browserLanguages(["en-US"]);
  mockApi({
    "GET /api/auth/me": notAuthenticated,
    "POST /api/auth/login": () => new Response("<html>Bad gateway</html>", { status: 502 }),
  });
  render(<App />);

  await screen.findByLabelText("Username");
  fillAndSubmit("mario", "correct-horse-battery", { username: "Username", submit: "Sign in" });

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Something went wrong. Please try again.");
  expect(alert).not.toHaveTextContent("Bad gateway");
});
