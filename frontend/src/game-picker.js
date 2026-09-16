async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

const TEAM_META = {
  ATL: ["Atlanta Hawks", "#e03a3e"], BOS: ["Boston Celtics", "#007a33"], BKN: ["Brooklyn Nets", "#000000"],
  CHA: ["Charlotte Hornets", "#1d1160"], CHI: ["Chicago Bulls", "#ce1141"], CLE: ["Cleveland Cavaliers", "#860038"],
  DAL: ["Dallas Mavericks", "#00538c"], DEN: ["Denver Nuggets", "#0e2240"], DET: ["Detroit Pistons", "#c8102e"],
  GSW: ["Golden State Warriors", "#1d428a"], HOU: ["Houston Rockets", "#ce1141"], IND: ["Indiana Pacers", "#002d62"],
  LAC: ["LA Clippers", "#c8102e"], LAL: ["Los Angeles Lakers", "#552583"], MEM: ["Memphis Grizzlies", "#5d76a9"],
  MIA: ["Miami Heat", "#98002e"], MIL: ["Milwaukee Bucks", "#00471b"], MIN: ["Minnesota Timberwolves", "#0c2340"],
  NOP: ["New Orleans Pelicans", "#0c2340"], NYK: ["New York Knicks", "#006bb6"], OKC: ["Oklahoma City Thunder", "#007ac1"],
  ORL: ["Orlando Magic", "#0077c0"], PHI: ["Philadelphia 76ers", "#006bb6"], PHX: ["Phoenix Suns", "#1d1160"],
  POR: ["Portland Trail Blazers", "#e03a3e"], SAC: ["Sacramento Kings", "#5a2d81"], SAS: ["San Antonio Spurs", "#c4ced4"],
  TOR: ["Toronto Raptors", "#ce1141"], UTA: ["Utah Jazz", "#002b5c"], WAS: ["Washington Wizards", "#002b5c"],
};

function teamName(abbr) { return TEAM_META[abbr]?.[0] || abbr; }
function teamColor(abbr) { return TEAM_META[abbr]?.[1] || "#201e1d"; }

function readableTextColor(hex) {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return (r * 0.299 + g * 0.587 + b * 0.114) > 150 ? "#201e1d" : "#f3f2f2";
}

function daysUntilTipOff() {
  const tip = new Date("2026-10-20T00:00:00");
  return Math.max(0, Math.ceil((tip - new Date()) / 86400000));
}

function speedControl() {
  const fieldset = document.createElement("fieldset");
  fieldset.className = "seg";
  fieldset.setAttribute("aria-label", "Replay speed");
  const groupName = `speed-${crypto.randomUUID()}`;
  [5, 10, 30, 100].forEach((speed) => {
    const id = `${groupName}-${speed}`;
    const input = document.createElement("input");
    input.type = "radio"; input.name = groupName; input.id = id; input.value = speed; input.checked = speed === 10;
    const label = document.createElement("label"); label.htmlFor = id; label.textContent = `${speed}×`;
    fieldset.append(input, label);
  });
  return fieldset;
}

function renderOffSeason(container) {
  const days = daysUntilTipOff();
  container.insertAdjacentHTML("beforeend", `
    <article class="offseason card elev-sm">
      <div class="count-ring" style="--progress:${Math.max(6, Math.min(100, 100 - days))}%"><strong>${days}</strong><span>days</span></div>
      <div><p class="card-kicker">Live schedule</p><h3>No live games right now</h3><p>The 2026–27 season tips off October 20, 2026. Until then, every replay runs through the same live pipeline.</p></div>
      <span class="tag tag-neutral">Off-season</span>
    </article>`);
}

function replayCard(fixture, onWatch) {
  const card = document.createElement("article");
  card.className = "replay-card card elev-sm";
  const controls = speedControl();
  const button = document.createElement("button");
  button.className = "btn btn-primary btn-block";
  button.innerHTML = `Watch replay <span aria-hidden="true">→</span>`;
  button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "Starting…";
    try {
      const checked = controls.querySelector("input:checked");
      const { stream_url, session_key } = await fetchJson(`/api/replays/${fixture.game_id}/start?speed=${checked.value}`, { method: "POST" });
      onWatch(stream_url, session_key, { awayTeam: fixture.away_team, homeTeam: fixture.home_team });
      // onWatch navigates to the game view, but this card is still sitting
      // in the DOM underneath -- reset it now so "Back to games" doesn't
      // land on a permanently-disabled "Starting..." button.
      button.disabled = false; button.innerHTML = `Watch replay <span aria-hidden="true">→</span>`;
    } catch (error) {
      button.disabled = false; button.innerHTML = `Try again <span aria-hidden="true">→</span>`;
      console.error(error);
    }
  });
  const awayColor = teamColor(fixture.away_team), homeColor = teamColor(fixture.home_team);
  const margin = Math.abs(fixture.final_home_score - fixture.final_away_score);
  card.innerHTML = `
    <div class="fixture-meta"><span class="tag tag-outline">${fixture.season}</span><span class="tag tag-outline">Final</span><span class="tag tag-accent">Decided by ${margin}</span></div>
    <div class="fixture-teams">
      <div class="fixture-team"><span class="team-badge" style="background:${awayColor};color:${readableTextColor(awayColor)};border-color:${awayColor}">${fixture.away_team}</span><span>${teamName(fixture.away_team)}</span></div>
      <span class="fixture-at">@</span>
      <div class="fixture-team"><span class="team-badge" style="background:${homeColor};color:${readableTextColor(homeColor)};border-color:${homeColor}">${fixture.home_team}</span><span>${teamName(fixture.home_team)}</span></div>
    </div>
    <p class="fixture-score"><b>${fixture.final_away_score}</b><span>–</span><b>${fixture.final_home_score}</b></p>
    <h3>${fixture.label}</h3>
    <p class="fixture-copy">Replay the full game through the live polling, probability and heat-check pipeline.</p>`;
  card.append(controls, button);
  return card;
}

export async function renderGamePicker(container, onWatch) {
  container.innerHTML = '<p class="empty-state">Loading available games…</p>';
  const [live, replays] = await Promise.all([
    fetchJson("/api/games/live").catch(() => ({ game_ids: [] })),
    fetchJson("/api/replays").catch(() => ({ fixtures: [] })),
  ]);
  container.replaceChildren();
  renderOffSeason(container);

  if (live.game_ids.length) {
    const liveGrid = document.createElement("div"); liveGrid.className = "live-games";
    live.game_ids.forEach((gameId) => {
      const button = document.createElement("button"); button.className = "btn btn-primary"; button.textContent = `Watch ${gameId}`;
      button.addEventListener("click", () => onWatch(`/api/games/${gameId}/stream`, gameId)); liveGrid.append(button);
    });
    container.append(liveGrid);
  }

  const cards = document.createElement("div"); cards.className = "replay-grid";
  replays.fixtures.forEach((fixture) => cards.append(replayCard(fixture, onWatch)));
  container.append(cards);
}
