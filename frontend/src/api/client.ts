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

export type StreamEvent = { type: "conversation" | "delta" | "done" | "error"; data: unknown };

const STREAM_EVENTS: string[] = ["conversation", "delta", "done", "error"];

/** One event of a server-sent stream (the lines between two blank lines); null for a comment (the keepalive), an
 * event of another name or data that is not JSON. */
function parseEvent(block: string): StreamEvent | null {
  let type = "";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") type = value;
    else if (field === "data") data.push(value);
  }
  if (!STREAM_EVENTS.includes(type) || data.length === 0) return null;
  try {
    return { type: type as StreamEvent["type"], data: JSON.parse(data.join("\n")) as unknown };
  } catch {
    return null;
  }
}

/** POST whose answer is a stream of server-sent events (fetch and a ReadableStream: EventSource only does GET). The
 * events reach `onEvent` as they arrive; the promise ends with the stream. An error status before the stream starts is
 * an ApiError like any other call; a stream that breaks halfway is an ApiError `network_error`; an abort by the
 * `signal` ends it quietly. */
async function stream(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { Accept: "text/event-stream", "X-Requested-With": "XMLHttpRequest" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: "POST",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      signal,
    });
  } catch {
    if (signal?.aborted) return;
    throw new ApiError(0, "network_error");
  }
  if (!response.ok) throw new ApiError(response.status, await errorCode(response));
  if (!response.body) throw new ApiError(0, "network_error");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch {
      if (signal?.aborted) return;
      throw new ApiError(0, "network_error");
    }
    if (chunk.done) return;
    // A "\r" that ends the chunk may be the first half of "\r\n": it waits for the next chunk
    buffer = (buffer + decoder.decode(chunk.value, { stream: true })).replace(/\r\n/g, "\n");
    const held = buffer.endsWith("\r") ? "\r" : "";
    buffer = buffer.slice(0, buffer.length - held.length).replace(/\r/g, "\n");
    for (let end = buffer.indexOf("\n\n"); end !== -1; end = buffer.indexOf("\n\n")) {
      const event = parseEvent(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (event) onEvent(event);
    }
    buffer += held;
  }
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
  /** POST (with a JSON body, or none) that answers with server-sent events: see `stream` above. */
  stream,
  /** One file as multipart/form-data in the field `file`. The browser sets the Content-Type with its boundary; the
   * header X-Requested-With is what the backend asks of the upload instead of a JSON body (CSRF). */
  upload: <T>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return send<T>("POST", path, { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }, form);
  },
};
