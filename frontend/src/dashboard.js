// The dashboard: hero, marquee, tonight's games, this week (moments,
// results, best performances), replays, how the numbers work, and a
// closing poster.

import { api } from "./api.js";
import { duration, localTime, percent, prefersReducedMotion, shortDate } from "./format.js";
import { createReveal } from "./reveal.js";
import { badge, esc, teamName } from "./teams.js";

const SCOREBOARD_REFRESH_MS = 15000;
const WEEK_POLL_MS = 2500;
const WEEK_ERROR_RETRY_MS = 30000;
const RESULTS_PREVIEW = 8;
const REPLAY_SPEEDS = [5, 10, 30, 100];
const DEFAULT_SPEED = 10;

const GENERIC_MARQUEE = [
  "Heat check",
  "Win probability, live",
  "Unusual runs flagged automatically",
  "Box score read every 12 seconds",
  "Replay a classic game",
];

const ARROW_RIGHT = `
  <svg class="icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
    <path d="M2 8h11M9 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" />
  </svg>`;

export function scrollToSection(id) {
  const target = document.getElementById(id);
  if (!target) return;
  target.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
}

function sectionHead(index, id, title, metaId = "") {
  return `
    <div class="section-head" data-reveal>
      <div>
        <p class="kicker section-index">${index}</p>
        <h2 id="${id}">${title}</h2>
      </div>
      ${metaId ? `<p class="section-meta" id="${metaId}" aria-live="polite"></p>` : ""}
    </div>`;
}

function layout() {
  return `
    <section class="hero" id="top" aria-labelledby="hero-title" data-reveal>
      <p class="eyebrow"><span class="eyebrow-bar" aria-hidden="true"></span>Live NBA win probability</p>
      <h1 class="hero-title" id="hero-title">Every run has a <span class="accent">temperature</span></h1>
      <p class="lede">
        Any broadcast can tell you a team is on an 8-0 run. This z-scores that run against three seasons
        of real ones and tells you whether it is actually unusual.
      </p>
      <div class="hero-actions">
        <a class="btn btn-primary" href="#/?section=replays">Watch a replay ${ARROW_RIGHT}</a>
        <a class="btn btn-ghost" href="#/?section=how">Read the method</a>
      </div>
    </section>

    <div class="marquee bleed" aria-hidden="true">
      <div class="marquee-row" id="marquee-row"></div>
    </div>

    <section class="section" id="tonight" aria-labelledby="tonight-title">
      ${sectionHead("01", "tonight-title", "Tonight", "tonight-meta")}
      <div id="tonight-body" data-reveal><p class="muted">Loading today's games…</p></div>
    </section>

    <section class="section" id="week" aria-labelledby="week-title">
      ${sectionHead("02", "week-title", "This week", "results-meta")}
      <div class="week-grid">
        <div class="area-moments" data-reveal>
          <div class="sub-head"><h3>Moments of the week</h3></div>
          <div id="moments-body"><p class="muted">Loading…</p></div>
        </div>
        <div class="area-results" data-reveal>
          <div class="sub-head"><h3>Results</h3></div>
          <div id="results-body"><p class="muted">Loading…</p></div>
        </div>
        <aside class="area-best" aria-labelledby="best-title" data-reveal>
          <div class="sub-head">
            <h3 id="best-title">Best performances</h3>
            <p class="kicker muted">By Game Score</p>
          </div>
          <div id="best-body"><p class="muted">Loading…</p></div>
        </aside>
      </div>
    </section>

    <section class="section" id="replays" aria-labelledby="replay-title">
      ${sectionHead("03", "replay-title", "Replay a classic game")}
      <div id="replay-body" class="replay-grid" data-reveal><p class="muted">Loading…</p></div>
    </section>

    <section class="section" id="how" aria-labelledby="how-title">
      ${sectionHead("04", "how-title", "How the numbers work")}
      <div id="how-body" data-reveal><p class="muted">Loading…</p></div>
    </section>

    <section class="poster bleed" aria-label="Closing">
      <p>Momentum is a feeling. <span class="accent">Sigma is a number.</span></p>
    </section>`;
}

// ---------------------------------------------------------------- marquee

function renderMarquee(row, phrases) {
  // Each track repeats the phrases until it is comfortably wider than any
  // screen, so translating the row by -50% (one track) loops seamlessly.
  const items = [];
  while (items.length < 12) items.push(...phrases);
  const track = `<div class="marquee-track">${items
    // Uppercasing would turn σ into Σ (a different symbol), so keep it as is.
    .map((p) => `<span class="marquee-item">${esc(p).replaceAll("σ", '<span class="sigma">σ</span>')}</span>`)
    .join("")}</div>`;
  row.innerHTML = track + track;
}

function weekPhrases(week) {
  const runs = week.moments.runs.slice(0, 3).map(
    (r) => `${r.team} ${r.points}-0 in ${duration(r.duration_sec)} · ${r.z_score.toFixed(1)}σ`,
  );
  const swings = week.moments.swings.slice(0, 2).map(
    (s) => `${s.team} win chance ${percent(s.wp_before)} to ${percent(s.wp_after)}`,
  );
  return [...runs, ...swings];
}

// ---------------------------------------------------------------- tonight

function leaderLine(leader) {
  if (!leader) return "";
  return `${esc(leader.name)} · ${leader.points} pts, ${leader.rebounds} reb, ${leader.assists} ast`;
}

function teamRow(side, game, { showScore, dimmed }) {
  const t = game[side];
  return `
    <div class="tile-team${dimmed ? " is-dimmed" : ""}">
      ${badge(t.tricode)}
      <span class="tile-team-name">${esc(t.name || teamName(t.tricode))}</span>
      <span class="tile-record">${t.wins}-${t.losses}</span>
      ${showScore ? `<span class="tile-score">${t.score}</span>` : ""}
    </div>`;
}

function gameTile(game) {
  const live = game.status === "live";
  const final = game.status === "final";
  const scheduled = game.status === "scheduled";
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
    teamRow("away", game, { showScore: !scheduled, dimmed: final && homeWon }),
    teamRow("home", game, { showScore: !scheduled, dimmed: final && !homeWon }),
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
    ${live ? `<span class="tile-cta">Follow live ${ARROW_RIGHT}</span>` : ""}`;

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
      <div class="card empty-card">
        <div>
          <h3>No NBA games today</h3>
          <p class="muted">
            See <a href="#/?section=week">this week's results</a> or
            <a href="#/?section=replays">replay a classic game</a>.
          </p>
        </div>
        <span class="chip">No games</span>
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
        ${esc(s.team)} win chance ${percent(s.wp_before)} to <span class="gain">${percent(s.wp_after)}</span>
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
        ${r.points}-0 in ${esc(duration(r.duration_sec))}
        <span class="chip chip-heat" title="Standard deviations quicker than usual for this point in a game">${r.z_score.toFixed(1)}σ</span>
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
    <div class="moment-grid" role="tabpanel" id="panel-swings" aria-labelledby="tab-swings">
      ${swings.length ? swings.map(swingCard).join("") : `<p class="muted">No big swings this week.</p>`}
    </div>
    <div class="moment-grid" role="tabpanel" id="panel-runs" aria-labelledby="tab-runs" hidden>
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
    ${extra ? `<button class="btn btn-quiet btn-sm" type="button" aria-expanded="false">Show all ${games.length} games</button>` : ""}`;
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
        <span class="perf-rank">${String(i + 1).padStart(2, "0")}</span>
        <div class="perf-body">
          <p class="perf-name">${esc(p.name)} ${badge(p.team, { size: "sm" })}</p>
          <p>${esc(p.line)}, ${esc(p.fg)} FG</p>
          <p class="muted">vs ${esc(p.opponent)} · ${esc(p.result)} · ${esc(shortDate(p.date))}</p>
        </div>
        <span class="perf-score" title="Game Score">${p.game_score.toFixed(1)}</span>
      </li>`,
    )
    .join("");

  const leaderList = (title, rows, unit) => `
    <div class="leader-list">
      <h4 class="kicker">${title}</h4>
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
        <h4 class="kicker">Most consistent</h4>
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
  targets.moments.innerHTML = `<div class="building"><p>Reading this week's games…${counts}</p>${bar}</div>`;
  targets.results.innerHTML = `<p class="muted">Results appear once the week is read.</p>`;
  targets.best.innerHTML = `<p class="muted">Box scores appear once the week is read.</p>`;
}

function renderWeekError(message, targets) {
  targets.moments.innerHTML = `<p class="notice">${esc(message)} Trying again shortly.</p>`;
  targets.results.innerHTML = "";
  targets.best.innerHTML = `<p class="muted">Unavailable right now.</p>`;
}

// ---------------------------------------------------------------- replays

function replayHref(fixtureId, speed) {
  return `#/replay/${encodeURIComponent(fixtureId)}?speed=${speed}`;
}

function renderReplays(container, data) {
  const fixtures = data.fixtures || [];
  if (!fixtures.length) {
    container.innerHTML = `<p class="muted">No replays are installed.</p>`;
    return;
  }
  container.innerHTML = fixtures
    .map((f, n) => {
      const group = `speed-${n}`;
      const options = REPLAY_SPEEDS.map(
        (s) => `
          <label class="seg-opt">
            <input type="radio" name="${group}" value="${s}"${s === DEFAULT_SPEED ? " checked" : ""} />
            <span>${s}×</span>
          </label>`,
      ).join("");
      return `
        <article class="card replay-card">
          <p class="replay-matchup">
            ${badge(f.away_team)} ${esc(f.away_team)}
            <span class="kicker">Final</span>
            ${esc(f.home_team)} ${badge(f.home_team)}
          </p>
          <p class="replay-score">${f.final_away_score}–${f.final_home_score}</p>
          <div class="replay-meta">
            <h3>${esc(f.label)}</h3>
            <p class="muted small">${esc(f.season)} · ${esc(f.away_team)} at ${esc(f.home_team)}</p>
          </div>
          <fieldset class="seg">
            <legend class="visually-hidden">Replay speed for ${esc(f.label)}</legend>
            ${options}
          </fieldset>
          <a class="btn btn-primary btn-block" href="${replayHref(f.game_id, DEFAULT_SPEED)}" data-fixture="${esc(f.game_id)}">
            Watch replay ${ARROW_RIGHT}
          </a>
        </article>`;
    })
    .join("");

  for (const card of container.querySelectorAll(".replay-card")) {
    const link = card.querySelector("a[data-fixture]");
    card.querySelector("fieldset").addEventListener("change", (event) => {
      link.href = replayHref(link.dataset.fixture, event.target.value);
    });
  }
}

// ---------------------------------------------------------------- how

function renderHow(container, info) {
  const num = (x) => Number(x).toLocaleString();
  const seasons = (info.seasons || []).slice().sort().join(", ");
  const trained = (info.train_seasons || []).join(" and ");
  const poll = info.poll_interval_seconds;

  const stat = (value, attrs, caption) => `
    <div class="stat">
      <strong class="stat-num" ${attrs}>${value}</strong>
      <span class="kicker stat-cap">${caption}</span>
    </div>`;
  const accuracy = info.accuracy * 100;

  const rows = [
    {
      title: "Poll",
      body: `<p>During a game the server reads NBA.com's box score every ${poll} seconds: score, quarter and clock.</p>`,
    },
    {
      title: "Recompute",
      body: `
        <p>
          A gradient-boosted model trained on ${esc(trained)} and tested on the held-out ${esc(info.test_season)}
          season turns that into each team's chance of winning. Its favourite won ${accuracy.toFixed(1)}% of the
          time across every moment of those games, with a log loss of ${info.log_loss.toFixed(3)} against
          ${info.logistic_log_loss.toFixed(3)} for a plain logistic regression (lower is better).
        </p>
        <p class="muted">
          It only knows the score margin, the time left and the quarter. It has no idea how good either team is,
          so every game starts near 50%.
        </p>`,
    },
    {
      title: "Flag",
      body: `
        <p>
          When a team scores 6 or more unanswered points, the time it took is compared with ${num(info.n_runs)} runs
          from ${esc(seasons)} at the same stage of a game. Runs at least ${info.anomaly_z_threshold} standard
          deviations quicker than usual are flagged as heat checks.
        </p>
        <p class="muted">
          That threshold was set by looking at three games, not by a proper evaluation. A ${poll}-second poll can
          merge baskets, so a replay (sampled every few seconds) and the same game live can flag different runs.
          The run history also includes ${esc(info.test_season)}, the season the model was tested on.
        </p>`,
    },
    {
      title: "Recap",
      body: `
        <p>
          Moments of the week are read from each finished game's play-by-play. A swing is a single play that moved
          the win chance by at least 8 points, comparing the moment just before and just after it. A hot run is an
          unanswered run of 8+ points at least ${info.weekly_run_min_z} standard deviations quicker than usual,
          timed to the second.
        </p>
        <p class="muted">
          Game Score is John Hollinger's one-number summary of a box score line: roughly points, plus credit for
          rebounds, assists, steals and blocks, minus missed shots, fouls and turnovers.
        </p>`,
    },
  ];

  container.innerHTML = `
    <div class="stat-strip">
      <div class="stat-grid">
        ${stat(num(info.n_runs), `data-count="${info.n_runs}"`, "Real 6+ point runs in the baseline")}
        ${stat(`${accuracy.toFixed(1)}%`, `data-count="${accuracy}" data-decimals="1" data-suffix="%"`, `Favourite won, held-out ${esc(info.test_season)}`)}
        ${stat(num(info.n_test_rows), `data-count="${info.n_test_rows}"`, "Held-out game states tested")}
        ${stat(`${poll}s`, `data-count="${poll}" data-suffix="s"`, "Between box-score reads")}
      </div>
    </div>
    <ol class="pipeline">
      ${rows
        .map(
          (row, i) => `
          <li class="pipe-row">
            <span class="pipe-index">${String(i + 1).padStart(2, "0")}</span>
            <h3>${row.title}</h3>
            <div class="pipe-body">${row.body}</div>
          </li>`,
        )
        .join("")}
    </ol>`;
}

// ---------------------------------------------------------------- wiring

export function renderDashboard(root, { section, onMode }) {
  root.innerHTML = layout();
  const $ = (id) => root.querySelector(`#${id}`);
  const targets = { moments: $("moments-body"), results: $("results-body"), best: $("best-body") };
  const reveal = createReveal(root);
  const timers = new Set();
  let alive = true;
  let sample = false;

  renderMarquee($("marquee-row"), GENERIC_MARQUEE);
  reveal.scan();

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

  let inflight = false;
  async function refreshScoreboard() {
    if (inflight) return;
    inflight = true;
    try {
      const board = await api.scoreboard();
      if (!alive) return;
      markMode(board.mode);
      renderTonight($("tonight-body"), $("tonight-meta"), board);
    } catch (error) {
      if (alive) $("tonight-body").innerHTML = `<p class="notice">${esc(error.message)}</p>`;
    } finally {
      inflight = false;
    }
  }

  async function scoreboardLoop() {
    if (document.visibilityState === "visible") await refreshScoreboard();
    later(scoreboardLoop, SCOREBOARD_REFRESH_MS);
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
        const phrases = weekPhrases(week);
        if (phrases.length) renderMarquee($("marquee-row"), phrases);
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
      if (!alive) return;
      render(target, data);
      reveal.scan();
      target.querySelectorAll("[data-count]").forEach((el) => reveal.count(el));
    } catch (error) {
      if (alive) target.innerHTML = `<p class="notice">${esc(error.message)}</p>`;
    }
  }

  const onVisible = () => {
    if (document.visibilityState === "visible") refreshScoreboard();
  };

  scoreboardLoop();
  loadWeek();
  loadOnce(api.replays, $("replay-body"), renderReplays);
  loadOnce(api.modelInfo, $("how-body"), renderHow);
  document.addEventListener("visibilitychange", onVisible);

  if (section && /^[a-z]+$/.test(section)) {
    requestAnimationFrame(() => scrollToSection(section));
  }

  return () => {
    alive = false;
    for (const id of timers) clearTimeout(id);
    timers.clear();
    reveal.destroy();
    document.removeEventListener("visibilitychange", onVisible);
  };
}
