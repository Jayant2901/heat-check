// The dashboard: tonight's games, the week's moments and results, the best
// performances, replays, and how the numbers are computed.

import { api } from "./api.js";
import { duration, localTime, percent, shortDate } from "./format.js";
import { badge, esc, teamName } from "./teams.js";

const SCOREBOARD_REFRESH_MS = 15000;
const WEEK_POLL_MS = 2500;
const WEEK_ERROR_RETRY_MS = 30000;
const RESULTS_PREVIEW = 8;
const REPLAY_SPEEDS = [5, 10, 30, 100];

function layout() {
  return `
    <section class="section" id="tonight" aria-labelledby="tonight-title">
      <div class="section-head">
        <h1 id="tonight-title">Tonight</h1>
        <p class="section-meta" id="tonight-meta" aria-live="polite"></p>
      </div>
      <div id="tonight-body"><p class="muted">Loading today's games…</p></div>
    </section>

    <div class="dashboard-grid">
      <section class="section area-moments" aria-labelledby="moments-title">
        <div class="section-head">
          <h2 id="moments-title">Moments of the week</h2>
        </div>
        <div id="moments-body"><p class="muted">Loading…</p></div>
      </section>

      <section class="section area-results" id="results" aria-labelledby="results-title">
        <div class="section-head">
          <h2 id="results-title">Results</h2>
          <p class="section-meta" id="results-meta"></p>
        </div>
        <div id="results-body"><p class="muted">Loading…</p></div>
      </section>

      <aside class="section area-best" aria-labelledby="best-title">
        <div class="section-head">
          <h2 id="best-title">Best performances</h2>
          <p class="section-meta">Ranked by Game Score</p>
        </div>
        <div id="best-body"><p class="muted">Loading…</p></div>
      </aside>

      <section class="section area-replay" aria-labelledby="replay-title">
        <details class="replays">
          <summary><h2 id="replay-title">Replay a classic game</h2></summary>
          <div id="replay-body" class="replay-grid"><p class="muted">Loading…</p></div>
        </details>
      </section>

      <section class="section area-how" id="how" aria-labelledby="how-title">
        <div class="section-head">
          <h2 id="how-title">How the numbers work</h2>
        </div>
        <div id="how-body" class="how-grid"><p class="muted">Loading…</p></div>
      </section>
    </div>`;
}

// ---------------------------------------------------------------- tonight

function leaderLine(leader) {
  if (!leader) return "";
  return `${esc(leader.name)} · ${leader.points} pts, ${leader.rebounds} reb, ${leader.assists} ast`;
}

function teamRow(side, game, { showScore, dimmed }) {
  const t = game[side];
  const record = `${t.wins}-${t.losses}`;
  return `
    <div class="tile-team${dimmed ? " is-dimmed" : ""}">
      ${badge(t.tricode)}
      <span class="tile-team-name">${esc(t.name || teamName(t.tricode))}</span>
      <span class="tile-record">${esc(record)}</span>
      ${showScore ? `<span class="tile-score num">${t.score}</span>` : ""}
    </div>`;
}

function gameTile(game) {
  const live = game.status === "live";
  const final = game.status === "final";
  const homeWon = game.home.score > game.away.score;

  let chip;
  let when;
  if (live) {
    chip = `<span class="chip chip-live"><span class="live-dot" aria-hidden="true"></span>Live</span>`;
    when = esc(game.clock_text || game.status_text);
  } else if (final) {
    chip = `<span class="chip">Final</span>`;
    when = "";
  } else {
    chip = `<span class="chip">Upcoming</span>`;
    when = esc(localTime(game.start_time_utc) || game.status_text);
  }

  const rows = [
    teamRow("away", game, { showScore: !game.status.startsWith("sched"), dimmed: final && homeWon }),
    teamRow("home", game, { showScore: !game.status.startsWith("sched"), dimmed: final && !homeWon }),
  ].join("");

  const leaders = live
    ? `<div class="tile-leaders">
         ${game.away_leader ? `<p>${badge(game.away.tricode, { size: "sm" })} ${leaderLine(game.away_leader)}</p>` : ""}
         ${game.home_leader ? `<p>${badge(game.home.tricode, { size: "sm" })} ${leaderLine(game.home_leader)}</p>` : ""}
       </div>`
    : "";

  const label = `${game.away.tricode} at ${game.home.tricode}`;
  const inner = `
    <div class="tile-top">${chip}<span class="tile-when">${when}</span></div>
    ${rows}
    ${leaders}
    ${live ? `<span class="tile-cta">Follow live <span aria-hidden="true">→</span></span>` : ""}`;

  if (live) {
    return `<a class="tile tile-live" href="#/live/${encodeURIComponent(game.game_id)}" aria-label="Follow ${esc(label)} live">${inner}</a>`;
  }
  return `<article class="tile${final ? " tile-final" : ""}" aria-label="${esc(label)}">${inner}</article>`;
}

function renderTonight(container, meta, board) {
  const games = board.games || [];
  const liveCount = games.filter((g) => g.status === "live").length;
  meta.textContent = games.length
    ? `${games.length} game${games.length === 1 ? "" : "s"}${liveCount ? `, ${liveCount} live` : ""}`
    : "";
  if (!games.length) {
    container.innerHTML = `
      <div class="empty">
        <p>No NBA games today.</p>
        <p class="muted"><a href="#/?section=results">See this week's results</a> or replay a classic game below.</p>
      </div>`;
    return;
  }
  container.innerHTML = `<div class="tile-grid">${games.map(gameTile).join("")}</div>`;
}

// ---------------------------------------------------------------- moments

function swingCard(s) {
  const lo = Math.min(s.wp_before, s.wp_after) * 100;
  const hi = Math.max(s.wp_before, s.wp_after) * 100;
  const tags = s.tags.map((tag) => `<span class="chip chip-tag">${esc(tag)}</span>`).join("");
  return `
    <article class="moment">
      <header class="moment-head">
        ${badge(s.team)}
        <p class="moment-where">${esc(s.matchup)} · ${esc(shortDate(s.date))} · ${esc(s.when)}</p>
      </header>
      <p class="moment-title">
        ${esc(s.team)} win chance <span class="num">${percent(s.wp_before)}</span>
        to <span class="num gain">${percent(s.wp_after)}</span>
      </p>
      <div class="swing-bar" role="img" aria-label="Win chance moved from ${percent(s.wp_before)} to ${percent(s.wp_after)}">
        <span class="swing-mid" style="left:50%"></span>
        <span class="swing-moved" style="left:${lo}%;width:${hi - lo}%"></span>
        <span class="swing-dot" style="left:${s.wp_after * 100}%"></span>
      </div>
      <p class="moment-detail">${esc(s.detail)} <span class="muted">(${esc(s.score)})</span></p>
      ${tags ? `<div class="chip-row">${tags}</div>` : ""}
    </article>`;
}

function runCard(r) {
  return `
    <article class="moment">
      <header class="moment-head">
        ${badge(r.team)}
        <p class="moment-where">${esc(r.matchup)} · ${esc(shortDate(r.date))} · ${esc(r.when)}</p>
      </header>
      <p class="moment-title">
        <span class="num">${r.points}-0</span> in ${esc(duration(r.duration_sec))}
        <span class="chip chip-heat num" title="Standard deviations faster than usual for this point in a game">${r.z_score.toFixed(1)}σ</span>
      </p>
      <p class="moment-detail">${esc(r.team)} run, ${esc(r.when)} to ${esc(r.ends)}. Score after: ${esc(r.score_after)}</p>
    </article>`;
}

function renderMoments(container, week) {
  const { swings, runs } = week.moments;
  container.innerHTML = `
    <div class="tabs" role="tablist" aria-label="Moments">
      <button class="tab" role="tab" id="tab-swings" aria-controls="panel-swings" aria-selected="true">Biggest swings</button>
      <button class="tab" role="tab" id="tab-runs" aria-controls="panel-runs" aria-selected="false" tabindex="-1">Hottest runs</button>
    </div>
    <div class="tab-panel moment-grid" role="tabpanel" id="panel-swings" aria-labelledby="tab-swings">
      ${swings.length ? swings.map(swingCard).join("") : `<p class="muted">No big swings this week.</p>`}
    </div>
    <div class="tab-panel moment-grid" role="tabpanel" id="panel-runs" aria-labelledby="tab-runs" hidden>
      ${runs.length ? runs.map(runCard).join("") : `<p class="muted">No unusually quick runs this week.</p>`}
    </div>`;

  const tabs = [...container.querySelectorAll('[role="tab"]')];
  const select = (tab) => {
    for (const t of tabs) {
      const selected = t === tab;
      t.setAttribute("aria-selected", String(selected));
      t.tabIndex = selected ? 0 : -1;
      container.querySelector(`#${t.getAttribute("aria-controls")}`).hidden = !selected;
    }
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      select(next);
      next.focus();
    });
  });
}

// ---------------------------------------------------------------- results

function resultRow(g) {
  const awayWon = g.winner === g.away;
  const line = g.top_performer
    ? `${esc(g.top_performer.name)} (${esc(g.top_performer.team)}) ${esc(g.top_performer.line)}`
    : "";
  const lead = g.largest_lead
    ? `largest lead ${g.largest_lead}${g.largest_lead_team ? ` (${esc(g.largest_lead_team)})` : ""}`
    : "";
  const flow = [`${g.lead_changes} lead change${g.lead_changes === 1 ? "" : "s"}`, lead].filter(Boolean).join(", ");
  return `
    <li class="result">
      <p class="result-date">${esc(shortDate(g.date))}</p>
      <div class="result-teams">
        <p class="${awayWon ? "is-winner" : ""}">${badge(g.away, { size: "sm" })} ${esc(g.away)} <span class="num">${g.away_score}</span></p>
        <p class="${awayWon ? "" : "is-winner"}">${badge(g.home, { size: "sm" })} ${esc(g.home)} <span class="num">${g.home_score}</span></p>
      </div>
      <div class="result-notes">
        ${line ? `<p>${line}</p>` : ""}
        <p class="muted">${flow}</p>
      </div>
    </li>`;
}

function renderResults(container, meta, week) {
  const games = week.games;
  const skipped = week.games_skipped ? `, ${week.games_skipped} couldn't be read` : "";
  meta.textContent = `${shortDate(week.window.start)} to ${shortDate(week.window.end)}${skipped}`;
  if (!games.length) {
    container.innerHTML = `<p class="muted">No finished games in the last week.</p>`;
    return;
  }
  const extra = games.length > RESULTS_PREVIEW;
  container.innerHTML = `
    <ol class="results">${games.map(resultRow).join("")}</ol>
    ${extra ? `<button class="button button-quiet" type="button" aria-expanded="false">Show all ${games.length} games</button>` : ""}`;
  const rows = [...container.querySelectorAll(".result")];
  rows.forEach((row, index) => {
    row.hidden = index >= RESULTS_PREVIEW;
  });
  const toggle = container.querySelector("button");
  toggle?.addEventListener("click", () => {
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    rows.forEach((row, index) => {
      row.hidden = expanded && index >= RESULTS_PREVIEW;
    });
    toggle.setAttribute("aria-expanded", String(!expanded));
    toggle.textContent = expanded ? `Show all ${games.length} games` : "Show fewer";
  });
}

// ---------------------------------------------------------------- best

function renderBest(container, week) {
  const top = week.top_performances;
  if (!top.length) {
    container.innerHTML = `<p class="muted">No box scores yet.</p>`;
    return;
  }
  const performances = top
    .map(
      (p, i) => `
      <li class="perf">
        <span class="perf-rank num">${i + 1}</span>
        <div class="perf-body">
          <p class="perf-name">${esc(p.name)} ${badge(p.team, { size: "sm" })}</p>
          <p class="perf-line">${esc(p.line)}, ${esc(p.fg)} FG</p>
          <p class="muted">vs ${esc(p.opponent)} · ${esc(p.result)} · ${esc(shortDate(p.date))}</p>
        </div>
        <span class="perf-score num" title="Game Score">${p.game_score.toFixed(1)}</span>
      </li>`,
    )
    .join("");

  const leaderList = (title, rows, unit) => `
    <div class="leader-list">
      <h3>${title}</h3>
      <ol>
        ${rows
          .map(
            (r) => `<li><span>${esc(r.name)} ${badge(r.team, { size: "sm" })}</span><span class="num">${r.value} ${unit}</span></li>`,
          )
          .join("")}
      </ol>
    </div>`;

  const consistent = week.week_averages.length
    ? `
      <div class="leader-list">
        <h3>Most consistent</h3>
        <p class="muted small">Average Game Score, 2+ games this week</p>
        <ol>
          ${week.week_averages
            .map(
              (a) => `<li><span>${esc(a.name)} ${badge(a.team, { size: "sm" })}</span><span class="num">${a.avg_game_score.toFixed(1)} <span class="muted">in ${a.games}</span></span></li>`,
            )
            .join("")}
        </ol>
      </div>`
    : "";

  container.innerHTML = `
    <ol class="perfs">${performances}</ol>
    <div class="leaders">
      ${leaderList("Points", week.leaders.points, "pts")}
      ${leaderList("Rebounds", week.leaders.rebounds, "reb")}
      ${leaderList("Assists", week.leaders.assists, "ast")}
    </div>
    ${consistent}`;
}

// ---------------------------------------------------------------- week states

function renderWeekBuilding(progress, targets) {
  const { done, total } = progress || { done: 0, total: 0 };
  const counts = total ? ` ${done} of ${total}` : "";
  const bar = total
    ? `<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><span style="width:${(done / total) * 100}%"></span></div>`
    : "";
  const html = `<div class="building"><p>Reading this week's games…${counts}</p>${bar}</div>`;
  targets.moments.innerHTML = html;
  targets.results.innerHTML = `<p class="muted">Results appear once the week is read.</p>`;
  targets.best.innerHTML = `<p class="muted">Box scores appear once the week is read.</p>`;
}

function renderWeekError(message, targets) {
  const html = `<p class="notice">${esc(message)} Trying again shortly.</p>`;
  targets.moments.innerHTML = html;
  targets.results.innerHTML = "";
  targets.best.innerHTML = `<p class="muted">Unavailable right now.</p>`;
}

// ---------------------------------------------------------------- replays

function renderReplays(container, data) {
  const fixtures = data.fixtures || [];
  if (!fixtures.length) {
    container.innerHTML = `<p class="muted">No replays are installed.</p>`;
    return;
  }
  container.innerHTML = fixtures
    .map((f) => {
      const selectId = `speed-${esc(f.game_id)}`;
      const options = REPLAY_SPEEDS.map(
        (s) => `<option value="${s}"${s === 10 ? " selected" : ""}>${s}× speed</option>`,
      ).join("");
      return `
        <article class="replay-card">
          <p class="replay-label">${esc(f.label)}</p>
          <p class="replay-teams">
            ${badge(f.away_team, { size: "sm" })} ${esc(f.away_team)} <span class="num">${f.final_away_score}</span>
            <span class="muted">at</span>
            ${badge(f.home_team, { size: "sm" })} ${esc(f.home_team)} <span class="num">${f.final_home_score}</span>
          </p>
          <p class="muted small">${esc(f.season)} season</p>
          <div class="replay-actions">
            <label class="visually-hidden" for="${selectId}">Replay speed</label>
            <select id="${selectId}" class="select">${options}</select>
            <a class="button" href="#/replay/${encodeURIComponent(f.game_id)}?speed=10" data-fixture="${esc(f.game_id)}">Watch</a>
          </div>
        </article>`;
    })
    .join("");
  for (const select of container.querySelectorAll("select")) {
    const link = select.parentElement.querySelector("a");
    select.addEventListener("change", () => {
      link.href = `#/replay/${encodeURIComponent(link.dataset.fixture)}?speed=${select.value}`;
    });
  }
}

// ---------------------------------------------------------------- how

function renderHow(container, info) {
  const pct = (x) => `${(x * 100).toFixed(1)}%`;
  const num = (x) => Number(x).toLocaleString();
  const seasons = (info.seasons || []).slice().sort().join(", ");
  container.innerHTML = `
    <article class="how">
      <h3>Win chance</h3>
      <p>
        A gradient-boosted model trained on ${esc((info.train_seasons || []).join(" and "))} and tested on the
        held-out ${esc(info.test_season)} season (${num(info.n_test_rows)} game states). Its favourite won
        <strong>${pct(info.accuracy)}</strong> of the time across every moment of those games, with a log loss of
        ${info.log_loss.toFixed(3)} against ${info.logistic_log_loss.toFixed(3)}
        for a plain logistic regression (lower is better).
      </p>
      <p class="muted">
        It only knows the score margin, the time left and the quarter. It has no idea how good either team is,
        so every game starts near 50%.
      </p>
    </article>
    <article class="how">
      <h3>Heat checks</h3>
      <p>
        During a game the server reads the box score every ${info.poll_interval_seconds} seconds. When a team scores
        6 or more unanswered points, the time it took is compared with ${num(info.n_runs)} runs from ${esc(seasons)}
        at the same stage of a game. Runs at least ${info.anomaly_z_threshold} standard deviations quicker than usual are flagged.
      </p>
      <p class="muted">
        That threshold was set by looking at three games, not by a proper evaluation. A ${info.poll_interval_seconds}-second
        poll can merge baskets, so a replay (sampled every few seconds) and the same game live can flag different runs.
        The run history also includes ${esc(info.test_season)}, the season the model was tested on.
      </p>
    </article>
    <article class="how">
      <h3>Moments of the week</h3>
      <p>
        Read from each finished game's play-by-play. A swing is a single play that moved the win chance by at least
        8 points, comparing the moment just before and just after it. A hot run is an unanswered run of 8+ points that was
        at least ${info.weekly_run_min_z} standard deviations quicker than usual, timed to the second.
      </p>
      <p class="muted">
        Game Score is John Hollinger's one-number summary of a box score line: roughly points, plus credit for
        rebounds, assists, steals and blocks, minus missed shots, fouls and turnovers.
      </p>
    </article>`;
}

// ---------------------------------------------------------------- wiring

export function renderDashboard(root, { section, onMode }) {
  root.innerHTML = layout();
  const $ = (id) => root.querySelector(`#${id}`);
  const targets = { moments: $("moments-body"), results: $("results-body"), best: $("best-body") };
  const timers = new Set();
  let alive = true;
  let sample = false;

  const later = (fn, ms) => {
    const id = setTimeout(() => {
      timers.delete(id);
      if (alive) fn();
    }, ms);
    timers.add(id);
  };

  const markMode = (mode) => {
    if (mode === "demo" && !sample) {
      sample = true;
      onMode(true);
    } else if (mode === "live" && sample) {
      sample = false;
      onMode(false);
    }
  };

  async function loadScoreboard() {
    if (document.visibilityState !== "visible") {
      later(loadScoreboard, SCOREBOARD_REFRESH_MS);
      return;
    }
    try {
      const board = await api.scoreboard();
      if (!alive) return;
      markMode(board.mode);
      renderTonight($("tonight-body"), $("tonight-meta"), board);
    } catch (error) {
      if (!alive) return;
      $("tonight-body").innerHTML = `<p class="notice">${esc(error.message)}</p>`;
    }
    later(loadScoreboard, SCOREBOARD_REFRESH_MS);
  }

  async function loadWeek() {
    let next = WEEK_POLL_MS;
    try {
      const week = await api.week();
      if (!alive) return;
      if (week.status === "building") {
        renderWeekBuilding(week.progress, targets);
      } else if (week.status === "error") {
        renderWeekError(week.message, targets);
        next = WEEK_ERROR_RETRY_MS;
      } else {
        markMode(week.mode);
        renderMoments(targets.moments, week);
        renderResults(targets.results, $("results-meta"), week);
        renderBest(targets.best, week);
        return;
      }
    } catch (error) {
      if (!alive) return;
      renderWeekError(error.message, targets);
      next = WEEK_ERROR_RETRY_MS;
    }
    later(loadWeek, next);
  }

  async function loadOnce(fetcher, target, render) {
    try {
      const data = await fetcher();
      if (alive) render(target, data);
    } catch (error) {
      if (alive) target.innerHTML = `<p class="notice">${esc(error.message)}</p>`;
    }
  }

  const onVisible = () => {
    if (document.visibilityState === "visible") loadScoreboardNow();
  };
  let inflight = false;
  async function loadScoreboardNow() {
    if (inflight) return;
    inflight = true;
    try {
      const board = await api.scoreboard();
      if (alive) {
        markMode(board.mode);
        renderTonight($("tonight-body"), $("tonight-meta"), board);
      }
    } catch {
      // the regular refresh will report it
    } finally {
      inflight = false;
    }
  }

  loadScoreboard();
  loadWeek();
  loadOnce(api.replays, $("replay-body"), renderReplays);
  loadOnce(api.modelInfo, $("how-body"), renderHow);
  document.addEventListener("visibilitychange", onVisible);

  if (section && /^[a-z]+$/.test(section)) {
    requestAnimationFrame(() => $(section)?.scrollIntoView({ block: "start" }));
  }

  return () => {
    alive = false;
    for (const id of timers) clearTimeout(id);
    timers.clear();
    document.removeEventListener("visibilitychange", onVisible);
  };
}
