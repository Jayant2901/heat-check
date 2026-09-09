// Thin wrapper around EventSource: dispatches each named SSE event type
// (wp_update / anomaly / game_end / degraded / heartbeat) to the matching
// handler, so callers never touch the raw EventSource API. This is the one
// place backend/app/api/stream.py's event contract is known on the frontend.

export function connectStream(streamUrl, handlers) {
  const source = new EventSource(streamUrl);

  for (const eventType of ["wp_update", "anomaly", "game_end", "degraded", "heartbeat"]) {
    const handler = handlers[eventType];
    if (!handler) continue;
    source.addEventListener(eventType, (evt) => {
      const payload = evt.data ? JSON.parse(evt.data) : {};
      handler(payload);
    });
  }

  source.onerror = () => {
    if (!handlers.onerror) return;
    // readyState stays CONNECTING while the browser auto-retries (its
    // default behavior on a dropped connection, e.g. Render's free tier
    // idling/timing out an SSE connection) -- that's a transient blip, not
    // a dead stream, so callers can show "reconnecting" instead of
    // something that reads as broken. CLOSED means it's given up for good.
    const reconnecting = source.readyState === EventSource.CONNECTING;
    handlers.onerror({ reconnecting });
  };

  return source;
}
