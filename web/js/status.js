// Status view: connection and device details, events, log, error codes, stored walks, calibration run.
import { cmd } from "./api.js";
import { $, esc, setText } from "./dom.js";
import { dateFmt, fmtDist, hms, locale, nf, timeFmt } from "./format.js";
import { t, tMsg } from "./i18n.js";
import { app } from "./state.js";

let logLines = [];
const ERROR_CODES = ["E01", "E02", "E05", "E06", "E08", "E10", "E11"];

export function connLabel() {
  const S = app.S;
  if (S.conn === "waiting" && S.retry_in != null) return t("conn_retry", { s: S.retry_in });
  return t("conn_" + S.conn);
}

export function addLogLines(lines) {
  logLines = logLines.concat(lines).slice(-50);
  renderLog();
}

export function translateStatus() {
  $("#errorCodes").innerHTML = ERROR_CODES.map((c) => `<dt>${c}</dt><dd>${esc(t("code_" + c))}</dd>`).join("");
}

function setHtml(el, html) {
  if (el.dataset.h !== html) {
    el.innerHTML = html;
    el.dataset.h = html;
  }
}

export function renderStatus() {
  const S = app.S;
  if (!S) return;
  const L = S.link;
  const info = S.device_info;
  const rows = [
    [t("st_state"), connLabel()],
    [t("st_message"), tMsg(S.message) || "-"],
    [t("st_device"), (S.device && S.device.name) || "-"],
    [t("st_model"), info ? info.model || "-" : "-"],
    [t("st_firmware"), info ? `${info.firmware} · ${t("st_protocol")} ${info.protocol}` : "-"],
    [t("st_speed_range"), `${nf(S.limits.min, 1)} - ${nf(S.limits.max, 1)} km/h`],
    [t("st_last_reply"), S.status_age != null ? t("ago_s", { v: nf(S.status_age, 1) }) : "-"],
    [t("st_latency"), L.latency_ms != null ? `${L.latency_ms} ms` : "-"],
    [t("st_since"), L.connected_since ? timeFmt(L.connected_since) : "-"],
    [t("st_packets"), nf(L.packets)],
    [t("st_drops"), `${L.connects} / ${L.drops}`],
    [t("st_attempts"), String(S.attempt)],
    ["deskwalk", S.version || "-"],
  ];
  setHtml($("#statusKv"), rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join(""));
  setText($("#connBtn"), S.want_connected ? t("disconnect") : t("connect"));
  $("#connBtn").classList.toggle("primary", !S.want_connected);
  const ev = S.events.map((e) => {
    const cls = e.level === "warning" ? "w" : e.level === "error" ? "e" : "";
    return `<span class="${cls}">${new Date(e.t * 1000).toLocaleTimeString(locale())}  ${esc(t(e.key, e.args))}</span>`;
  }).join("\n") || esc(t("nothing_yet"));
  setHtml($("#events"), ev);
  renderRecords();
}

function renderRecords() {
  const S = app.S;
  const c = S.connected;
  const running = !!(S.belt && S.belt.running);
  $("#recordRead").disabled = !c || running;
  $("#recordRead").title = running ? t("err_belt_running") : "";
  const list = S.belt_records;
  setText($("#recordsAt"), list ? timeFmt(list.read_at) : "");
  if (!list) {
    setHtml($("#recordList"), "");
    return;
  }
  const html = list.items.map((r) => {
    const when = r.start_ts ? `${dateFmt(r.start_ts)} ${timeFmt(r.start_ts)}` : t("rec_unknown_time");
    let action;
    if (r.imported) action = `<span class="tag">${t("imported_tag")}</span>`;
    else if (r.known) action = `<span class="tag">${t("rec_known")}</span>`;
    else action = `<button type="button" class="btn small primary" data-import="${esc(r.signature)}"${S.session ? " disabled" : ""}>${t("import_session")}</button>`;
    return `<div class="record-item"><span>${esc(when)}</span>${action}<span class="meta">${hms(r.time)} · ${esc(fmtDist(r.dist / 100))} · ${nf(r.steps)} ${t("steps")}</span></div>`;
  }).join("") || `<p class="hint">${t("no_stored_walks")}</p>`;
  setHtml($("#recordList"), html);
}

export function renderLog() {
  if (app.view !== "status") return;
  const box = $("#log");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  box.innerHTML = logLines.map((l) => `<span class="${/ERROR|CRITICAL/.test(l) ? "e" : /WARNING/.test(l) ? "w" : ""}">${esc(l)}</span>`).join("\n") || "-";
  if (atBottom) box.scrollTop = box.scrollHeight;
}

export function initStatus() {
  $("#connBtn").addEventListener("click", () => cmd(app.S && app.S.want_connected ? "disconnect" : "connect"));
  $("#forgetBtn").addEventListener("click", () => cmd("forget_device"));
  $("#recordRead").addEventListener("click", () => cmd("read_records"));
  $("#recordList").addEventListener("click", (e) => {
    const b = e.target.closest("[data-import]");
    if (b) cmd("import_record", { signature: b.dataset.import });
  });
}
