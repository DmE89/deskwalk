// Live chart of the current session (SVG, no library).
import { getJSON } from "./api.js";
import { $ } from "./dom.js";
import { U, hm, imp, ms, nf } from "./format.js";
import { app } from "./state.js";

let chartSession = null;
let chartPts = [];
let loading = false;

export async function loadChart() {
  if (loading) return;
  loading = true;
  const d = await getJSON("/history?session=current");
  loading = false;
  chartSession = d ? d.id : null;
  chartPts = d ? d.samples.map((p) => [p.ts, p.speed, p.cadence]) : [];
  drawChart();
}

export function liveChart() {
  const sess = app.S.session;
  if (!sess) {
    $("#chartEmpty").classList.toggle("hidden", chartPts.length > 0);
    return;
  }
  if (sess.id !== chartSession) {
    loadChart();
    return;
  }
  const now = Date.now() / 1000;
  if (!chartPts.length || now - chartPts[chartPts.length - 1][0] >= 2) {
    chartPts.push([now, app.S.belt ? app.S.belt.speed : 0, app.S.cadence]);
    if (app.view === "training") drawChart();
  }
}

export function drawChart() {
  const svg = $("#chart");
  $("#chartEmpty").classList.toggle("hidden", chartPts.length > 0);
  const W = Math.max(240, Math.round(svg.clientWidth || 360));
  const H = Math.max(160, Math.round(svg.clientHeight || 240));
  const L = 30;
  const R = 36;
  const T = 10;
  const B = 22;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (chartPts.length < 2) {
    svg.innerHTML = "";
    return;
  }
  const step = Math.max(1, Math.floor(chartPts.length / 600));
  const pts = chartPts.filter((_, i) => i % step === 0 || i === chartPts.length - 1);
  const t0 = pts[0][0];
  const t1 = pts[pts.length - 1][0];
  const span = Math.max(60, t1 - t0);
  // round ticks: metric 0 to 6 km/h in steps of 2, imperial 0 to 4 mph in steps of 1, cadence on the same lines
  const n = imp() ? 4 : 3;
  const vMax = imp() ? 4 : 6;
  const cStep = Math.ceil(Math.max(120, ...pts.map((p) => p[2])) / n / 10) * 10;
  const cMax = cStep * n;
  const x = (ts) => L + ((ts - t0) / span) * (W - L - R);
  const ys = (v) => T + (H - T - B) * (1 - Math.min(vMax, U.speedOut(v)) / vMax);
  const yc = (c) => T + (H - T - B) * (1 - Math.min(cMax, c) / cMax);
  const elapsed = (sec) => (span >= 3600 ? hm(sec) + " h" : ms(sec));
  const sessionStart = app.S && app.S.session ? app.S.session.start_ts : t0;
  let g = "";
  for (let i = 0; i <= n; i++) {
    const y = T + (H - T - B) * (1 - i / n);
    g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>`;
    g += `<text x="${L - 6}" y="${y + 3}" text-anchor="end">${nf((vMax / n) * i, 0)}</text>`;
    g += `<text x="${W - R + 6}" y="${y + 3}">${nf(cStep * i, 0)}</text>`;
  }
  for (let i = 0; i <= 3; i++) {
    const ts = t0 + (span / 3) * i;
    const anchor = i === 0 ? "start" : i === 3 ? "end" : "middle";
    g += `<text x="${x(ts)}" y="${H - 6}" text-anchor="${anchor}">${elapsed(ts - sessionStart)}</text>`;
  }
  const speedLine = pts.map((p) => `${x(p[0]).toFixed(1)},${ys(p[1]).toFixed(1)}`).join(" ");
  const area = `${x(pts[0][0]).toFixed(1)},${H - B} ${speedLine} ${x(pts[pts.length - 1][0]).toFixed(1)},${H - B}`;
  const cadLine = pts.map((p) => `${x(p[0]).toFixed(1)},${yc(p[2]).toFixed(1)}`).join(" ");
  svg.innerHTML = `${g}<polygon class="speed-area" points="${area}"/><polyline class="cad-line" points="${cadLine}"/><polyline class="speed-line" points="${speedLine}"/>`;
}

export function initChart() {
  new ResizeObserver(() => {
    if (app.view === "training") drawChart();
  }).observe($("#chart"));
}
