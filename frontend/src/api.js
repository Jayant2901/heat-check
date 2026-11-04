// Every backend call the frontend makes. Errors carry the server's
// readable `detail` message when there is one, so views can show it.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" }, ...options });
  } catch {
    throw new ApiError("Can't reach the Heat Check server.", 0);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // not JSON; keep the status line
    }
    throw new ApiError(detail, response.status);
  }
  return response.json();
}

export const api = {
  scoreboard: () => request("/api/scoreboard"),
  week: () => request("/api/dashboard/week"),
  modelInfo: () => request("/api/model-info"),
  replays: () => request("/api/replays"),
  startReplay: (fixtureId, speed) =>
    request(`/api/replays/${encodeURIComponent(fixtureId)}/start?speed=${encodeURIComponent(speed)}`, {
      method: "POST",
    }),
};

export function liveStreamUrl(gameId) {
  return `/api/games/${encodeURIComponent(gameId)}/stream`;
}

// EventSource can't see HTTP status codes. When a stream fails before any
// data arrives, ask once with fetch to find out why (404, 429, 503...).
export async function explainStreamFailure(streamUrl) {
  const controller = new AbortController();
  try {
    const response = await fetch(streamUrl, { signal: controller.signal });
    if (response.ok) return null;
    try {
      const body = await response.json();
      return body.detail || `${response.status}`;
    } catch {
      return `${response.status} ${response.statusText}`;
    }
  } catch {
    return null;
  } finally {
    controller.abort();
  }
}
