const WIDTH = 760, HEIGHT = 260;
const PLOT = { left: 36, right: 748, top: 16, bottom: 236 };
const REGULATION = 2880, OT = 300;

function boundaries(max) {
  const values = [0, 720, 1440, 2160, 2880];
  while (values[values.length - 1] < max) values.push(values[values.length - 1] + OT);
  return values;
}

export function createWormChart(container, onHover = () => {}) {
  const svg = d3.select(container).append("svg").attr("viewBox", `0 0 ${WIDTH} ${HEIGHT}`).attr("role", "img").attr("aria-label", "Home-team win probability over time");
  const defs = svg.append("defs"); const gradientId = `probability-${crypto.randomUUID()}`;
  const gradient = defs.append("linearGradient").attr("id", gradientId).attr("gradientUnits", "userSpaceOnUse").attr("x1", 0).attr("y1", PLOT.top).attr("x2", 0).attr("y2", PLOT.bottom);
  gradient.selectAll("stop").data([{o:"0%",c:"#ec3013"},{o:"50%",c:"#ec3013"},{o:"50%",c:"#201e1d"},{o:"100%",c:"#201e1d"}]).enter().append("stop").attr("offset",d=>d.o).attr("stop-color",d=>d.c);
  const g = svg.append("g"); let maximum = REGULATION; const x = d3.scaleLinear().range([PLOT.left, PLOT.right]); const y = d3.scaleLinear().domain([0,100]).range([PLOT.bottom,PLOT.top]);
  const grid = g.append("g"), trace = g.append("g"), markers = g.append("g");
  const area = d3.area().curve(d3.curveMonotoneX).x(d=>x(d.t)).y0(y(50)).y1(d=>y(d.wp_home * 100));
  const line = d3.line().curve(d3.curveMonotoneX).x(d=>x(d.t)).y(d=>y(d.wp_home * 100));
  const areaPath = trace.append("path").attr("class","probability-area").attr("fill",`url(#${gradientId})`);
  const linePath = trace.append("path").attr("class","probability-line").attr("stroke",`url(#${gradientId})`);
  const latest = trace.append("circle").attr("class","chart-latest").attr("r",4).style("display","none");
  const points = [];
  function redrawGrid() {
    x.domain([0, maximum]); const bs = boundaries(maximum);
    grid.selectAll(".quarter-rule").data(bs.slice(1,-1)).join("line").attr("class","quarter-rule").attr("x1",d=>x(d)).attr("x2",d=>x(d)).attr("y1",PLOT.top).attr("y2",PLOT.bottom);
    grid.selectAll(".quarter-label").data(bs).join("text").attr("class","quarter-label").attr("x",d=>x(d)).attr("y",HEIGHT-7).attr("text-anchor",d=>d===0?"start":d===bs[bs.length-1]?"end":"middle").text((d,i)=>i===0?"TIP":d<=REGULATION?`Q${d/720}`:`OT${Math.round((d-REGULATION)/OT)}`);
    grid.selectAll(".probability-tick").data([0,25,50,75,100]).join("text").attr("class","probability-tick").attr("x",PLOT.left-8).attr("y",d=>y(d)+4).attr("text-anchor","end").text(d=>`${d}%`);
    grid.selectAll(".fifty-line").data([50]).join("line").attr("class","fifty-line").attr("x1",PLOT.left).attr("x2",PLOT.right).attr("y1",d=>y(d)).attr("y2",d=>y(d));
  }
  function redraw() { areaPath.datum(points).attr("d",area); linePath.datum(points).attr("d",line); const last=points.at(-1); if(last) latest.style("display",null).attr("cx",x(last.t)).attr("cy",y(last.wp_home*100)); markers.selectAll(".anomaly-rule").attr("x1",d=>x(d.t)).attr("x2",d=>x(d.t)); markers.selectAll(".anomaly-dot").attr("cx",d=>x(d.t)); }
  redrawGrid();
  function addPoint(point) {
    const previous=points.at(-1); if(previous && point.t<previous.t)return;
    points.push(point); if(point.t>maximum){maximum=point.t+OT/2;redrawGrid();} redraw();
  }
  function addAnomaly(anomaly) {
    markers.append("line").datum(anomaly).attr("class","anomaly-rule").attr("x1",x(anomaly.t)).attr("x2",x(anomaly.t)).attr("y1",PLOT.top).attr("y2",PLOT.bottom);
    markers.append("circle").datum(anomaly).attr("class","anomaly-dot").attr("cx",x(anomaly.t)).attr("cy",y(50)).attr("r",Math.min(9,4+anomaly.z_score)).on("mouseenter",()=>onHover(anomaly.message)).on("mouseleave",()=>onHover("Home / away probability"));
  }
  function reset(){points.length=0;maximum=REGULATION;areaPath.attr("d",null);linePath.attr("d",null);latest.style("display","none");markers.selectAll("*").remove();redrawGrid();onHover("Home / away probability");}
  function destroy(){svg.remove();}
  return {addPoint,addAnomaly,reset,destroy};
}
