// Hash router. Routes:
//   #/                               dashboard
//   #/?section=<id>                  dashboard, scrolled to a section
//   #/live/<gameId>                  a live game
//   #/replay/<fixtureId>?speed=10    starts a replay session on entry
// Hash routing keeps refresh and the back button working with no server
// configuration. Each view returns a cleanup function, called on leave.

import { renderDashboard, scrollToSection } from "./dashboard.js";
import { renderGame } from "./game-view.js";

const app = document.getElementById("app");
let cleanup = null;
let dashboardMounted = false;

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
  dashboardMounted = false;

  const { parts, params } = parseHash();
  const [kind, id] = parts;

  if (kind === "live" && id) {
    cleanup = renderGame(app, { mode: "live", id, onMode: setSampleMode });
  } else if (kind === "replay" && id) {
    const speed = Number(params.get("speed")) || 10;
    cleanup = renderGame(app, { mode: "replay", id, speed, onMode: setSampleMode });
  } else {
    const section = params.get("section");
    cleanup = renderDashboard(app, { section, onMode: setSampleMode });
    dashboardMounted = true;
    if (!section) window.scrollTo(0, 0);
    return;
  }
  window.scrollTo(0, 0);
  app.focus({ preventScroll: true });
}

// On the dashboard, section links just scroll: no re-render, no refetch,
// and clicking the same link twice still works (the hash doesn't change,
// so no hashchange would fire). From a game page they route normally.
document.addEventListener("click", (event) => {
  if (!dashboardMounted || event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a[href^="#/?section="]');
  if (!link) return;
  event.preventDefault();
  const href = link.getAttribute("href");
  history.replaceState(null, "", href);
  scrollToSection(new URLSearchParams(href.split("?")[1]).get("section"));
});

window.addEventListener("hashchange", route);
route();
