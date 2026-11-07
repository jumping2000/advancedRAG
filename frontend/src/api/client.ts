/** JSON client of the backend API. Errors arrive as {"error": {"code", "message"}}: the UI translates the code. */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string) {
    super(`API error ${status}: ${code}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function errorCode(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    const code = (body as { error?: { code?: unknown } } | null)?.error?.code;
    return typeof code === "string" ? code : "unknown";
  } catch {
    return "unknown";
  }
}

/** One request. The caller sets the headers that go with its body; an error status becomes an ApiError. */
async function send<T>(method: string, path: string, headers: Record<string, string>, body?: BodyInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { method, headers, body, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (!response.ok) throw new ApiError(response.status, await errorCode(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return send<T>(method, path, headers, body === undefined ? undefined : JSON.stringify(body));
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
  /** One file as multipart/form-data in the field `file`. The browser sets the Content-Type with its boundary; the
   * header X-Requested-With is what the backend asks of the upload instead of a JSON body (CSRF). */
  upload: <T>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return send<T>("POST", path, { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }, form);
  },
};
