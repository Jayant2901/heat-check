// Small formatting helpers shared by the dashboard and the game page.

const REGULATION_PERIODS = 4;
const PERIOD_SECONDS = 720;
const OT_SECONDS = 300;

export function periodLabel(period) {
  if (!period || period < 1) return "";
  if (period <= REGULATION_PERIODS) return `Q${period}`;
  if (period === 5) return "OT";
  return `${period - 4}OT`;
}

export function clockText(period, clockSeconds) {
  const label = periodLabel(period);
  if (!label || clockSeconds == null) return label;
  const total = Math.max(0, Math.floor(clockSeconds));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${label} ${minutes}:${seconds}`;
}

// Elapsed game seconds -> {period, clock}. The inverse of the backend's
// app/model/features.py elapsed_seconds(); used for backlog points, which
// carry only `t`.
export function fromElapsed(t) {
  // A boundary (t = 720, 2880, ...) belongs to the period that just ended,
  // so the final buzzer reads "Q4 0:00" rather than "OT 5:00".
  const regulation = REGULATION_PERIODS * PERIOD_SECONDS;
  if (t <= regulation) {
    const period = Math.max(1, Math.ceil(t / PERIOD_SECONDS));
    return { period, clock: period * PERIOD_SECONDS - t };
  }
  const overtime = Math.ceil((t - regulation) / OT_SECONDS);
  const period = REGULATION_PERIODS + overtime;
  return { period, clock: regulation + overtime * OT_SECONDS - t };
}

export function percent(probability) {
  return `${Math.round(probability * 100)}%`;
}

export function duration(seconds) {
  const s = Math.round(seconds);
  if (s < 120) return `${s} seconds`;
  const minutes = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${minutes} min ${rest} s` : `${minutes} minutes`;
}

export function shortDate(isoDate) {
  // isoDate is a plain YYYY-MM-DD schedule date; avoid time-zone shifts.
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export function localTime(isoDateTime) {
  const date = new Date(isoDateTime);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
