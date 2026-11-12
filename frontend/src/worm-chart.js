// Win-probability chart: the home team's chance of winning over game time.
// Above the 50% line is the home side (accent), below it the away side
// (ink). Colours come from CSS classes, so both themes work. Quarter rules,
// and a labelled marker ("8-0") on the 50% line for each flagged run. Markers
// can be hovered, focused, clicked or tapped; selecting one is reported to the
// page (onSelect) so the heat-check list can follow, and the page can select
// one too (select(key)). Drawing is coalesced to one repaint per frame, so a
// backlog of hundreds of points costs one draw, not hundreds.
// Uses the global `d3` from /vendor/d3.min.js, which only game pages need,
// so it is loaded on demand with loadD3() instead of on every page.

const MAX_WIDTH = 1280;
const MIN_WIDTH = 300;
const MARGIN = { top: 16, right: 12, bottom: 28, left: 40 };
const REGULATION = 2880;
const QUARTER = 720;
const OVERTIME = 300;

function boundaries(maxT) {
  const values = [0, QUARTER, 2 * QUARTER, 3 * QUARTER, REGULATION];
  while (values[values.length - 1] < maxT) {
    values.push(values[values.length - 1] + OVERTIME);
  }
  return values;
}

// Label for the i-th period segment: Q1-Q4, then OT, 2OT, ...
function segmentLabel(i) {
  if (i < 4) return `Q${i + 1}`;
  if (i === 4) return "OT";
  return `${i - 3}OT`;
}

let chartCount = 0;
let d3Loading = null;

// Stable identity of a flagged run, shared by the chart and the list.
export function anomalyKey(a) {
  return `${Math.round(a.t)}:${a.team}:${a.magnitude}`;
}

const HIT_RADIUS = 17; // a 34px target: big enough for a finger
const LABEL_GAP = 36; // markers closer than this (px) stagger their labels

export function loadD3() {
  if (window.d3) return Promise.resolve(window.d3);
  if (!d3Loading) {
    d3Loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/vendor/d3.min.js";
      script.async = true;
      script.onload = () => resolve(window.d3);
      script.onerror = () => {
        d3Loading = null;
        reject(new Error("Couldn't load the chart library."));
      };
      document.head.appendChild(script);
    });
  }
  return d3Loading;
}

export function createWormChart(container, { homeLabel, awayLabel, onHover = () => {}, onSelect = () => {} }) {
  const d3 = window.d3;
  chartCount += 1;
  const id = `wp-${chartCount}`;
  // Draw at the container's real width so text stays at its true size on
  // phones instead of being scaled down with the whole picture.
  const WIDTH = Math.round(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, container.clientWidth || MAX_WIDTH)));
  const HEIGHT = WIDTH < 500 ? 220 : 280;
  const innerBottom = HEIGHT - MARGIN.bottom;

  const svg = d3
    .select(container)
    .append("svg")
    .attr("class", "wp-chart")
    .attr("viewBox", `0 0 ${WIDTH} ${HEIGHT}`)
    .attr("role", "img")
    .attr("aria-label", `${homeLabel} win chance over the game`);

  const x = d3.scaleLinear().range([MARGIN.left, WIDTH - MARGIN.right]);
  const y = d3.scaleLinear().domain([0, 1]).range([innerBottom, MARGIN.top]);
  let maxT = REGULATION;

  // Two clip paths split the area at the 50% line.
  const defs = svg.append("defs");
  defs
    .append("clipPath")
    .attr("id", `${id}-above`)
    .append("rect")
    .attr("x", 0)
    .attr("y", 0)
    .attr("width", WIDTH)
    .attr("height", y(0.5));
  defs
    .append("clipPath")
    .attr("id", `${id}-below`)
    .append("rect")
    .attr("x", 0)
    .attr("y", y(0.5))
    .attr("width", WIDTH)
    .attr("height", HEIGHT - y(0.5));

  const grid = svg.append("g").attr("class", "wp-grid");
  const sideLabels = svg.append("g").attr("class", "wp-side-labels");
  const plot = svg.append("g");
  const markers = svg.append("g").attr("class", "wp-markers");

  const area = d3
    .area()
    .curve(d3.curveMonotoneX)
    .x((d) => x(d.t))
    .y0(y(0.5))
    .y1((d) => y(d.wp_home));
  const line = d3
    .line()
    .curve(d3.curveMonotoneX)
    .x((d) => x(d.t))
    .y((d) => y(d.wp_home));

  const homeArea = plot
    .append("path")
    .attr("class", "wp-area wp-area-home")
    .attr("clip-path", `url(#${id}-above)`);
  const awayArea = plot
    .append("path")
    .attr("class", "wp-area wp-area-away")
    .attr("clip-path", `url(#${id}-below)`);
  const homeLine = plot
    .append("path")
    .attr("class", "wp-line wp-line-home")
    .attr("clip-path", `url(#${id}-above)`);
  const awayLine = plot
    .append("path")
    .attr("class", "wp-line wp-line-away")
    .attr("clip-path", `url(#${id}-below)`);
  const latest = plot.append("circle").attr("class", "wp-latest").attr("r", 4).style("display", "none");

  const points = [];
  const anomalies = [];
  let selectedKey = null;
  let frame = null;

  function drawGrid() {
    x.domain([0, maxT]);
    const bounds = boundaries(maxT);

    grid
      .selectAll(".wp-period")
      .data(bounds.slice(1, -1))
      .join("line")
      .attr("class", "wp-period")
      .attr("x1", (d) => x(d))
      .attr("x2", (d) => x(d))
      .attr("y1", MARGIN.top)
      .attr("y2", innerBottom);

    grid
      .selectAll(".wp-period-label")
      .data(bounds.slice(0, -1))
      .join("text")
      .attr("class", "wp-period-label")
      .attr("x", (d, i) => (x(d) + x(bounds[i + 1])) / 2)
      .attr("y", HEIGHT - 8)
      .attr("text-anchor", "middle")
      .text((d, i) => segmentLabel(i));

    grid
      .selectAll(".wp-tick")
      .data([0, 0.25, 0.5, 0.75, 1])
      .join("text")
      .attr("class", "wp-tick")
      .attr("x", MARGIN.left - 8)
      .attr("y", (d) => y(d) + 4)
      .attr("text-anchor", "end")
      .text((d) => `${Math.round(d * 100)}%`);

    grid
      .selectAll(".wp-fifty")
      .data([0.5])
      .join("line")
      .attr("class", "wp-fifty")
      .attr("x1", MARGIN.left)
      .attr("x2", WIDTH - MARGIN.right)
      .attr("y1", (d) => y(d))
      .attr("y2", (d) => y(d));

    sideLabels
      .selectAll("text")
      .data([
        { text: `${homeLabel} ahead`, y: MARGIN.top + 12, side: "home" },
        { text: `${awayLabel} ahead`, y: innerBottom - 6, side: "away" },
      ])
      .join("text")
      .attr("x", WIDTH - MARGIN.right - 6)
      .attr("text-anchor", "end")
      .attr("y", (d) => d.y)
      .attr("class", (d) => `wp-side-${d.side}`)
      .text((d) => d.text);
  }

  function paint() {
    homeArea.attr("d", area(points));
    awayArea.attr("d", area(points));
    homeLine.attr("d", line(points));
    awayLine.attr("d", line(points));
    const last = points[points.length - 1];
    if (last) {
      latest
        .style("display", null)
        .attr("cx", x(last.t))
        .attr("cy", y(last.wp_home))
        .attr("class", `wp-latest ${last.wp_home >= 0.5 ? "wp-latest-home" : "wp-latest-away"}`);
    }
    // Labels stagger upward when markers sit close together.
    const sorted = [...anomalies].sort((a, b) => a.t - b.t);
    const level = new Map();
    let lastX = -Infinity;
    let lastLevel = 0;
    for (const a of sorted) {
      const px = x(a.t);
      lastLevel = px - lastX < LABEL_GAP ? (lastLevel + 1) % 3 : 0;
      lastX = px;
      level.set(anomalyKey(a), lastLevel);
    }
    const radius = (d) => Math.min(11, 6 + d.z_score * 1.4);

    markers
      .selectAll("g.wp-marker")
      .data(sorted, anomalyKey)
      .join((enter) => {
        const g = enter.append("g").attr("tabindex", 0).attr("role", "button");
        g.append("line").attr("class", "wp-marker-rule");
        g.append("circle").attr("class", "wp-marker-hit").attr("r", HIT_RADIUS);
        g.append("circle").attr("class", "wp-marker-dot");
        g.append("text").attr("class", "wp-marker-label").attr("text-anchor", "middle");
        g.append("title");
        g.on("mouseenter focus", (event, d) => onHover(d.message));
        g.on("mouseleave blur", () => onHover(null));
        g.on("click", (event, d) => toggle(anomalyKey(d)));
        g.on("keydown", (event, d) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            toggle(anomalyKey(d));
          }
        });
        return g;
      })
      .attr("class", (d) => `wp-marker wp-marker-${d.team}${anomalyKey(d) === selectedKey ? " is-selected" : ""}`)
      .attr("aria-pressed", (d) => String(anomalyKey(d) === selectedKey))
      .attr("aria-label", (d) => `${d.magnitude}-0 run. ${d.message}`)
      .call((g) => g.select("title").text((d) => d.message))
      .call((g) =>
        g
          .select("line")
          .attr("x1", (d) => x(d.t))
          .attr("x2", (d) => x(d.t))
          .attr("y1", MARGIN.top)
          .attr("y2", innerBottom),
      )
      .call((g) =>
        g
          .select(".wp-marker-hit")
          .attr("cx", (d) => x(d.t))
          .attr("cy", y(0.5)),
      )
      .call((g) =>
        g
          .select(".wp-marker-dot")
          .attr("cx", (d) => x(d.t))
          .attr("cy", y(0.5))
          .attr("r", radius),
      )
      .call((g) =>
        g
          .select("text")
          .attr("x", (d) => Math.max(MARGIN.left + 14, Math.min(WIDTH - MARGIN.right - 14, x(d.t))))
          .attr("y", (d) => y(0.5) - radius(d) - 6 - level.get(anomalyKey(d)) * 13)
          .text((d) => `${d.magnitude}-0`),
      );
  }

  // Coalesce redraws: however many points arrive in a frame, paint once.
  function draw() {
    if (frame !== null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      paint();
    });
  }

  function toggle(key) {
    select(selectedKey === key ? null : key);
    onSelect(selectedKey === null ? null : anomalies.find((a) => anomalyKey(a) === selectedKey) || null);
  }

  // Called by the page (the heat-check list) as well as by taps on a marker.
  function select(key) {
    selectedKey = key;
    paint();
  }

  function addPoint(point) {
    const last = points[points.length - 1];
    if (last && point.t < last.t) return; // a reconnect replaying older history
    if (last && point.t === last.t) {
      points[points.length - 1] = point; // same moment re-sent: replace, don't duplicate
    } else {
      points.push(point);
    }
    if (point.t > maxT) {
      maxT = boundaries(point.t).at(-1);
      drawGrid();
    }
    draw();
  }

  function addAnomaly(anomaly) {
    if (anomalies.some((a) => a.t === anomaly.t && a.message === anomaly.message)) return;
    anomalies.push(anomaly);
    draw();
  }

  function reset() {
    points.length = 0;
    anomalies.length = 0;
    maxT = REGULATION;
    selectedKey = null;
    latest.style("display", "none");
    drawGrid();
    paint();
  }

  function destroy() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    svg.remove();
  }

  drawGrid();
  return { addPoint, addAnomaly, select, reset, destroy };
}
