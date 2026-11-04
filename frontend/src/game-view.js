// The game page: scoreboard (with period ring and momentum meter), the
// win-probability chart, and the heat-check list. Used for both live games
// (#/live/<id>) and replays (#/replay/<id>).

import { api, explainStreamFailure, liveStreamUrl } from "./api.js";
import { clockText, fromElapsed, percent } from "./format.js";
import { connectStream } from "./sse-client.js";
import { badge, esc, teamName } from "./teams.js";
// "?v=3": bumped whenever worm-chart.js changes shape, so browsers that
// cached an older version fetch the new one.
import { createWormChart, loadD3 } from "./worm-chart.js?v=4";

const CHART_CAPTION =
  "Above the dashed line the home team is favoured, below it the away team. Dots are flagged runs, bigger for more unusual ones.";
const MAX_HEAT_CHECKS = 20;
const MOMENTUM_WINDOW = 180; // game seconds
const MOMENTUM_FULL_SCALE = 12; // a 12-point edge fills one side of the meter
const RING_RADIUS = 16;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

const ARROW_LEFT = `
  <svg class="icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
    <path d="M14 8H3M7 4L3 8l4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" />
  </svg>`;

function layout(mode) {
  return `
    <div class="game-page">
      <p class="back"><a class="btn btn-bare" href="#/">${ARROW_LEFT} Back to games</a></p>
      <section class="card elev-lg scoreboard" id="game-header" aria-live="polite">
        <p class="muted">${mode === "replay" ? "Starting the replay…" : "Connecting to the game…"}</p>
      </section>
      <p class="notice" id="game-notice" role="status" hidden></p>
      <section class="card elev-md" aria-labelledby="chart-title">
        <div class="card-head">
          <div>
            <p class="kicker">Win probability</p>
            <h2 id="chart-title">Who's likely to win</h2>
          </div>
        </div>
        <div class="legend" id="chart-legend"></div>
        <div id="chart" class="chart"></div>
        <p class="chart-caption muted">${CHART_CAPTION}</p>
      </section>
      <section class="card elev-sm" aria-labelledby="heat-title">
        <div class="card-head">
          <div>
            <p class="kicker">Run monitor</p>
            <h2 id="heat-title">Heat checks</h2>
          </div>
          <p class="kicker muted">Newest first</p>
        </div>
        <ol class="heat-list" id="heat-list"></ol>
        <p class="muted" id="heat-empty">No unusually quick runs yet.</p>
      </section>
    </div>`;
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
  const dot = `<span class="live-dot" aria-hidden="true"></span>`;
  if (view.mode === "replay") {
    return `<span class="chip chip-replay">${dot}Live replay${view.speed ? ` · ${view.speed}×` : ""}</span>`;
  }
  return `<span class="chip chip-live">${dot}Live</span>`;
}

function readout(view) {
  const home = view.home || "Home";
  const away = view.away || "Away";
  if (view.status === "final") {
    const homeWon = view.homeScore > view.awayScore;
    const high = Math.max(view.homeScore, view.awayScore);
    const low = Math.min(view.homeScore, view.awayScore);
    return `Final: ${esc(homeWon ? home : away)} won ${high}-${low}.`;
  }
  if (view.wpHome == null) return "The win chance appears with the first score update.";
  if (view.status === "pregame") {
    return "Not started yet. The model knows nothing about team strength, so every game starts near 50%.";
  }
  const favourite = view.wpHome >= 0.5 ? home : away;
  const chance = Math.max(view.wpHome, 1 - view.wpHome);
  return `${esc(favourite)} has a ${percent(chance)} chance to win, based only on the score, the time left and the quarter.`;
}

// Fraction of the current period still to play, for the ring.
function periodRing(view) {
  if (view.status !== "live" || view.period == null || view.clock == null) return "";
  const length = view.period > 4 ? 300 : 720;
  const fraction = Math.max(0, Math.min(1, view.clock / length));
  const arc = fraction * RING_CIRCUMFERENCE;
  return `
    <svg class="period-ring" viewBox="0 0 40 40" role="img" aria-label="${Math.round(fraction * 100)}% of the period left">
      <circle class="period-ring-track" cx="20" cy="20" r="${RING_RADIUS}" />
      <circle class="period-ring-arc" cx="20" cy="20" r="${RING_RADIUS}"
        stroke-dasharray="${arc} ${RING_CIRCUMFERENCE}" transform="rotate(-90 20 20)" />
    </svg>`;
}

// Points each team scored over the last MOMENTUM_WINDOW game seconds.
function momentum(history) {
  const last = history[history.length - 1];
  if (!last) return { home: 0, away: 0, edge: 0 };
  const cutoff = last.t - MOMENTUM_WINDOW;
  let ref = history[0];
  for (const point of history) {
    if (point.t <= cutoff) ref = point;
    else break;
  }
  const home = last.home - ref.home;
  const away = last.away - ref.away;
  return { home, away, edge: home - away };
}

function momentumMeter(view, m) {
  const share = Math.max(-1, Math.min(1, m.edge / MOMENTUM_FULL_SCALE));
  const width = Math.abs(share) * 50;
  const side = share >= 0 ? "is-home" : "is-away";
  const away = view.away || "Away";
  const home = view.home || "Home";
  const label = `Last 3 minutes of game time: ${away} scored ${m.away}, ${home} scored ${m.home}`;
  return `
    <div class="momentum" role="img" aria-label="${esc(label)}">
      <div class="momentum-track">
        <span class="momentum-bar ${side}" style="width:${width}%"></span>
        <span class="momentum-tick"></span>
      </div>
      <p class="momentum-labels">${esc(away)} ${m.away} · Last 3 min · ${m.home} ${esc(home)}</p>
    </div>`;
}

function scoreSide(code, score, sideLabel) {
  const name = teamName(code) || code || sideLabel;
  return `
    <div class="score-side${sideLabel === "Home" ? " score-side-home" : ""}">
      ${badge(code, { size: "lg" })}
      <div>
        <p class="kicker">${esc(name)} · ${sideLabel}</p>
        <p class="score-num">${score}</p>
      </div>
    </div>`;
}

function renderHeader(el, view, history) {
  const wp = view.wpHome ?? 0.5;
  const score = (value) => (value == null || view.status === "pregame" ? "–" : value);
  const clock = view.status === "final" ? "" : view.status === "pregame" ? "Tip-off soon" : view.clockText;
  el.innerHTML = `
    <div class="score-row">
      ${scoreSide(view.away, score(view.awayScore), "Away")}
      <div class="score-center">
        ${statusChip(view)}
        <div class="clock-row">
          ${periodRing(view)}
          <span class="clock">${esc(clock || "")}</span>
        </div>
        ${view.status === "live" ? momentumMeter(view, momentum(history)) : ""}
      </div>
      ${scoreSide(view.home, score(view.homeScore), "Home")}
    </div>
    <div class="wp-block">
      <div class="wp-bar" role="img" aria-label="${esc(view.away)} ${percent(1 - wp)}, ${esc(view.home)} ${percent(wp)}">
        <span class="wp-bar-away" style="width:${(1 - wp) * 100}%"></span>
        <span class="wp-bar-home" style="width:${wp * 100}%"></span>
      </div>
      <div class="wp-bar-labels">
        <span>${esc(view.away)} ${percent(1 - wp)}</span>
        <span>${esc(view.home)} ${percent(wp)}</span>
      </div>
      <p class="wp-readout">${readout(view)}</p>
    </div>`;
}

export function renderGame(root, { mode, id, speed, onMode }) {
  root.innerHTML = layout(mode);
  const header = root.querySelector("#game-header");
  const notice = root.querySelector("#game-notice");
  const legend = root.querySelector("#chart-legend");
  const heatList = root.querySelector("#heat-list");
  const heatEmpty = root.querySelector("#heat-empty");

  const view = {
    mode,
    speed,
    home: "",
    away: "",
    homeScore: null,
    awayScore: null,
    period: null,
    clock: null,
    clockText: "",
    wpHome: null,
    status: "connecting",
  };
  const history = []; // {t, home, away}, for the momentum meter
  let alive = true;
  let source = null;
  let chart = null;
  let receivedAny = false;
  let streamUrl = null;

  const showNotice = (html) => {
    notice.innerHTML = html;
    notice.hidden = !html;
  };

  function renderLegend(message = null) {
    legend.innerHTML = `
      <span class="legend-key"><span class="legend-swatch legend-swatch-home"></span>${esc(view.home)} (home)</span>
      <span class="legend-key"><span class="legend-swatch legend-swatch-away"></span>${esc(view.away)} (away)</span>
      ${message ? `<span class="chip chip-heat legend-tag">${esc(message)}</span>` : ""}`;
  }

  function ensureChart() {
    if (chart || !view.home || !view.away) return;
    renderLegend();
    chart = createWormChart(root.querySelector("#chart"), {
      homeLabel: view.home,
      awayLabel: view.away,
      onHover: (message) => renderLegend(message),
    });
  }

  function track(p) {
    const last = history[history.length - 1];
    if (last && p.t < last.t) return; // a reconnect replaying older history
    const entry = { t: p.t, home: p.home_score, away: p.away_score };
    if (last && p.t === last.t) history[history.length - 1] = entry;
    else history.push(entry);
    const keepFrom = p.t - 2 * MOMENTUM_WINDOW;
    while (history.length > 1 && history[0].t < keepFrom) history.shift();
  }

  function onPoint(p) {
    receivedAny = true;
    view.home = p.home_team || view.home;
    view.away = p.away_team || view.away;
    view.homeScore = p.home_score;
    view.awayScore = p.away_score;
    view.wpHome = p.wp_home;
    const where = p.period != null ? { period: p.period, clock: p.clock } : fromElapsed(p.t);
    view.period = where.period;
    view.clock = where.clock;
    view.clockText = clockText(where.period, where.clock);
    if (view.status !== "final") view.status = p.status === "scheduled" ? "pregame" : "live";
    track(p);
    showNotice("");
    ensureChart();
    chart?.addPoint({ t: p.t, wp_home: p.wp_home });
    renderHeader(header, view, history);
  }

  function onAnomaly(a) {
    receivedAny = true;
    ensureChart();
    chart?.addAnomaly(a);
    const where = fromElapsed(a.t);
    const row = document.createElement("li");
    row.className = `heat heat-${heatTier(a.z_score)}`;
    row.innerHTML = `
      <span class="chip chip-heat">${a.z_score.toFixed(1)}σ</span>
      <p>${esc(a.message)}</p>
      <span class="heat-clock">${esc(clockText(where.period, where.clock))}</span>`;
    heatList.prepend(row);
    while (heatList.children.length > MAX_HEAT_CHECKS) heatList.lastElementChild.remove();
    heatEmpty.hidden = true;
  }

  function onEnd(e) {
    view.status = "final";
    view.homeScore = e.final_home_score;
    view.awayScore = e.final_away_score;
    renderHeader(header, view, history);
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
    showNotice(`Connection lost. <button type="button" class="btn btn-quiet btn-sm" id="retry">Try again</button>`);
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
      // The chart needs D3; fetch it while the game is looked up.
      const d3Ready = loadD3();
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
        await d3Ready;
        if (!alive) return;
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
        await d3Ready;
        if (!alive) return;
        connect(session.stream_url);
      }
      ensureChart();
      renderHeader(header, view, history);
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
