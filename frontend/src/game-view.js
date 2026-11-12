// The game page: scoreboard (with period ring and momentum meter), connection
// status, the run meter, the win-probability chart and the heat-check feed.
// Used for both live games (#/live/<id>) and replays (#/replay/<id>).
//
// Every wp_update carries the run in progress, so the run meter shows
// something on every prediction; a run that crosses the threshold is also
// flagged on the chart, listed in the feed and announced in a toast.

import { api, explainStreamFailure, liveStreamUrl } from "./api.js";
import { clockText, fromElapsed, percent } from "./format.js";
import { connectStream } from "./sse-client.js";
import { badge, esc, teamName } from "./teams.js";
// "?v=5": bumped whenever worm-chart.js changes shape, so browsers that
// cached an older version fetch the new one.
import { anomalyKey, createWormChart, loadD3 } from "./worm-chart.js?v=5";

const CHART_CAPTION =
  "Above the dashed line the home team is favoured, below it the away team. Labelled markers are flagged runs, bigger for more unusual ones. Tap one to read it.";
const MAX_HEAT_CHECKS = 100;
const MIN_RUN_DISPLAY = 4; // points; a basket or two is not worth calling a run
const STALLED_SECONDS = 30; // no event or heartbeat for this long: say so
const TOAST_SECONDS = 9;
const MAX_TOASTS = 3;
const TIER_WORDS = { mild: "Quicker than usual", mid: "Unusual", high: "Rare", extreme: "Extreme" };
const DELAY_SAMPLES = 10;
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
      <div class="statusbar" id="game-status" role="status"></div>
      <p class="notice" id="game-notice" role="status" hidden></p>
      <section class="card elev-md" aria-labelledby="chart-title">
        <div class="card-head">
          <div>
            <p class="kicker">Win probability</p>
            <h2 id="chart-title">Who's likely to win</h2>
          </div>
        </div>
        <div class="run-meter" id="run-meter" data-state="idle">
          <p class="run-text" id="run-text">No active run</p>
          <div class="gauge" id="run-gauge" role="meter" aria-label="How close the current run is to a heat check"
            aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
            <span class="gauge-fill" id="run-fill"></span>
          </div>
          <p class="run-sub muted" id="run-sub"></p>
        </div>
        <div class="legend" id="chart-legend"></div>
        <div id="chart" class="chart"></div>
        <p class="marker-detail" id="marker-detail" hidden></p>
        <p class="chart-caption muted">${CHART_CAPTION}</p>
      </section>
      <section class="card elev-sm" aria-labelledby="heat-title">
        <div class="card-head">
          <div>
            <p class="kicker">Run monitor</p>
            <h2 id="heat-title">Heat checks</h2>
          </div>
          <div class="head-tools">
            <button type="button" class="btn btn-quiet btn-sm" id="sound-toggle" aria-pressed="false">Sound off</button>
            <p class="kicker muted">Newest first</p>
          </div>
        </div>
        <ol class="heat-list" id="heat-list" aria-live="polite"></ol>
        <p class="muted" id="heat-empty">No unusually quick runs yet. A heat check is a run of 6 or more unanswered points that came faster than usual for that point in a game.</p>
      </section>
    </div>
    <div class="toast-region" id="toast-region" aria-live="polite"></div>`;
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

function shortSeconds(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 120) return `${s} s`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function readSound() {
  try {
    return localStorage.getItem("heatcheck.sound") === "1";
  } catch {
    return false; // storage can be blocked; sound simply stays off
  }
}

function saveSound(on) {
  try {
    localStorage.setItem("heatcheck.sound", on ? "1" : "0");
  } catch {
    // nothing to do: a preference that can't be saved is not worth an error
  }
}

export function renderGame(root, { mode, id, speed, onMode }) {
  root.innerHTML = layout(mode);
  const $ = (selector) => root.querySelector(selector);
  const header = $("#game-header");
  const notice = $("#game-notice");
  const legend = $("#chart-legend");
  const heatList = $("#heat-list");
  const heatEmpty = $("#heat-empty");
  const statusBar = $("#game-status");
  const markerDetail = $("#marker-detail");
  const toastRegion = $("#toast-region");
  const soundToggle = $("#sound-toggle");
  const meter = {
    box: $("#run-meter"),
    text: $("#run-text"),
    gauge: $("#run-gauge"),
    fill: $("#run-fill"),
    sub: $("#run-sub"),
  };

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
  const seenAnomalies = new Map(); // key -> {anomaly, row}
  const delays = []; // recent feed delays, seconds, from plays seen live
  let alive = true;
  let source = null;
  let chart = null;
  let receivedAny = false;
  let streamUrl = null;
  let conn = "connecting"; // connecting | live | reconnecting | ended | lost
  let lastSeen = null; // when anything (event or heartbeat) last arrived
  let lastPlayAt = null; // when a play last arrived live
  let fidelity = "play_by_play";
  let run = null;
  let selectedKey = null;
  let headerFrame = null;
  let soundOn = readSound();
  let audio = null;

  const showNotice = (html) => {
    notice.innerHTML = html;
    notice.hidden = !html;
  };

  // ---- connection status -------------------------------------------------

  function renderStatus() {
    const now = Date.now();
    const silent = lastSeen ? (now - lastSeen) / 1000 : 0;
    let state = conn;
    let label = { connecting: "Connecting…", reconnecting: "Reconnecting…", ended: "Ended", lost: "Connection lost" }[conn];
    if (conn === "live") {
      state = silent > STALLED_SECONDS ? "stalled" : "live";
      label = state === "stalled" ? `No data for ${Math.round(silent)} s` : mode === "replay" ? "Replay" : "Live";
    }
    const parts = [
      `<span class="conn conn-${state}"><span class="conn-dot" aria-hidden="true"></span>${esc(label)}</span>`,
    ];
    if (mode === "live" && conn === "live" && lastPlayAt) {
      parts.push(`<span class="status-item">Last play ${shortSeconds((now - lastPlayAt) / 1000)} ago</span>`);
    }
    if (mode === "live" && delays.length && conn !== "ended") {
      const seconds = Math.round(median(delays));
      parts.push(
        `<span class="status-item" title="Time from the NBA's feed logging a play to this server sending it to you. It cannot be shorter than the feed itself, and it does not include the broadcast delay or your own network.">Feed delay ≈ ${seconds} s</span>`,
      );
    }
    if (fidelity === "boxscore_ticks") {
      parts.push(
        `<span class="status-item status-warn" title="Play-by-play was unavailable for this game, so runs are timed from score updates. Some heat checks can be missed or mistimed.">Approximate mode</span>`,
      );
    }
    statusBar.innerHTML = parts.join("");
  }

  // ---- run meter ---------------------------------------------------------

  function renderRun() {
    let state = "idle";
    let text = "No active run";
    let sub = "A run is unanswered points by one team. It can be flagged once it reaches 6 points.";
    let fill = 0;
    if (view.status === "final") {
      state = "final";
      text = "Game over";
      sub = "";
    } else if (run && run.points >= MIN_RUN_DISPLAY) {
      const code = run.team === "home" ? view.home : view.away;
      const z = run.z_score;
      const threshold = run.threshold || 1.5;
      text = `${esc(code || run.team)} on a ${run.points}-0 run, ${shortSeconds(run.duration_sec)}`;
      fill = z == null ? 0 : Math.max(0, Math.min(1, z / threshold));
      if (run.flagged) {
        state = "flagged";
        sub = `Flagged as a heat check${z != null ? ` · ${z.toFixed(1)}σ` : ""}`;
      } else {
        state = "building";
        sub =
          z == null
            ? "Too small to score yet: runs are scored from 6 points."
            : `Heat check at ${threshold}σ · this run is at ${z.toFixed(1)}σ`;
      }
      if (run.since_last_score_sec >= 20) sub += ` · last basket ${shortSeconds(run.since_last_score_sec)} ago`;
    }
    meter.box.dataset.state = state;
    meter.box.dataset.side = run && state !== "idle" ? run.team : "";
    meter.text.innerHTML = text;
    meter.sub.textContent = sub;
    meter.fill.style.width = `${Math.round(fill * 100)}%`;
    meter.gauge.setAttribute("aria-valuenow", String(Math.round(fill * 100)));
    meter.gauge.setAttribute("aria-valuetext", state === "idle" ? "no active run" : `${Math.round(fill * 100)}% of the way to a heat check`);
  }

  // ---- header, legend, chart ---------------------------------------------

  function renderLegend(message = null) {
    legend.innerHTML = `
      <span class="legend-key"><span class="legend-swatch legend-swatch-home"></span>${esc(view.home)} (home)</span>
      <span class="legend-key"><span class="legend-swatch legend-swatch-away"></span>${esc(view.away)} (away)</span>
      ${message ? `<span class="chip chip-heat legend-tag">${esc(message)}</span>` : ""}`;
  }

  function scheduleHeader() {
    if (headerFrame !== null) return;
    headerFrame = requestAnimationFrame(() => {
      headerFrame = null;
      if (alive) renderHeader(header, view, history);
    });
  }

  function ensureChart() {
    if (chart || !view.home || !view.away) return;
    renderLegend();
    chart = createWormChart($("#chart"), {
      homeLabel: view.home,
      awayLabel: view.away,
      onHover: (message) => renderLegend(message),
      onSelect: (anomaly) => select(anomaly ? anomalyKey(anomaly) : null, { fromChart: true }),
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

  // ---- heat-check feed, selection, toast, sound ----------------------------

  function whereText(a) {
    const where = a.period ? { period: a.period, clock: a.clock } : fromElapsed(a.t);
    return clockText(where.period, where.clock);
  }

  function select(key, { fromChart = false } = {}) {
    selectedKey = key;
    for (const [k, entry] of seenAnomalies) {
      entry.row.classList.toggle("is-selected", k === key);
      entry.row.querySelector(".heat-btn").setAttribute("aria-pressed", String(k === key));
    }
    const entry = key ? seenAnomalies.get(key) : null;
    if (!fromChart) chart?.select(key);
    markerDetail.hidden = !entry;
    if (entry) {
      markerDetail.textContent = `${whereText(entry.anomaly)} · ${entry.anomaly.message}`;
      if (fromChart) entry.row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function addHeatRow(a) {
    const key = anomalyKey(a);
    if (seenAnomalies.has(key)) return false; // a reconnect re-sending history
    const code = a.team_code || (a.team === "home" ? view.home : view.away);
    const tier = a.tier || heatTier(a.z_score);
    const row = document.createElement("li");
    row.className = `heat heat-${tier}`;
    row.innerHTML = `
      <button type="button" class="heat-btn" aria-pressed="false">
        ${badge(code, { size: "sm" })}
        <span class="heat-main">
          <span class="heat-title">${a.magnitude}-0 run in ${esc(shortSeconds(a.duration_sec))}</span>
          <span class="heat-sub"><span class="chip chip-heat">${a.z_score.toFixed(1)}σ</span> ${esc(TIER_WORDS[tier])}</span>
          <span class="heat-msg">${esc(a.message)}</span>
        </span>
        <span class="heat-clock">${esc(whereText(a))}</span>
      </button>`;
    row.querySelector(".heat-btn").addEventListener("click", () => select(selectedKey === key ? null : key));
    heatList.prepend(row);
    seenAnomalies.set(key, { anomaly: a, row });
    while (heatList.children.length > MAX_HEAT_CHECKS) {
      const last = heatList.lastElementChild;
      for (const [k, entry] of seenAnomalies) if (entry.row === last) seenAnomalies.delete(k);
      last.remove();
    }
    heatEmpty.hidden = true;
    return true;
  }

  function showToast(a) {
    const code = a.team_code || (a.team === "home" ? view.home : view.away);
    const toast = document.createElement("div");
    toast.className = `toast toast-${a.tier || heatTier(a.z_score)}`;
    toast.innerHTML = `
      <p><strong>Heat check</strong> ${esc(code)} ${a.magnitude}-0 run in ${esc(shortSeconds(a.duration_sec))}
      <span class="toast-clock">${esc(whereText(a))}</span></p>
      <button type="button" class="toast-close" aria-label="Dismiss">×</button>`;
    const dismiss = () => toast.remove();
    toast.querySelector(".toast-close").addEventListener("click", dismiss);
    toast.addEventListener("click", (event) => {
      if (event.target.closest(".toast-close")) return;
      select(anomalyKey(a));
      dismiss();
    });
    toastRegion.prepend(toast);
    while (toastRegion.children.length > MAX_TOASTS) toastRegion.lastElementChild.remove();
    setTimeout(dismiss, TOAST_SECONDS * 1000);
    if (soundOn) beep();
  }

  function beep() {
    try {
      audio ||= new (window.AudioContext || window.webkitAudioContext)();
      const now = audio.currentTime;
      for (const [offset, freq] of [[0, 740], [0.14, 988]]) {
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(0.12, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.18);
        osc.connect(gain).connect(audio.destination);
        osc.start(now + offset);
        osc.stop(now + offset + 0.2);
      }
    } catch {
      // no audio available; the toast is still there
    }
  }

  function renderSound() {
    soundToggle.textContent = soundOn ? "Sound on" : "Sound off";
    soundToggle.setAttribute("aria-pressed", String(soundOn));
  }

  soundToggle.addEventListener("click", () => {
    soundOn = !soundOn;
    saveSound(soundOn);
    renderSound();
    if (soundOn) beep(); // the click is the user gesture browsers require, and confirms it works
  });

  // ---- stream events -------------------------------------------------------

  function onPoint(p) {
    receivedAny = true;
    lastSeen = Date.now();
    if (conn !== "ended") conn = "live";
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
    fidelity = p.fidelity || fidelity;
    run = p.run ?? null;
    if (!p.backlog) {
      lastPlayAt = Date.now();
      if (typeof p.feed_delay_sec === "number") {
        delays.push(p.feed_delay_sec);
        if (delays.length > DELAY_SAMPLES) delays.shift();
      }
    }
    track(p);
    showNotice("");
    ensureChart();
    chart?.addPoint({ t: p.t, wp_home: p.wp_home });
    scheduleHeader();
    renderRun();
    renderStatus();
  }

  function onAnomaly(a) {
    receivedAny = true;
    lastSeen = Date.now();
    ensureChart();
    chart?.addAnomaly(a);
    const added = addHeatRow(a);
    if (added && !a.backlog) showToast(a);
  }

  function onEnd(e) {
    view.status = "final";
    view.homeScore = e.final_home_score;
    view.awayScore = e.final_away_score;
    conn = "ended";
    run = null;
    renderHeader(header, view, history);
    renderRun();
    renderStatus();
    showNotice("");
    // Nothing more will come; stop the browser from reconnecting.
    source?.close();
  }

  async function onError({ reconnecting }) {
    if (!alive || view.status === "final") return;
    if (reconnecting) {
      conn = "reconnecting";
      renderStatus();
      showNotice("Reconnecting…");
      return;
    }
    source?.close();
    const reason = !receivedAny && streamUrl ? await explainStreamFailure(streamUrl) : null;
    if (!alive) return;
    conn = "lost";
    renderStatus();
    if (!receivedAny && reason) {
      header.innerHTML = `<p class="notice">${esc(reason)}</p><p><a href="#/">Back to tonight's games</a></p>`;
      return;
    }
    showNotice(`Connection lost. <button type="button" class="btn btn-quiet btn-sm" id="retry">Try again</button>`);
    notice.querySelector("#retry")?.addEventListener("click", () => window.location.reload());
  }

  function connect(url) {
    streamUrl = url;
    conn = "connecting";
    source = connectStream(url, {
      wp_update: onPoint,
      anomaly: onAnomaly,
      game_end: onEnd,
      degraded: () => showNotice("The data source isn't responding right now. Still trying…"),
      heartbeat: () => {
        lastSeen = Date.now();
        if (conn === "reconnecting" || conn === "connecting") conn = "live";
        renderStatus();
      },
      onopen: () => {
        lastSeen = Date.now();
        if (conn !== "ended") conn = "live";
        showNotice("");
        renderStatus();
      },
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
        if (game) {
          view.home = game.home.tricode;
          view.away = game.away.tricode;
          if (game.status === "final") view.status = "final";
        }
        // A game that has dropped off today's scoreboard (it ended after
        // midnight Eastern, say) may still be held by the server for a day,
        // so try the stream either way; the server answers 404 if it isn't.
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
      conn = "lost";
      renderStatus();
      header.innerHTML = `<p class="notice">${esc(error.message)}</p><p><a href="#/">Back to the dashboard</a></p>`;
    }
  }

  renderSound();
  renderRun();
  renderStatus();
  const ticker = setInterval(renderStatus, 1000);
  start();

  return () => {
    alive = false;
    clearInterval(ticker);
    if (headerFrame !== null) cancelAnimationFrame(headerFrame);
    source?.close();
    chart?.destroy();
    audio?.close?.();
  };
}
