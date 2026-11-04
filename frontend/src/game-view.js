// The game page: scoreboard header, win-probability chart, heat-check list.
// Used for both live games (#/live/<id>) and replays (#/replay/<id>).

import { api, explainStreamFailure, liveStreamUrl } from "./api.js";
import { clockText, fromElapsed, percent } from "./format.js";
import { connectStream } from "./sse-client.js";
import { badge, esc, matchupColors, teamName } from "./teams.js";
// "?v=2": worm-chart.js existed before the redesign; the query string makes
// browsers that cached the old version fetch the new one.
import { createWormChart } from "./worm-chart.js?v=2";

const CHART_CAPTION = "Above the line the home team is favoured, below it the away team. Markers are flagged runs.";
const MAX_HEAT_CHECKS = 20;

function layout(mode) {
  return `
    <p class="back"><a href="#/"><span aria-hidden="true">←</span> All games</a></p>
    <section class="card game-header" id="game-header" aria-live="polite">
      <p class="muted">${mode === "replay" ? "Starting the replay…" : "Connecting to the game…"}</p>
    </section>
    <p class="notice" id="game-notice" role="status" hidden></p>
    <section class="card chart-card" aria-labelledby="chart-title">
      <div class="card-head">
        <h2 id="chart-title">Win chance</h2>
      </div>
      <div id="chart" class="chart"></div>
      <p class="chart-caption muted" id="chart-caption">${CHART_CAPTION}</p>
    </section>
    <section class="card" aria-labelledby="heat-title">
      <div class="card-head">
        <h2 id="heat-title">Heat checks</h2>
        <p class="muted small">Newest first</p>
      </div>
      <ol class="heat-list" id="heat-list"></ol>
      <p class="muted" id="heat-empty">No unusually quick runs yet.</p>
    </section>`;
}

function heatTier(z) {
  if (z >= 3.5) return "extreme";
  if (z >= 3.0) return "high";
  if (z >= 2.5) return "mid";
  return "mild";
}

function statusChip(view) {
  if (view.status === "final") return `<span class="chip">Final</span>`;
  if (view.status === "pregame") return `<span class="chip">Pre-game</span>`;
  if (view.mode === "replay") return `<span class="chip chip-replay">Replay${view.speed ? ` · ${view.speed}×` : ""}</span>`;
  return `<span class="chip chip-live"><span class="live-dot" aria-hidden="true"></span>Live</span>`;
}

function readout(view) {
  const home = view.home || "Home";
  const away = view.away || "Away";
  if (view.status === "final") {
    const homeWon = view.homeScore > view.awayScore;
    const winner = homeWon ? home : away;
    const high = Math.max(view.homeScore, view.awayScore);
    const low = Math.min(view.homeScore, view.awayScore);
    return `Final: ${esc(winner)} won ${high}-${low}.`;
  }
  if (view.wpHome == null) return "The win chance appears with the first score update.";
  if (view.status === "pregame") {
    return "Not started yet. The model knows nothing about team strength, so every game starts near 50%.";
  }
  const favourite = view.wpHome >= 0.5 ? home : away;
  const chance = Math.max(view.wpHome, 1 - view.wpHome);
  return `${esc(favourite)} has a ${percent(chance)} chance to win, based only on the score, the time left and the quarter.`;
}

function renderHeader(el, view) {
  const wp = view.wpHome ?? 0.5;
  const score = (value) => (value == null || view.status === "pregame" ? "–" : value);
  const clock = view.status === "final" ? "" : view.status === "pregame" ? "Tip-off soon" : view.clockText;
  el.innerHTML = `
    <div class="game-teams">
      <div class="game-team">
        ${badge(view.away, { size: "lg" })}
        <div>
          <p class="game-team-name">${esc(teamName(view.away) || view.away || "Away")}</p>
          <p class="muted small">Away</p>
        </div>
        <p class="game-score num">${score(view.awayScore)}</p>
      </div>
      <div class="game-center">
        ${statusChip(view)}
        <p class="game-clock">${esc(clock || "")}</p>
      </div>
      <div class="game-team game-team-home">
        <p class="game-score num">${score(view.homeScore)}</p>
        <div>
          <p class="game-team-name">${esc(teamName(view.home) || view.home || "Home")}</p>
          <p class="muted small">Home</p>
        </div>
        ${badge(view.home, { size: "lg" })}
      </div>
    </div>
    <div class="wp-bar" role="img" aria-label="${esc(view.away)} ${percent(1 - wp)}, ${esc(view.home)} ${percent(wp)}">
      <span style="width:${(1 - wp) * 100}%;background:${view.colors.away}"></span>
      <span style="width:${wp * 100}%;background:${view.colors.home}"></span>
    </div>
    <div class="wp-bar-labels num">
      <span>${esc(view.away)} ${percent(1 - wp)}</span>
      <span>${esc(view.home)} ${percent(wp)}</span>
    </div>
    <p class="wp-readout">${readout(view)}</p>`;
}

export function renderGame(root, { mode, id, speed, onMode }) {
  root.innerHTML = layout(mode);
  const header = root.querySelector("#game-header");
  const notice = root.querySelector("#game-notice");
  const caption = root.querySelector("#chart-caption");
  const heatList = root.querySelector("#heat-list");
  const heatEmpty = root.querySelector("#heat-empty");

  const view = {
    mode,
    speed,
    home: "",
    away: "",
    homeScore: null,
    awayScore: null,
    clockText: "",
    wpHome: null,
    status: "connecting",
    colors: { home: "#6b7280", away: "#9ca3af" },
  };
  let alive = true;
  let source = null;
  let chart = null;
  let receivedAny = false;
  let streamUrl = null;

  const showNotice = (html) => {
    notice.innerHTML = html;
    notice.hidden = !html;
  };

  function ensureChart() {
    if (chart || !view.home || !view.away) return;
    view.colors = matchupColors(view.home, view.away);
    chart = createWormChart(root.querySelector("#chart"), {
      homeColor: view.colors.home,
      awayColor: view.colors.away,
      homeLabel: view.home,
      awayLabel: view.away,
      onHover: (message) => {
        caption.textContent = message || CHART_CAPTION;
      },
    });
  }

  function onPoint(p) {
    receivedAny = true;
    view.home = p.home_team || view.home;
    view.away = p.away_team || view.away;
    view.homeScore = p.home_score;
    view.awayScore = p.away_score;
    view.wpHome = p.wp_home;
    const where = p.period != null ? { period: p.period, clock: p.clock } : fromElapsed(p.t);
    view.clockText = clockText(where.period, where.clock);
    if (view.status !== "final") view.status = p.status === "scheduled" ? "pregame" : "live";
    showNotice("");
    ensureChart();
    chart?.addPoint({ t: p.t, wp_home: p.wp_home });
    renderHeader(header, view);
  }

  function onAnomaly(a) {
    receivedAny = true;
    ensureChart();
    chart?.addAnomaly(a);
    const where = fromElapsed(a.t);
    const row = document.createElement("li");
    row.className = `heat heat-${heatTier(a.z_score)}`;
    row.innerHTML = `
      <span class="chip chip-heat num">${a.z_score.toFixed(1)}σ</span>
      <p>${esc(a.message)}</p>
      <span class="muted">${esc(clockText(where.period, where.clock))}</span>`;
    heatList.prepend(row);
    while (heatList.children.length > MAX_HEAT_CHECKS) heatList.lastElementChild.remove();
    heatEmpty.hidden = true;
  }

  function onEnd(e) {
    view.status = "final";
    view.homeScore = e.final_home_score;
    view.awayScore = e.final_away_score;
    renderHeader(header, view);
    showNotice("");
    // The server forgets a finished game; stop the browser from reconnecting.
    source?.close();
  }

  async function onError({ reconnecting }) {
    if (!alive || view.status === "final") return;
    if (reconnecting) {
      showNotice("Reconnecting…");
      return;
    }
    source?.close();
    const reason = !receivedAny && streamUrl ? await explainStreamFailure(streamUrl) : null;
    if (!alive) return;
    if (!receivedAny && reason) {
      header.innerHTML = `<p class="notice">${esc(reason)}</p><p><a href="#/">Back to tonight's games</a></p>`;
      return;
    }
    showNotice(`Connection lost. <button type="button" class="button button-quiet" id="retry">Try again</button>`);
    notice.querySelector("#retry")?.addEventListener("click", () => window.location.reload());
  }

  function connect(url) {
    streamUrl = url;
    source = connectStream(url, {
      wp_update: onPoint,
      anomaly: onAnomaly,
      game_end: onEnd,
      degraded: () => showNotice("NBA.com isn't responding right now. Still trying…"),
      onerror: onError,
    });
  }

  async function start() {
    try {
      if (mode === "live") {
        const board = await api.scoreboard();
        if (!alive) return;
        onMode?.(board.mode === "demo");
        const game = board.games.find((g) => g.game_id === id);
        if (!game) {
          header.innerHTML = `<p class="notice">That game isn't on today's scoreboard.</p><p><a href="#/">Back to tonight's games</a></p>`;
          return;
        }
        view.home = game.home.tricode;
        view.away = game.away.tricode;
        if (game.status === "final") view.status = "final";
        connect(liveStreamUrl(id));
      } else {
        const [session, manifest] = await Promise.all([
          api.startReplay(id, speed),
          api.replays().catch(() => ({ fixtures: [] })),
        ]);
        if (!alive) return;
        const fixture = (manifest.fixtures || []).find((f) => f.game_id === id);
        if (fixture) {
          view.home = fixture.home_team;
          view.away = fixture.away_team;
        }
        connect(session.stream_url);
      }
      ensureChart();
      renderHeader(header, view);
    } catch (error) {
      if (!alive) return;
      header.innerHTML = `<p class="notice">${esc(error.message)}</p><p><a href="#/">Back to the dashboard</a></p>`;
    }
  }

  start();

  return () => {
    alive = false;
    source?.close();
    chart?.destroy();
  };
}
