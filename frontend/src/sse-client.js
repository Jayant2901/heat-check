// Thin wrapper around EventSource: dispatches each named SSE event type
// (wp_update / anomaly / game_end / degraded / heartbeat) to the matching
// handler, so callers never touch the raw EventSource API. This is the one
// place backend/app/api/stream.py's event contract is known on the frontend.
//
// On a dropped connection the browser reconnects by itself and sends the id of
// the last event it saw (Last-Event-ID), so the server replays only what was
// missed; the chart and list also ignore anything they already have.

export function connectStream(streamUrl, handlers) {
  const source = new EventSource(streamUrl);

  for (const eventType of ["wp_update", "anomaly", "game_end", "degraded", "heartbeat"]) {
    const handler = handlers[eventType];
    if (!handler) continue;
    source.addEventListener(eventType, (evt) => {
      let payload = {};
      try {
        payload = evt.data ? JSON.parse(evt.data) : {};
      } catch {
        return; // a malformed message must not take the page down
      }
      handler(payload);
    });
  }

  source.onopen = () => handlers.onopen?.();

  source.onerror = () => {
    if (!handlers.onerror) return;
    // readyState stays CONNECTING while the browser auto-retries (its
    // default behavior on a dropped connection, e.g. a host idling/timing
    // out an SSE connection) -- that's a transient blip, not a dead stream,
    // so callers can show "reconnecting" instead of something that reads as
    // broken. CLOSED means it's given up for good.
    const reconnecting = source.readyState === EventSource.CONNECTING;
    handlers.onerror({ reconnecting });
  };

  return source;
}
