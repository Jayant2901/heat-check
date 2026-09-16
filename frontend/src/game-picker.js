async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  return response.json();
}

function daysUntilTipOff() {
  const tip = new Date("2026-10-20T00:00:00");
  return Math.max(0, Math.ceil((tip - new Date()) / 86400000));
}

function speedControl() {
  const fieldset = document.createElement("fieldset");
  fieldset.className = "seg";
  fieldset.setAttribute("aria-label", "Replay speed");
  [5, 10, 30, 100].forEach((speed) => {
    const id = `speed-${crypto.randomUUID()}`;
    const input = document.createElement("input");
    input.type = "radio"; input.name = id.slice(0, -2); input.id = id; input.value = speed; input.checked = speed === 10;
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
  card.innerHTML = `
    <div class="fixture-teams"><span class="team-badge accent">${fixture.home_team}</span><span>${fixture.home_team}</span><span class="tag tag-outline">Final</span><span>${fixture.away_team}</span><span class="team-badge neutral">${fixture.away_team}</span></div>
    <p class="fixture-score"><b>${fixture.final_home_score}</b><span>–</span><b>${fixture.final_away_score}</b></p>
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
