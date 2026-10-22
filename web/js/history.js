// History view (bars, heat map, session list, export) and records view.
import { cmd, getJSON } from "./api.js";
import { $, armed, esc, setText, toast } from "./dom.js";
import { U, dateFmt, dayFmt, fmtDist, hm, hms, isoDay, locale, nf, timeFmt } from "./format.js";
import { plural, t } from "./i18n.js";
import { app } from "./state.js";

let HIST = null;

export async function loadHistory() {
  const h = await getJSON("/history");
  if (!h) return;
  HIST = h;
  renderBars();
  renderHeat();
  renderSessions();
}

const goal = () => (app.S ? app.S.settings.goal : { type: "steps", value: 8000 });

function renderBars() {
  if (!HIST || app.view !== "history") return;
  const byDay = Object.fromEntries(HIST.days.map((d) => [d.day, d]));
  const days = [];
  for (let i = 29; i >= 0; i--) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    days.push(isoDay(d));
  }
  const g = goal();
  const goalD = g.type === "km" ? U.distOut(g.value) : null;
  const val = (d) => (byDay[d] ? U.distOut(byDay[d].km) : 0);
  const max = Math.max(1, goalD || 0, ...days.map(val));
  const svg = $("#bars");
  const W = Math.max(280, Math.round(svg.clientWidth || 900));
  const H = 220;
  const L = 30;
  const B = 22;
  const T = 8;
  const bw = (W - L) / 30;
  const labelEvery = W < 500 ? 7 : 5;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const step = max > 8 ? 2 : max > 4 ? 1 : 0.5;
  let out = "";
  for (let v = 0; v <= max + 1e-9; v += step) {
    out += `<line class="grid" x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${nf(v, step < 1 ? 1 : 0)}</text>`;
  }
  days.forEach((d, i) => {
    const v = val(d);
    const x = L + i * bw + bw * 0.18;
    const hgt = Math.max(v > 0 ? 2 : 0, H - B - y(v));
    const hit = byDay[d] && (g.type === "km" ? byDay[d].km >= g.value : byDay[d].steps >= g.value);
    out += `<rect class="bar${hit ? " hit" : ""}" x="${x}" y="${H - B - hgt}" width="${bw * 0.64}" height="${hgt}" rx="2"><title>${dayFmt(d)}: ${nf(v, 2)} ${U.dist()}</title></rect>`;
    if ((29 - i) % labelEvery === 0) {
      const last = i === 29;
      const label = new Date(d + "T12:00:00").toLocaleDateString(locale(), { day: "2-digit", month: "2-digit" });
      out += `<text x="${last ? W : x + bw * 0.32}" y="${H - 6}" text-anchor="${last ? "end" : "middle"}">${label}</text>`;
    }
  });
  if (goalD) out += `<line class="goal" x1="${L}" x2="${W}" y1="${y(goalD)}" y2="${y(goalD)}"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = out;
  const total = days.reduce((a, d) => a + (byDay[d] ? byDay[d].km : 0), 0);
  setText($("#histSum"), t("hist_sum", { v: fmtDist(total, 1) }));
  svg.setAttribute("aria-label", `${t("bars_label")}: ${fmtDist(total, 1)}`);
}

function renderHeat() {
  const byDay = Object.fromEntries(HIST.days.map((d) => [d.day, d.km]));
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const start = new Date(today);
  start.setDate(start.getDate() - 52 * 7 - ((today.getDay() + 6) % 7));
  const vals = Object.values(byDay).filter((v) => v > 0).sort((a, b) => a - b);
  const q = (p) => (vals.length ? vals[Math.min(vals.length - 1, Math.floor(p * vals.length))] : 1);
  const th = [q(0.25), q(0.5), q(0.75)];
  // weekday names from the browser locale (2024-01-01 was a Monday)
  const wd = [0, 2, 4].reduce((a, i) => {
    a[i] = new Date(2024, 0, 1 + i).toLocaleDateString(locale(), { weekday: "short" });
    return a;
  }, Array(7).fill(""));
  let html = "";
  let lastMonth = -1;
  let total = 0;
  let active = 0;
  wd.forEach((w, i) => {
    html += `<span class="wd" style="grid-row:${i + 2}">${w}</span>`;
  });
  const d = new Date(start);
  for (let col = 0; col < 53; col++) {
    for (let row = 0; row < 7; row++) {
      if (d > today) break;
      const iso = isoDay(d);
      const v = byDay[iso] || 0;
      if (row === 0 && d.getMonth() !== lastMonth && d.getDate() <= 7) {
        lastMonth = d.getMonth();
        html += `<span class="m" style="grid-column:${col + 2}">${d.toLocaleDateString(locale(), { month: "short" })}</span>`;
      }
      const lv = v <= 0 ? 0 : v <= th[0] ? 1 : v <= th[1] ? 2 : v <= th[2] ? 3 : 4;
      if (v > 0) {
        total += v;
        active++;
      }
      html += `<i class="${lv ? "l" + lv : ""}" style="grid-column:${col + 2};grid-row:${row + 2}" title="${dayFmt(iso)}: ${esc(fmtDist(v))}"></i>`;
      d.setDate(d.getDate() + 1);
    }
  }
  $("#heat").innerHTML = html;
  setText($("#heatSum"), t("heat_sum", { v: fmtDist(total, 1), n: active, days: t(active === 1 ? "day_dat_one" : "day_dat_other") }));
  const sc = $("#heatScroll");
  sc.scrollLeft = sc.scrollWidth;
}

function renderSessions() {
  const rows = HIST.sessions.map((s) => {
    const dur = s.active_s;
    const avg = dur > 0 ? s.distance_km / (dur / 3600) : 0;
    const open = s.end_ts == null;
    return `<tr><td>${dateFmt(s.start_ts)} <span class="muted num">${timeFmt(s.start_ts)}</span>${open ? `<span class="open-tag">${t("running_tag")}</span>` : ""}</td>
      <td class="n">${hms(dur)}</td><td class="n">${nf(U.distOut(s.distance_km), 2)}</td>
      <td class="n hide-s">${nf(Math.round(s.own_steps))}</td><td class="n hide-s">${nf(Math.round(s.calories))}</td><td class="n hide-s">${nf(U.speedOut(avg), 1)}</td>
      <td class="act"><button type="button" class="btn small" data-export="csv" data-id="${s.id}">${t("export_csv")}</button><button type="button" class="btn small" data-export="tcx" data-id="${s.id}">${t("export_tcx")}</button>${open ? "" : `<button type="button" class="btn small danger" data-del="${s.id}">${t("delete")}</button>`}</td></tr>`;
  });
  $("#sessionRows").innerHTML = rows.join("") || `<tr><td colspan="7" class="empty">${t("no_sessions")}</td></tr>`;
}

// export, built in the browser (data stays metric)
async function exportSession(id, kind) {
  const s = await getJSON(`/history?session=${encodeURIComponent(id)}`);
  if (!s) {
    toast(t("err_not_found_session"), true);
    return;
  }
  const stamp = new Date(s.start_ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  const base = `deskwalk_${stamp.getFullYear()}-${pad(stamp.getMonth() + 1)}-${pad(stamp.getDate())}_${pad(stamp.getHours())}${pad(stamp.getMinutes())}`;
  let text;
  let type;
  if (kind === "csv") {
    text = "time,seconds,speed_kmh,cadence_spm\n"
      + s.samples.map((p) => `${new Date(p.ts * 1000).toISOString()},${Math.round(p.ts - s.start_ts)},${p.speed.toFixed(1)},${p.cadence}`).join("\n") + "\n";
    type = "text/csv";
  } else {
    const iso = (ts) => new Date(ts * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
    let dist = 0;
    const tps = s.samples.map((p, i) => {
      if (i) dist += (s.samples[i - 1].speed / 3.6) * (p.ts - s.samples[i - 1].ts);
      return `<Trackpoint><Time>${iso(p.ts)}</Time><DistanceMeters>${dist.toFixed(1)}</DistanceMeters><Extensions><ns3:TPX><ns3:Speed>${(p.speed / 3.6).toFixed(3)}</ns3:Speed><ns3:RunCadence>${Math.round(p.cadence / 2)}</ns3:RunCadence></ns3:TPX></Extensions></Trackpoint>`;
    }).join("");
    text = `<?xml version="1.0" encoding="UTF-8"?>\n<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2"><Activities><Activity Sport="Other"><Id>${iso(s.start_ts)}</Id><Lap StartTime="${iso(s.start_ts)}"><TotalTimeSeconds>${Math.round(s.active_s)}</TotalTimeSeconds><DistanceMeters>${(s.distance_km * 1000).toFixed(1)}</DistanceMeters><MaximumSpeed>${(s.max_speed / 3.6).toFixed(3)}</MaximumSpeed><Calories>${Math.round(s.calories)}</Calories><Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod><Track>${tps}</Track></Lap><Notes>Treadmill walk, ${Math.round(s.own_steps)} steps</Notes></Activity></Activities></TrainingCenterDatabase>\n`;
    type = "application/vnd.garmin.tcx+xml";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = `${base}.${kind}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export async function loadRecords() {
  const h = await getJSON("/history");
  if (!h) return;
  const r = h.records;
  const Ls = r.longest_session;
  const Bd = r.best_day;
  const Tt = r.totals;
  const Ks = r.streak;
  const card = (title, big, unit, when) => `<div class="card record"><h2 class="card-title">${title}</h2><div class="big num">${big}<small>${esc(unit)}</small></div><div class="when">${when}</div></div>`;
  $("#records").innerHTML = [
    card(t("rec_longest"), Ls ? hm(Ls.active_s) : "-", "h", Ls ? `${dateFmt(Ls.start_ts)} · ${esc(fmtDist(Ls.distance_km))}` : t("rec_none")),
    card(t("rec_best_day"), Bd ? nf(U.distOut(Bd.km), 2) : "-", U.dist(), Bd ? t("rec_day_sub", { date: dayFmt(Bd.day), steps: nf(Bd.steps) }) : t("rec_none")),
    card(t("rec_streak"), nf(Ks.best), plural(Ks.best), t("rec_streak_sub", { n: Ks.current })),
    card(t("rec_total_dist"), nf(U.distOut(Tt.km), 1), U.dist(), t("rec_dist_sub", { s: nf(Tt.sessions), d: nf(Tt.days) })),
    card(t("rec_total_time"), hm(Tt.seconds), "h", t("rec_time_sub", { steps: nf(Tt.steps), kcal: nf(Tt.kcal) })),
    card(t("rec_top_speed"), nf(U.speedOut(Tt.max_speed), 1), U.speed(), t("rec_speed_sub")),
  ].join("");
}

export function translateHistory() {
  setText($("#barsTitle"), t("bars_title", { unit: U.dist() }));
  setText($("#thDist"), U.dist());
  setText($("#thAvg"), t("avg_unit", { unit: U.speed() }));
}

export function initHistory() {
  $("#sessionRows").addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    const ex = e.target.closest("[data-export]");
    if (del && armed(del, t("confirm_delete"), t("delete")) && (await cmd("delete_session", { id: Number(del.dataset.del) }))) loadHistory();
    if (ex) exportSession(Number(ex.dataset.id), ex.dataset.export);
  });
  new ResizeObserver(renderBars).observe($("#bars"));
}
