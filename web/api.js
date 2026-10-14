/**
 * API client for the Agent HTTP boundary.
 *
 * The tenant API key is the caller's own credential and lives in localStorage.
 * The model provider key never reaches the browser: it stays in the server
 * process environment only.
 */

const STORAGE_KEY = "weather-agent:api-key";
const API_PREFIX = "/v1";

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message || code);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export function getApiKey() {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setApiKey(value) {
  try {
    if (value) localStorage.setItem(STORAGE_KEY, value);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage may be unavailable (private mode); the app still works per-session */
  }
}

export function hasApiKey() {
  return Boolean(getApiKey());
}

function authHeader() {
  const key = getApiKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

async function parseError(response) {
  let code = `http_${response.status}`;
  let message = `请求失败（HTTP ${response.status}）`;
  try {
    const body = await response.json();
    if (body && body.error) {
      code = body.error.code || code;
      message = body.error.message || message;
    }
  } catch {
    /* non-JSON error body: keep the generic message */
  }
  return new ApiError(code, message, response.status);
}

async function request(path, { method = "GET", body } = {}) {
  const headers = { ...authHeader() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`${API_PREFIX}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return null;
  return response.json();
}

/* ---------- sessions ---------- */

export const listSessions = (limit = 50) => request(`/sessions?limit=${limit}`);

export const createSession = (title) =>
  request("/sessions", { method: "POST", body: { title: title ?? null } });

export const deleteSession = (sessionId) =>
  request(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });

export const sessionMessages = (sessionId) =>
  request(`/sessions/${encodeURIComponent(sessionId)}/messages`);

export const cancelRun = (runId) =>
  request(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });

export async function health() {
  const response = await fetch(`${API_PREFIX}/health`);
  if (!response.ok) throw await parseError(response);
  return response.json();
}

/* ---------- streaming chat ---------- */

/**
 * Send one chat turn and consume the Server-Sent Event stream.
 *
 * The payload is parsed incrementally, so events are delivered as soon as the
 * server flushes them. Returning never mixes turns: `onEvent` only receives
 * events for this request.
 */
export async function streamChat({ sessionId, message, signal, onEvent }) {
  const response = await fetch(`${API_PREFIX}/chat`, {
    method: "POST",
    headers: {
      ...authHeader(),
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ session_id: sessionId, message }),
    signal,
  });

  if (!response.ok) throw await parseError(response);
  if (!response.body) throw new ApiError("no_stream", "浏览器不支持流式响应。", 0);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const handleFrame = (frame) => {
    const dataLine = frame
      .split("\n")
      .find((line) => line.startsWith("data: "));
    if (!dataLine) return;
    try {
      onEvent(JSON.parse(dataLine.slice(6)));
    } catch {
      /* ignore malformed frame instead of breaking the stream */
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleFrame(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) handleFrame(buffer);
}
