import { renderGamePicker } from "./game-picker.js";
import { connectStream } from "./sse-client.js";
import { createWormChart } from "./worm-chart.js";

const chartContainer = document.getElementById("worm-chart");
const matchupHeader = document.getElementById("matchup-header");
const anomalyFeed = document.getElementById("anomaly-feed");
const pickerContainer = document.getElementById("game-picker");

const chart = createWormChart(chartContainer);
let currentSource = null;

function formatClock(period, clockSeconds) {
  if (period == null || clockSeconds == null) return "";
  const periodLabel = period <= 4 ? `Q${period}` : period === 5 ? "OT" : `${period - 4}OT`;
  const minutes = Math.floor(clockSeconds / 60);
  const seconds = Math.floor(clockSeconds % 60)
    .toString()
    .padStart(2, "0");
  return `${periodLabel} · ${minutes}:${seconds}`;
}

function renderMatchup({ awayTeam, homeTeam, awayScore, homeScore, clockText, statusText, connected }) {
  matchupHeader.innerHTML = `
    <div class="team-block away">
      <div class="team-score">${awayScore ?? "–"}</div>
      <div class="team-abbr">${awayTeam || "AWAY"}</div>
    </div>
    <div class="game-meta">
      <div class="status-pill ${connected ? "connected" : ""}"><span class="dot"></span>${statusText}</div>
      <div class="clock">${clockText || ""}</div>
    </div>
    <div class="team-block home">
      <div class="team-abbr">${homeTeam || "HOME"}</div>
      <div class="team-score">${homeScore ?? "–"}</div>
    </div>
  `;
}

function addAnomalyToFeed(anomaly) {
  let ul = anomalyFeed.querySelector("ul");
  if (!ul) {
    anomalyFeed.innerHTML = "<ul></ul>";
    ul = anomalyFeed.querySelector("ul");
  }
  const li = document.createElement("li");
  li.innerHTML = `<span class="z-badge">${anomaly.z_score.toFixed(1)}σ</span><span>🔥 ${anomaly.message}</span>`;
  ul.prepend(li);
}

function watch(streamUrl, label, meta = {}) {
  if (currentSource) {
    currentSource.close();
  }
  chart.reset();
  anomalyFeed.innerHTML = '<p class="empty-state">No heat checks yet.</p>';

  const view = {
    awayTeam: meta.awayTeam,
    homeTeam: meta.homeTeam,
    awayScore: null,
    homeScore: null,
    clockText: "",
    statusText: "Connecting…",
    connected: false,
  };
  renderMatchup(view);

  currentSource = connectStream(streamUrl, {
    wp_update: (payload) => {
      view.awayScore = payload.away_score;
      view.homeScore = payload.home_score;
      view.awayTeam = payload.away_team || view.awayTeam;
      view.homeTeam = payload.home_team || view.homeTeam;
      view.statusText = "Live";
      view.connected = true;
      if (payload.period != null) {
        view.clockText = formatClock(payload.period, payload.clock);
      }
      renderMatchup(view);
      chart.addPoint(payload);
    },
    anomaly: (payload) => {
      chart.addAnomaly(payload);
      addAnomalyToFeed(payload);
    },
    game_end: () => {
      view.statusText = "Final";
      view.clockText = "";
      renderMatchup(view);
    },
    degraded: () => {
      view.statusText = "Reconnecting…";
      view.connected = false;
      renderMatchup(view);
    },
    onerror: () => {
      view.statusText = "Connection lost";
      view.connected = false;
      renderMatchup(view);
    },
  });
}

renderGamePicker(pickerContainer, watch);
