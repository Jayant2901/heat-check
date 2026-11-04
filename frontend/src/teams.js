// Team names and colours, plus the two HTML helpers every view uses.
// Team colours appear only on badges and in the win-probability chart.

const TEAMS = {
  ATL: { name: "Hawks", city: "Atlanta", primary: "#e03a3e", secondary: "#c1d32f" },
  BOS: { name: "Celtics", city: "Boston", primary: "#007a33", secondary: "#ba9653" },
  BKN: { name: "Nets", city: "Brooklyn", primary: "#000000", secondary: "#a1a1a4" },
  CHA: { name: "Hornets", city: "Charlotte", primary: "#1d1160", secondary: "#00788c" },
  CHI: { name: "Bulls", city: "Chicago", primary: "#ce1141", secondary: "#000000" },
  CLE: { name: "Cavaliers", city: "Cleveland", primary: "#860038", secondary: "#fdbb30" },
  DAL: { name: "Mavericks", city: "Dallas", primary: "#00538c", secondary: "#b8c4ca" },
  DEN: { name: "Nuggets", city: "Denver", primary: "#0e2240", secondary: "#fec524" },
  DET: { name: "Pistons", city: "Detroit", primary: "#c8102e", secondary: "#1d42ba" },
  GSW: { name: "Warriors", city: "Golden State", primary: "#1d428a", secondary: "#ffc72c" },
  HOU: { name: "Rockets", city: "Houston", primary: "#ce1141", secondary: "#c4ced4" },
  IND: { name: "Pacers", city: "Indiana", primary: "#002d62", secondary: "#fdbb30" },
  LAC: { name: "Clippers", city: "LA", primary: "#c8102e", secondary: "#1d428a" },
  LAL: { name: "Lakers", city: "Los Angeles", primary: "#552583", secondary: "#fdb927" },
  MEM: { name: "Grizzlies", city: "Memphis", primary: "#5d76a9", secondary: "#12173f" },
  MIA: { name: "Heat", city: "Miami", primary: "#98002e", secondary: "#f9a01b" },
  MIL: { name: "Bucks", city: "Milwaukee", primary: "#00471b", secondary: "#eee1c6" },
  MIN: { name: "Timberwolves", city: "Minnesota", primary: "#0c2340", secondary: "#78be20" },
  NOP: { name: "Pelicans", city: "New Orleans", primary: "#0c2340", secondary: "#c8102e" },
  NYK: { name: "Knicks", city: "New York", primary: "#006bb6", secondary: "#f58426" },
  OKC: { name: "Thunder", city: "Oklahoma City", primary: "#007ac1", secondary: "#ef3b24" },
  ORL: { name: "Magic", city: "Orlando", primary: "#0077c0", secondary: "#c4ced4" },
  PHI: { name: "76ers", city: "Philadelphia", primary: "#006bb6", secondary: "#ed174c" },
  PHX: { name: "Suns", city: "Phoenix", primary: "#1d1160", secondary: "#e56020" },
  POR: { name: "Trail Blazers", city: "Portland", primary: "#e03a3e", secondary: "#000000" },
  SAC: { name: "Kings", city: "Sacramento", primary: "#5a2d81", secondary: "#63727a" },
  SAS: { name: "Spurs", city: "San Antonio", primary: "#000000", secondary: "#c4ced4" },
  TOR: { name: "Raptors", city: "Toronto", primary: "#ce1141", secondary: "#000000" },
  UTA: { name: "Jazz", city: "Utah", primary: "#002b5c", secondary: "#00471b" },
  WAS: { name: "Wizards", city: "Washington", primary: "#002b5c", secondary: "#e31837" },
};

const FALLBACK = { name: "", city: "", primary: "#6b7280", secondary: "#9ca3af" };

export function team(code) {
  return TEAMS[code] || { ...FALLBACK, name: code || "" };
}

export function teamName(code) {
  return team(code).name || code || "";
}

export function teamColor(code) {
  return team(code).primary;
}

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function rgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function luminance(hex) {
  const channel = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = rgb(hex).map(channel);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function distance(a, b) {
  const [r1, g1, b1] = rgb(a);
  const [r2, g2, b2] = rgb(b);
  return Math.hypot(r1 - r2, g1 - g2, b1 - b2);
}

// Text colour for a badge filled with the team colour.
function inkOn(hex) {
  return contrast(hex, "#ffffff") >= contrast(hex, "#111111") ? "#ffffff" : "#111111";
}

export function badge(code, { size = "" } = {}) {
  const colour = teamColor(code);
  const label = esc(code || "?");
  const title = esc([team(code).city, teamName(code)].filter(Boolean).join(" "));
  const sizeClass = size ? ` badge-${size}` : "";
  return `<span class="badge${sizeClass}" style="--team:${colour};--team-ink:${inkOn(colour)}" title="${title}">${label}</span>`;
}

function isDark() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

// Chart colours for a matchup: each must be visible on the page background,
// and the two must be told apart (BOS and MIL are both green, for example).
export function matchupColors(home, away) {
  const background = isDark() ? "#16181c" : "#ffffff";
  const pick = (code, avoid) => {
    const options = [team(code).primary, team(code).secondary];
    const visible = options.filter((c) => contrast(c, background) >= 2.2);
    const pool = visible.length ? visible : [isDark() ? "#d1d5db" : "#4b5563"];
    if (!avoid) return pool[0];
    return pool.find((c) => distance(c, avoid) > 110) || (isDark() ? "#d1d5db" : "#4b5563");
  };
  const homeColor = pick(home);
  const awayColor = pick(away, homeColor);
  return { home: homeColor, away: awayColor };
}
