// Hash router. Routes:
//   #/                         dashboard
//   #/live/<gameId>            a live game
//   #/replay/<fixtureId>?speed=10   starts a replay session on entry
// Hash routing keeps refresh and the back button working with no server
// configuration. Each view returns a cleanup function, called on leave.

import { renderDashboard } from "./dashboard.js";
import { renderGame } from "./game-view.js";

const app = document.getElementById("app");
let cleanup = null;

function parseHash() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, params: new URLSearchParams(query) };
}

export function setSampleMode(isSample) {
  document.getElementById("sample-pill").hidden = !isSample;
  document.getElementById("sample-banner").hidden = !isSample;
}

function route() {
  if (cleanup) {
    cleanup();
    cleanup = null;
  }
  app.replaceChildren();

  const { parts, params } = parseHash();
  const [kind, id] = parts;

  if (kind === "live" && id) {
    cleanup = renderGame(app, { mode: "live", id, onMode: setSampleMode });
  } else if (kind === "replay" && id) {
    const speed = Number(params.get("speed")) || 10;
    cleanup = renderGame(app, { mode: "replay", id, speed, onMode: setSampleMode });
  } else {
    cleanup = renderDashboard(app, { section: params.get("section"), onMode: setSampleMode });
    return;
  }
  window.scrollTo(0, 0);
  app.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);
route();
