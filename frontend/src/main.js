import { renderGamePicker } from "./game-picker.js";
import { connectStream } from "./sse-client.js";
import { createWormChart } from "./worm-chart.js";

const chartContainer = document.getElementById("worm-chart");
const statusLine = document.getElementById("status-line");
const scoreRow = document.getElementById("score-row");
const anomalyFeed = document.getElementById("anomaly-feed");
const pickerContainer = document.getElementById("game-picker");

const chart = createWormChart(chartContainer);
let currentSource = null;

function setStatus(text, connected) {
  statusLine.textContent = text;
  statusLine.classList.toggle("connected", Boolean(connected));
}

function updateScoreRow(homeScore, awayScore) {
  scoreRow.innerHTML = `
    <div class="team">${awayScore}</div>
    <div class="score">vs</div>
    <div class="team">${homeScore}</div>
  `;
}

function addAnomalyToFeed(anomaly) {
  let ul = anomalyFeed.querySelector("ul");
  if (!ul) {
    anomalyFeed.innerHTML = "<ul></ul>";
    ul = anomalyFeed.querySelector("ul");
  }
  const li = document.createElement("li");
  li.textContent = `🔥 ${anomaly.message}`;
  ul.prepend(li);
}

function watch(streamUrl, label) {
  if (currentSource) {
    currentSource.close();
  }
  chart.reset();
  anomalyFeed.innerHTML = '<p class="empty-state">No heat checks yet.</p>';
  setStatus(`Connecting to ${label}...`, false);

  currentSource = connectStream(streamUrl, {
    wp_update: (payload) => {
      setStatus(`Live: ${label}`, true);
      chart.addPoint(payload);
      updateScoreRow(payload.home_score, payload.away_score);
    },
    anomaly: (payload) => {
      chart.addAnomaly(payload);
      addAnomalyToFeed(payload);
    },
    game_end: (payload) => {
      setStatus(`Final: ${label}`, true);
    },
    degraded: () => {
      setStatus(`${label}: data source degraded, retrying...`, false);
    },
    onerror: () => {
      setStatus(`${label}: connection lost`, false);
    },
  });
}

renderGamePicker(pickerContainer, watch);
