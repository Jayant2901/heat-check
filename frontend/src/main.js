import { renderGamePicker } from "./game-picker.js";
import { connectStream } from "./sse-client.js";
import { createWormChart } from "./worm-chart.js";
import { mountDunkHero } from "./hero-dunk.js";

const lobbyView = document.getElementById("lobby-view");
const gameView = document.getElementById("game-view");
const chartContainer = document.getElementById("worm-chart");
const matchupHeader = document.getElementById("matchup-header");
const anomalyFeed = document.getElementById("anomaly-feed");
const pickerContainer = document.getElementById("game-picker");
const chartLegend = document.getElementById("chart-legend");
const chartTooltip = document.getElementById("chart-tooltip");

let currentSource = null;
let chart = null;

function formatClock(period, clockSeconds) {
  if (period == null || clockSeconds == null) return "";
  const label = period <= 4 ? `Q${period}` : period === 5 ? "OT" : `${period - 4}OT`;
  const min = Math.floor(clockSeconds / 60);
  const sec = Math.floor(clockSeconds % 60).toString().padStart(2, "0");
  return `${label} · ${min}:${sec}`;
}

function shotClockProgress(clockSeconds) {
  if (clockSeconds == null) return 0;
  return Math.max(0, Math.min(1, clockSeconds / 720));
}

function renderMatchup(view) {
  const margin = (view.homeScore ?? 0) - (view.awayScore ?? 0);
  const momentum = Math.min(1, Math.abs(margin) / 12) * 50;
  const side = margin >= 0 ? "home" : "away";
  const ring = `${shotClockProgress(view.clockSeconds) * 360}deg`;
  matchupHeader.innerHTML = `
    <div class="score-team score-away"><span class="team-badge neutral">${view.awayTeam || "AWY"}</span><div><p class="team-label">Away</p><strong>${view.awayTeam || "AWAY"}</strong></div><b>${view.awayScore ?? "–"}</b></div>
    <div class="score-center"><span class="tag ${view.connected ? "tag-accent" : "tag-neutral"}"><i class="status-square"></i>${view.statusText}</span><div class="clock-row"><span class="clock-ring" style="--ring:${ring}"></span><code>${view.clockText || "TIP-OFF"}</code></div><div class="momentum"><div><span>AWAY</span><em>MOMENTUM</em><span>HOME</span></div><div class="momentum-track ${side}"><i style="--amount:${momentum}%"></i></div></div></div>
    <div class="score-team score-home"><b>${view.homeScore ?? "–"}</b><div><p class="team-label">Home</p><strong>${view.homeTeam || "HOME"}</strong></div><span class="team-badge accent">${view.homeTeam || "HME"}</span></div>`;
  chartLegend.innerHTML = `<span class="legend-home"><i></i>${view.homeTeam || "HOME"}</span><span class="legend-away"><i></i>${view.awayTeam || "AWAY"}</span>`;
}

function addAnomalyToFeed(anomaly, clockText) {
  let list = anomalyFeed.querySelector("ul");
  if (!list) {
    anomalyFeed.querySelector(".empty-state")?.remove();
    list = document.createElement("ul");
    anomalyFeed.appendChild(list);
  }
  const row = document.createElement("li");
  row.innerHTML = `<span class="tag tag-accent">${anomaly.z_score.toFixed(1)}σ</span><span>${anomaly.message}</span><code>${clockText || ""}</code>`;
  list.prepend(row);
  while (list.children.length > 10) list.lastElementChild.remove();
}

function showGameView() {
  lobbyView.hidden = true;
  gameView.hidden = false;
  window.scrollTo({ top: 0, behavior: "auto" });
  if (!chart) chart = createWormChart(chartContainer, (message) => { chartTooltip.textContent = message; });
}

function showLobby() {
  currentSource?.close();
  currentSource = null;
  chart?.destroy();
  chart = null;
  chartContainer.replaceChildren();
  lobbyView.hidden = false;
  gameView.hidden = true;
  document.getElementById("watch").scrollIntoView({ behavior: "smooth", block: "start" });
}

function watch(streamUrl, label, meta = {}) {
  currentSource?.close();
  showGameView();
  chart.reset();
  anomalyFeed.innerHTML = `<div class="feed-header"><div><p class="card-kicker">Run monitor</p><h2 id="heat-title">Heat checks</h2></div><span class="tag tag-outline">Latest first</span></div><p class="empty-state">No heat checks yet.</p>`;
  const view = { awayTeam: meta.awayTeam, homeTeam: meta.homeTeam, awayScore: null, homeScore: null, clockText: "", clockSeconds: null, statusText: "Connecting", connected: false };
  renderMatchup(view);
  currentSource = connectStream(streamUrl, {
    wp_update: (payload) => {
      Object.assign(view, { awayScore: payload.away_score, homeScore: payload.home_score, awayTeam: payload.away_team || view.awayTeam, homeTeam: payload.home_team || view.homeTeam, connected: true, statusText: "Live replay", clockSeconds: payload.clock ?? view.clockSeconds });
      if (payload.period != null) view.clockText = formatClock(payload.period, payload.clock);
      renderMatchup(view); chart.addPoint(payload);
    },
    anomaly: (payload) => { chart.addAnomaly(payload); addAnomalyToFeed(payload, view.clockText); },
    game_end: () => { view.statusText = "Final"; view.connected = false; view.clockText = "FINAL"; renderMatchup(view); },
    degraded: () => { view.statusText = "Reconnecting"; view.connected = false; renderMatchup(view); },
    onerror: ({ reconnecting }) => { view.statusText = reconnecting ? "Reconnecting" : "Connection lost"; view.connected = false; renderMatchup(view); },
  });
}

function initialiseReveal() {
  const observer = new IntersectionObserver((entries) => entries.forEach(({ isIntersecting, target }) => {
    if (!isIntersecting) return;
    target.classList.add("revealed"); observer.unobserve(target);
  }), { threshold: 0.15, rootMargin: "0px 0px -8% 0px" });
  document.querySelectorAll("[data-reveal]").forEach((node, index) => { node.style.transitionDelay = `${(index % 4) * 70}ms`; observer.observe(node); });
}

function initialiseCounters() {
  const observer = new IntersectionObserver((entries) => entries.forEach(({ isIntersecting, target }) => {
    if (!isIntersecting || target.dataset.counted) return;
    target.dataset.counted = ""; const total = Number(target.dataset.count); const start = performance.now();
    const tick = (now) => { const p = Math.min(1, (now - start) / 1100); target.textContent = Math.round(total * (1 - Math.pow(1 - p, 3))).toLocaleString() + (target.dataset.suffix || ""); if (p < 1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick); observer.unobserve(target);
  }), { threshold: 0.45 });
  document.querySelectorAll("[data-count]").forEach((counter) => observer.observe(counter));
}

document.querySelectorAll("[data-nav]").forEach((node) => node.addEventListener("click", (event) => {
  const target = event.currentTarget.dataset.nav; if (target === "top") return;
  event.preventDefault(); document.getElementById(target === "live" || target === "replays" ? "watch" : target).scrollIntoView({ behavior: "smooth", block: "start" });
}));
document.getElementById("back-to-games").addEventListener("click", showLobby);
initialiseReveal(); initialiseCounters();
mountDunkHero(document.getElementById("dunk-canvas"), document.querySelector(".hero-scroll"));
renderGamePicker(pickerContainer, watch);
