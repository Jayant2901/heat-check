// Lists live games and replay fixtures in one picker. Selecting a live game
// or a replay session both resolve to a stream_url and hand it to onWatch --
// the caller (main.js) doesn't need to know which kind it picked.

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

export async function renderGamePicker(container, onWatch) {
  container.innerHTML = "";

  const [liveData, replayData] = await Promise.all([
    fetchJson("/api/games/live").catch(() => ({ game_ids: [] })),
    fetchJson("/api/replays").catch(() => ({ fixtures: [] })),
  ]);

  const liveSection = document.createElement("div");
  liveSection.innerHTML = "<h2>Live games</h2>";
  if (liveData.game_ids.length === 0) {
    liveSection.innerHTML +=
      '<p class="empty-state">No live games right now -- the 2026-27 season starts October 20, 2026. Try a replay below.</p>';
  } else {
    const ul = document.createElement("ul");
    for (const gameId of liveData.game_ids) {
      const li = document.createElement("li");
      li.innerHTML = `<span>${gameId}</span>`;
      const button = document.createElement("button");
      button.textContent = "Watch live";
      button.onclick = () => onWatch(`/api/games/${gameId}/stream`, gameId);
      li.appendChild(button);
      ul.appendChild(li);
    }
    liveSection.appendChild(ul);
  }
  container.appendChild(liveSection);

  const replaySection = document.createElement("div");
  replaySection.innerHTML = "<h2>Replay a classic game</h2>";
  if (replayData.fixtures.length === 0) {
    replaySection.innerHTML += '<p class="empty-state">No replay fixtures available yet.</p>';
  } else {
    const ul = document.createElement("ul");
    for (const fixture of replayData.fixtures) {
      const li = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = `${fixture.label} (${fixture.away_team} ${fixture.final_away_score} - ${fixture.final_home_score} ${fixture.home_team})`;
      li.appendChild(label);

      const speedSelect = document.createElement("select");
      for (const speed of [5, 10, 30, 100]) {
        const opt = document.createElement("option");
        opt.value = speed;
        opt.textContent = `${speed}x`;
        if (speed === 10) opt.selected = true;
        speedSelect.appendChild(opt);
      }
      li.appendChild(speedSelect);

      const button = document.createElement("button");
      button.textContent = "Watch replay";
      button.onclick = async () => {
        const { stream_url, session_key } = await fetchJson(
          `/api/replays/${fixture.game_id}/start?speed=${speedSelect.value}`,
          { method: "POST" }
        );
        onWatch(stream_url, session_key);
      };
      li.appendChild(button);
      ul.appendChild(li);
    }
    replaySection.appendChild(ul);
  }
  container.appendChild(replaySection);
}
