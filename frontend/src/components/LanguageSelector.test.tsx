// SPEC-2026-11-06-fase-1-fondazioni
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import { App } from "../app/App";

type Route = (init: RequestInit | undefined) => Response;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const notAuthenticated = () => json(401, { error: { code: "not_authenticated", message: "Authentication required" } });

function user(language: "it" | "en" | null) {
  return {
    id: "8a1c7f9e-0000-4000-8000-000000000001",
    username: "mario",
    role: "user",
    is_active: true,
    language,
  };
}

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

/** A fake backend with one user: the profile language and the session survive across page loads. */
function fakeBackend() {
  const state = { language: null as "it" | "en" | null, signedIn: false };
  const calls = mockApi({
    "GET /api/auth/me": () => (state.signedIn ? json(200, user(state.language)) : notAuthenticated()),
    "POST /api/auth/login": () => {
      state.signedIn = true;
      return json(200, user(state.language));
    },
    "POST /api/auth/logout": () => {
      state.signedIn = false;
      return new Response(null, { status: 204 });
    },
    "PATCH /api/auth/me": (init) => {
      if (!state.signedIn) return notAuthenticated();
      const body = JSON.parse(String(init?.body)) as { language: "it" | "en" };
      state.language = body.language;
      return json(200, user(state.language));
    },
  });
  return { state, calls };
}

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

function signIn(labels: { username: string; submit: string }) {
  fireEvent.change(screen.getByLabelText(labels.username), { target: { value: "mario" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct-horse-battery" } });
  fireEvent.click(screen.getByRole("button", { name: labels.submit }));
}

function patchBodies(calls: { key: string; init: RequestInit | undefined }[]): unknown[] {
  return calls.filter((call) => call.key === "PATCH /api/auth/me").map((call) => JSON.parse(String(call.init?.body)));
}

beforeEach(() => {
  window.history.pushState({}, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-028: English chosen, sign out, sign in again from an Italian browser: the UI is in English", async () => {
  browserLanguages(["it-IT", "en-US"]);
  const { state, calls } = fakeBackend();
  render(<App />);

  await screen.findByLabelText("Nome utente");
  signIn({ username: "Nome utente", submit: "Accedi" });
  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Esci" })).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Lingua"), { target: { value: "en" } });

  expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
  expect(patchBodies(calls)).toEqual([{ language: "en" }]);
  expect(state.language).toBe("en");

  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await screen.findByRole("button", { name: /^(Sign in|Accedi)$/ });
  cleanup();

  // A new page load from the Italian browser: the login page follows the browser, the profile wins after sign-in.
  render(<App />);
  await screen.findByLabelText("Nome utente");
  signIn({ username: "Nome utente", submit: "Accedi" });

  expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
  expect(screen.getByText("You are signed in as mario.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Esci" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("Language")).toHaveValue("en");
  expect(document.documentElement.lang).toBe("en");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-028: the selector on the login page changes the language without reloading and saves nothing", async () => {
  browserLanguages(["it-IT"]);
  const { calls } = fakeBackend();
  render(<App />);

  const username = await screen.findByLabelText("Nome utente");
  fireEvent.change(username, { target: { value: "mario" } });
  expect(screen.getByLabelText("Lingua")).toHaveValue("it");

  fireEvent.change(screen.getByLabelText("Lingua"), { target: { value: "en" } });

  expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  // Same page, same form state: nothing was reloaded.
  expect(screen.getByLabelText("Username")).toBe(username);
  expect(screen.getByLabelText("Username")).toHaveValue("mario");
  expect(screen.getByLabelText("Language")).toHaveValue("en");
  expect(calls.map((call) => call.key)).toEqual(["GET /api/auth/me"]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-028: the selector offers exactly Italian and English", async () => {
  browserLanguages(["en-US"]);
  fakeBackend();
  render(<App />);

  const selector = await screen.findByLabelText("Language");
  const options = within(selector).getAllByRole("option");
  expect(options.map((option) => [(option as HTMLOptionElement).value, option.textContent])).toEqual([
    ["it", "Italiano"],
    ["en", "English"],
  ]);
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-028: after sign-in, choosing Italian saves it and switches the UI without reloading", async () => {
  browserLanguages(["en-US"]);
  const { state, calls } = fakeBackend();
  state.signedIn = true;
  state.language = "en";
  render(<App />);

  const signOut = await screen.findByRole("button", { name: "Sign out" });
  fireEvent.change(screen.getByLabelText("Language"), { target: { value: "it" } });

  expect(await screen.findByRole("button", { name: "Esci" })).toBe(signOut);
  expect(screen.getByText("Hai effettuato l'accesso come mario.")).toBeInTheDocument();
  expect(screen.getByLabelText("Lingua")).toHaveValue("it");
  expect(patchBodies(calls)).toEqual([{ language: "it" }]);
  const patch = calls.find((call) => call.key === "PATCH /api/auth/me");
  expect(new Headers(patch?.init?.headers).get("Content-Type")).toBe("application/json");
  expect(state.language).toBe("it");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-028: a language saved in the profile replaces the browser language when the session is open", async () => {
  browserLanguages(["it-IT"]);
  const { state } = fakeBackend();
  state.signedIn = true;
  state.language = "en";
  render(<App />);

  expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
  expect(screen.getByLabelText("Language")).toHaveValue("en");
});

it("SPEC-2026-11-06-fase-1-fondazioni/AC-028: if saving the language fails, the UI says so in the chosen language", async () => {
  browserLanguages(["it-IT"]);
  mockApi({
    "GET /api/auth/me": () => json(200, user(null)),
    "PATCH /api/auth/me": () => new Response("<html>Bad gateway</html>", { status: 502 }),
  });
  render(<App />);

  await screen.findByRole("button", { name: "Esci" });
  fireEvent.change(screen.getByLabelText("Lingua"), { target: { value: "en" } });

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Something went wrong. Please try again.");
  expect(alert).not.toHaveTextContent("Bad gateway");
});
