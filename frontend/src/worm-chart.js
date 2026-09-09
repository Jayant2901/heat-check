// The win-probability "worm" chart: x = elapsed game time, y = home win
// probability (0-100%, tie line at 50%). Renders with D3 and grows
// incrementally as wp_update events arrive -- no full redraw per point.

const MARGIN = { top: 16, right: 16, bottom: 28, left: 40 };
const HEIGHT = 320;
const REGULATION_SECONDS = 4 * 12 * 60;
const OT_SECONDS = 5 * 60;

function quarterBoundaries(maxT) {
  const bounds = [0, 12 * 60, 24 * 60, 36 * 60, REGULATION_SECONDS];
  let t = REGULATION_SECONDS;
  while (t < maxT) {
    t += OT_SECONDS;
    bounds.push(t);
  }
  return bounds;
}

export function createWormChart(container) {
  const width = container.clientWidth || 600;
  const innerWidth = width - MARGIN.left - MARGIN.right;
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom;

  const svg = d3
    .select(container)
    .append("svg")
    .attr("viewBox", `0 0 ${width} ${HEIGHT}`);

  const g = svg.append("g").attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);

  let xMax = REGULATION_SECONDS;
  const x = d3.scaleLinear().domain([0, xMax]).range([0, innerWidth]);
  const y = d3.scaleLinear().domain([0, 100]).range([innerHeight, 0]);

  // Split fill, home color above the 50% line and away color below -- the
  // broadcast-standard "worm chart" look. The gradient is purely
  // y-position-based (a hard stop exactly at the tie line), so it never
  // needs to change when the x domain extends into OT.
  const gradientId = `wp-split-${Math.random().toString(36).slice(2)}`;
  svg
    .append("defs")
    .append("linearGradient")
    .attr("id", gradientId)
    .attr("gradientUnits", "userSpaceOnUse")
    .attr("x1", 0)
    .attr("y1", 0)
    .attr("x2", 0)
    .attr("y2", innerHeight)
    .selectAll("stop")
    .data([
      { offset: "0%", color: "var(--home)" },
      { offset: "50%", color: "var(--home)" },
      { offset: "50%", color: "var(--away)" },
      { offset: "100%", color: "var(--away)" },
    ])
    .enter()
    .append("stop")
    .attr("offset", (d) => d.offset)
    .attr("stop-color", (d) => d.color);

  const quarterLinesGroup = g.append("g").attr("class", "quarter-lines");
  g.append("line")
    .attr("class", "tie-line")
    .attr("x1", 0)
    .attr("x2", innerWidth)
    .attr("y1", y(50))
    .attr("y2", y(50));

  const xAxisGroup = g.append("g").attr("class", "axis x-axis").attr("transform", `translate(0,${innerHeight})`);
  const yAxisGroup = g.append("g").attr("class", "axis y-axis");

  const line = d3
    .line()
    .curve(d3.curveMonotoneX)
    .x((d) => x(d.t))
    .y((d) => y(d.wp_home * 100));

  const area = d3
    .area()
    .curve(d3.curveMonotoneX)
    .x((d) => x(d.t))
    .y0(y(50))
    .y1((d) => y(d.wp_home * 100));

  const areaPath = g.append("path").attr("class", "wp-area").attr("fill", `url(#${gradientId})`);
  const path = g.append("path").attr("class", "wp-line-home");
  const latestPoint = g.append("circle").attr("class", "latest-point").attr("r", 4).style("display", "none");
  const anomalyGroup = g.append("g").attr("class", "anomalies");

  const tooltip = d3
    .select(container)
    .append("div")
    .attr("class", "anomaly-tooltip")
    .style("position", "absolute");

  const points = [];

  function redrawAxesAndGridlines() {
    x.domain([0, xMax]);
    xAxisGroup.call(
      d3
        .axisBottom(x)
        .tickValues(quarterBoundaries(xMax))
        .tickFormat((t) => (t === 0 ? "Tip-off" : `${Math.round(t / 60)}'`))
    );
    yAxisGroup.call(d3.axisLeft(y).ticks(5).tickFormat((v) => `${v}%`));

    const bounds = quarterBoundaries(xMax).filter((t) => t > 0 && t < xMax);
    const lines = quarterLinesGroup.selectAll("line").data(bounds);
    lines
      .enter()
      .append("line")
      .attr("class", "quarter-line")
      .merge(lines)
      .attr("x1", (d) => x(d))
      .attr("x2", (d) => x(d))
      .attr("y1", 0)
      .attr("y2", innerHeight);
    lines.exit().remove();
  }

  redrawAxesAndGridlines();

  function addPoint(point) {
    // A dropped SSE connection auto-reconnects (EventSource's default
    // behavior) to the same stream URL, which re-runs the backend's backlog
    // replay from t=0 -- without this guard, every reconnect redrew the
    // whole line from scratch, appended after what was already there, and
    // made the worm chart jump backward. Only reject strictly-earlier
    // points: two distinct real events can legitimately share the same t
    // (e.g. back-to-back free throws with the clock frozen), so equal-t
    // points still get plotted.
    const lastPoint = points[points.length - 1];
    if (lastPoint && point.t < lastPoint.t) {
      return;
    }
    points.push(point);
    if (point.t > xMax) {
      xMax = point.t + OT_SECONDS / 2;
      redrawAxesAndGridlines();
    }
    areaPath.datum(points).attr("d", area);
    path.datum(points).attr("d", line);
    latestPoint
      .style("display", null)
      .attr("cx", x(point.t))
      .attr("cy", y(point.wp_home * 100));

    // re-position any anomaly markers whose x depends on the (possibly
    // just-extended) domain
    anomalyGroup.selectAll(".anomaly-line").attr("x1", (d) => x(d.t)).attr("x2", (d) => x(d.t));
    anomalyGroup.selectAll(".anomaly-marker").attr("cx", (d) => x(d.t));
  }

  function addAnomaly(anomaly) {
    const cy = innerHeight / 2;
    anomalyGroup
      .append("line")
      .datum(anomaly)
      .attr("class", "anomaly-line")
      .attr("x1", x(anomaly.t))
      .attr("x2", x(anomaly.t))
      .attr("y1", 0)
      .attr("y2", innerHeight);

    const size = Math.min(10, 4 + Math.abs(anomaly.z_score));
    anomalyGroup
      .append("circle")
      .datum(anomaly)
      .attr("class", "anomaly-marker")
      .attr("cx", x(anomaly.t))
      .attr("cy", cy)
      .attr("r", size)
      .on("mouseenter", (event) => {
        tooltip
          .style("opacity", 1)
          .style("left", `${event.offsetX + 12}px`)
          .style("top", `${event.offsetY - 12}px`)
          .text(anomaly.message);
      })
      .on("mousemove", (event) => {
        tooltip.style("left", `${event.offsetX + 12}px`).style("top", `${event.offsetY - 12}px`);
      })
      .on("mouseleave", () => tooltip.style("opacity", 0));
  }

  function reset() {
    points.length = 0;
    xMax = REGULATION_SECONDS;
    areaPath.datum([]).attr("d", area);
    path.datum([]).attr("d", line);
    latestPoint.style("display", "none");
    anomalyGroup.selectAll("*").remove();
    redrawAxesAndGridlines();
  }

  return { addPoint, addAnomaly, reset };
}
