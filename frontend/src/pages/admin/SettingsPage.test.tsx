// SPEC-2026-11-20-fase-4-rerank-rewrite
// The admin page «Impostazioni» (REQ-012): two switches, «Riscrittura della domanda» and «Reranker», that show the saved
// state and save it at each change; the switch of a service that is not configured is disabled with «Non configurato»;
// a refused save puts the switch back and shows the translated reason; the texts exist in Italian and English; a `user`
// has no menu entry and the route takes them to `/`, no session to `/login`: AC-021, AC-022, AC-023.
// AC-028 (the page through Nginx with the fakes, then the chat) is the end-to-end test backend/tests/e2e/test_settings_ui.py.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { App } from "../../app/App";
import { i18n } from "../../i18n";

type UserOut = { id: string; username: string; role: "admin" | "user"; is_active: boolean; language: null };
type KbOut = { id: string; name: string; description: string | null; created_at: string };
type Step = { enabled: boolean; configured: boolean };
type SettingsOut = { rerank: Step; rewrite: Step };
type Route = (init: RequestInit | undefined) => Response | Promise<Response>;
type Call = { key: string; init: RequestInit | undefined };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const apiError = (status: number, code: string) => () => json(status, { error: { code, message: "English text" } });

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

const SETTINGS = "/api/admin/settings";
const GET = `GET ${SETTINGS}`;
const PUT = `PUT ${SETTINGS}`;

const IT = {
  title: "Impostazioni",
  rewrite: "Riscrittura della domanda",
  rewriteHelp: "Riformula una domanda di seguito come domanda autonoma prima della ricerca.",
  rerank: "Reranker",
  rerankHelp: "Riordina i passaggi trovati prima della risposta.",
  notConfigured: "Non configurato",
  serviceNotConfigured: "Il servizio di questo passo non è configurato.",
};
const EN = {
  title: "Settings",
  rewrite: "Question rewriting",
  rewriteHelp: "Rewrites a follow-up question as a standalone question before the search.",
  rerank: "Reranker",
  rerankHelp: "Reorders the retrieved passages before the answer.",
  notConfigured: "Not configured",
  serviceNotConfigured: "The service of this step is not configured.",
};

/** Mock only the network boundary: every call is recorded, unknown routes fail the request. */
function mockApi(routes: Record<string, Route>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const key = `${init?.method ?? "GET"} ${new URL(raw, "http://localhost").pathname}`;
      calls.push({ key, init });
      const route = routes[key];
      if (!route) throw new Error(`unexpected request ${key}`);
      return route(init);
    }),
  );
  return calls;
}

const keys = (calls: Call[]) => calls.map((call) => call.key);
const callsOf = (calls: Call[], key: string) => calls.filter((call) => call.key === key);
const bodyOf = (call: Call) => JSON.parse(call.init?.body as string) as unknown;
const putBodies = (calls: Call[]) => callsOf(calls, PUT).map(bodyOf);

/** The backend of the settings: GET returns `server.state`; PUT applies the fields it gets, as the backend does
 * (409 `service_not_configured` for switching on a step that is not configured, and then nothing is saved). */
function settingsBackend(server: { state: SettingsOut }) {
  return {
    [GET]: () => json(200, server.state),
    [PUT]: (init: RequestInit | undefined) => {
      const body = JSON.parse(init?.body as string) as { rerank_enabled?: boolean; rewrite_enabled?: boolean };
      const { rerank, rewrite } = server.state;
      if ((body.rerank_enabled && !rerank.configured) || (body.rewrite_enabled && !rewrite.configured)) {
        return apiError(409, "service_not_configured")();
      }
      server.state = {
        rerank: { ...rerank, enabled: body.rerank_enabled ?? rerank.enabled },
        rewrite: { ...rewrite, enabled: body.rewrite_enabled ?? rewrite.enabled },
      };
      return json(200, server.state);
    },
  } satisfies Record<string, Route>;
}

function adminRoutes(extra: Record<string, Route>) {
  return mockApi({ "GET /api/auth/me": () => json(200, anna), "GET /api/kbs": () => json(200, [manuali]), ...extra });
}

function browserLanguages(languages: string[]) {
  vi.spyOn(window.navigator, "languages", "get").mockReturnValue(languages);
  vi.spyOn(window.navigator, "language", "get").mockReturnValue(languages[0]);
}

async function openPage(heading = IT.title) {
  window.history.pushState({}, "", "/admin/settings");
  render(<App />);
  await screen.findByRole("heading", { name: heading, level: 2 });
}

const toggle = (name: string) => screen.findByRole("switch", { name });
const noKeysOnScreen = () => expect(document.body.textContent).not.toMatch(/\b(settings|errors)\.[A-Za-z_]+/);

/** A response the test releases when it wants (to see the switch while its save is in flight). */
function held() {
  let release: (response: Response) => void = () => undefined;
  const promise = new Promise<Response>((resolve) => {
    release = resolve;
  });
  return { route: () => promise, release };
}

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

// AC-021 ---------------------------------------------------------------------------------------------------------------

it("AC-021: with the rewriting configured and the reranker not, the rewriting switch is active and the reranker one is disabled with «Non configurato»", async () => {
  const server = {
    state: { rerank: { enabled: false, configured: false }, rewrite: { enabled: false, configured: true } },
  };
  const calls = adminRoutes(settingsBackend(server));
  await openPage();

  const rewrite = await toggle(IT.rewrite);
  const rerank = await toggle(IT.rerank);
  expect(rewrite).toBeEnabled();
  expect(rewrite).not.toBeChecked();
  expect(rerank).toBeDisabled();
  expect(rerank).not.toBeChecked();
  expect(screen.getAllByText(IT.notConfigured)).toHaveLength(1);
  expect(rerank).toHaveAccessibleDescription(expect.stringContaining(IT.notConfigured));
  expect(rewrite).not.toHaveAccessibleDescription(expect.stringContaining(IT.notConfigured));
  expect(screen.getByText(IT.rewriteHelp)).toBeInTheDocument();
  expect(screen.getByText(IT.rerankHelp)).toBeInTheDocument();
  expect(callsOf(calls, GET)).toHaveLength(1);
  expect(callsOf(calls, PUT)).toHaveLength(0); // opening the page saves nothing
  noKeysOnScreen();
});

it("AC-021: switching on the rewriting saves it at once, and it is still on after the page is loaded again", async () => {
  const server = {
    state: { rerank: { enabled: false, configured: false }, rewrite: { enabled: false, configured: true } },
  };
  const calls = adminRoutes(settingsBackend(server));
  await openPage();

  fireEvent.click(await toggle(IT.rewrite));

  await waitFor(() => expect(putBodies(calls)).toEqual([{ rewrite_enabled: true }]));
  await waitFor(async () => expect(await toggle(IT.rewrite)).toBeChecked());
  expect(server.state.rewrite.enabled).toBe(true);
  expect(server.state.rerank.enabled).toBe(false);
  expect(screen.queryByText(IT.serviceNotConfigured)).not.toBeInTheDocument();

  cleanup(); // a new load of the page
  await openPage();
  expect(await toggle(IT.rewrite)).toBeChecked();
  expect(await toggle(IT.rerank)).not.toBeChecked();
  expect(callsOf(calls, GET)).toHaveLength(2);
  expect(callsOf(calls, PUT)).toHaveLength(1);
});

it("AC-021: each switch saves only its own field, on and then off", async () => {
  const server = {
    state: { rerank: { enabled: false, configured: true }, rewrite: { enabled: true, configured: true } },
  };
  const calls = adminRoutes(settingsBackend(server));
  await openPage();
  expect(await toggle(IT.rewrite)).toBeChecked(); // the saved state
  expect(await toggle(IT.rerank)).not.toBeChecked();
  expect(screen.queryByText(IT.notConfigured)).not.toBeInTheDocument();

  fireEvent.click(await toggle(IT.rerank));
  await waitFor(async () => expect(await toggle(IT.rerank)).toBeChecked());
  await waitFor(() => expect(putBodies(calls)).toEqual([{ rerank_enabled: true }]));

  fireEvent.click(await toggle(IT.rewrite));
  await waitFor(async () => expect(await toggle(IT.rewrite)).not.toBeChecked());
  await waitFor(() => expect(putBodies(calls)).toEqual([{ rerank_enabled: true }, { rewrite_enabled: false }]));

  fireEvent.click(await toggle(IT.rerank));
  await waitFor(() =>
    expect(putBodies(calls)).toEqual([{ rerank_enabled: true }, { rewrite_enabled: false }, { rerank_enabled: false }]),
  );
  await waitFor(async () => expect(await toggle(IT.rerank)).not.toBeChecked());
  expect(server.state).toEqual({
    rerank: { enabled: false, configured: true },
    rewrite: { enabled: false, configured: true },
  });
});

it("AC-021: a switch left on whose service is no longer configured shows the saved state, disabled with «Non configurato»", async () => {
  adminRoutes(
    settingsBackend({
      state: { rerank: { enabled: true, configured: false }, rewrite: { enabled: true, configured: true } },
    }),
  );
  await openPage();

  const rerank = await toggle(IT.rerank);
  expect(rerank).toBeChecked();
  expect(rerank).toBeDisabled();
  expect(rerank).toHaveAccessibleDescription(expect.stringContaining(IT.notConfigured));
  expect(await toggle(IT.rewrite)).toBeEnabled();
});

it("AC-021: settings that cannot be read show the translated error without an alert and no switch", async () => {
  adminRoutes({ [GET]: apiError(500, "internal_error") });
  await openPage();

  expect(await screen.findByText("Si è verificato un errore. Riprova.")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // the header selector owns the alert
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  noKeysOnScreen();
});

it("AC-021: the admin sees the menu entry «Impostazioni» and opens the page from it", async () => {
  adminRoutes(
    settingsBackend({
      state: { rerank: { enabled: false, configured: true }, rewrite: { enabled: false, configured: true } },
    }),
  );
  render(<App />);

  const banner = await screen.findByRole("banner");
  fireEvent.click(await within(banner).findByRole("link", { name: "Impostazioni" }));

  expect(await screen.findByRole("heading", { name: IT.title, level: 2 })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/admin/settings");
  expect(await toggle(IT.rewrite)).toBeEnabled();
});

// AC-022 ---------------------------------------------------------------------------------------------------------------

it("AC-022: a save refused with 409 puts the switch back to its previous state and shows the translated reason", async () => {
  const put = held();
  const calls = adminRoutes({
    [GET]: () =>
      json(200, { rerank: { enabled: false, configured: true }, rewrite: { enabled: false, configured: true } }),
    [PUT]: put.route,
  });
  await openPage();

  fireEvent.click(await toggle(IT.rerank));
  await waitFor(async () => expect(await toggle(IT.rerank)).toBeChecked()); // the change, while it is saved
  expect(putBodies(calls)).toEqual([{ rerank_enabled: true }]);

  put.release(apiError(409, "service_not_configured")());

  expect(await screen.findByText(IT.serviceNotConfigured)).toBeInTheDocument();
  await waitFor(async () => expect(await toggle(IT.rerank)).not.toBeChecked());
  expect(await toggle(IT.rewrite)).not.toBeChecked();
  noKeysOnScreen();
});

it("AC-022: switching off a step that the backend refuses puts it back on", async () => {
  adminRoutes({
    [GET]: () =>
      json(200, { rerank: { enabled: true, configured: true }, rewrite: { enabled: true, configured: true } }),
    [PUT]: apiError(409, "service_not_configured"),
  });
  await openPage();

  fireEvent.click(await toggle(IT.rewrite));

  expect(await screen.findByText(IT.serviceNotConfigured)).toBeInTheDocument();
  await waitFor(async () => expect(await toggle(IT.rewrite)).toBeChecked());
  expect(await toggle(IT.rerank)).toBeChecked();
});

it("AC-022: a save without a connection puts the switch back and shows the network error", async () => {
  adminRoutes({
    [GET]: () =>
      json(200, { rerank: { enabled: false, configured: true }, rewrite: { enabled: false, configured: true } }),
    [PUT]: () => Promise.reject(new TypeError("Failed to fetch")),
  });
  await openPage();

  fireEvent.click(await toggle(IT.rewrite));

  expect(await screen.findByText("Il server non è raggiungibile. Riprova.")).toBeInTheDocument();
  await waitFor(async () => expect(await toggle(IT.rewrite)).not.toBeChecked());
});

it("AC-022: a save refused for another reason puts the switch back and shows the generic error", async () => {
  adminRoutes({
    [GET]: () =>
      json(200, { rerank: { enabled: false, configured: true }, rewrite: { enabled: false, configured: true } }),
    [PUT]: apiError(500, "internal_error"),
  });
  await openPage();

  fireEvent.click(await toggle(IT.rerank));

  expect(await screen.findByText("Si è verificato un errore. Riprova.")).toBeInTheDocument();
  await waitFor(async () => expect(await toggle(IT.rerank)).not.toBeChecked());
  noKeysOnScreen();
});

it("AC-022: with the UI in English the page, its menu entry, «Not configured» and the reason of a refusal are in English", async () => {
  browserLanguages(["en-US"]);
  adminRoutes({
    [GET]: () =>
      json(200, { rerank: { enabled: false, configured: false }, rewrite: { enabled: false, configured: true } }),
    [PUT]: apiError(409, "service_not_configured"),
  });
  await openPage(EN.title);

  const banner = screen.getByRole("banner");
  expect(within(banner).getByRole("link", { name: "Settings" })).toBeInTheDocument();
  const rewrite = await toggle(EN.rewrite);
  const rerank = await toggle(EN.rerank);
  expect(rerank).toBeDisabled();
  expect(rerank).toHaveAccessibleDescription(expect.stringContaining(EN.notConfigured));
  expect(screen.getByText(EN.notConfigured)).toBeInTheDocument();
  expect(screen.getByText(EN.rewriteHelp)).toBeInTheDocument();
  expect(screen.getByText(EN.rerankHelp)).toBeInTheDocument();

  fireEvent.click(rewrite);

  expect(await screen.findByText(EN.serviceNotConfigured)).toBeInTheDocument();
  await waitFor(async () => expect(await toggle(EN.rewrite)).not.toBeChecked());
  for (const text of Object.values(IT).filter((value) => value !== "Reranker")) {
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  }
  noKeysOnScreen();
});

// AC-023 ---------------------------------------------------------------------------------------------------------------

it("AC-023: a user sees no menu entry «Impostazioni» and the route /admin/settings takes them to /", async () => {
  const calls = mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  window.history.pushState({}, "", "/admin/settings");
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Manuali" })).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Impostazioni" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: IT.title })).not.toBeInTheDocument();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(window.location.pathname).toBe("/");
  expect(keys(calls).filter((key) => key !== "GET /api/auth/me" && key !== "GET /api/kbs")).toEqual([]);
});

it("AC-023: a user with the interface in English sees no menu entry «Settings»", async () => {
  browserLanguages(["en-US"]);
  mockApi({
    "GET /api/auth/me": () => json(200, mario),
    "GET /api/kbs": () => json(200, [manuali]),
  });
  render(<App />);

  const banner = await screen.findByRole("banner");
  expect(await within(banner).findByText("mario")).toBeInTheDocument();
  expect(within(banner).queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
});

it("AC-023: without a session the route /admin/settings goes to the login page", async () => {
  const calls = mockApi({ "GET /api/auth/me": apiError(401, "not_authenticated") });
  window.history.pushState({}, "", "/admin/settings");
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Accedi" })).toBeInTheDocument();
  expect(window.location.pathname).toBe("/login");
  expect(keys(calls)).toEqual(["GET /api/auth/me"]);
});
