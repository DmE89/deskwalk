// Status view: connection details, events and the server log.
import { cmd } from "./api.js";
import { $, esc, setText } from "./dom.js";
import { locale, nf, timeFmt } from "./format.js";
import { t, tMsg } from "./i18n.js";
import { app } from "./state.js";

let logLines = [];

export function connLabel() {
  const S = app.S;
  if (S.conn === "waiting" && S.retry_in != null) return t("conn_retry", { s: S.retry_in });
  return t("conn_" + S.conn);
}

export function addLogLines(lines) {
  logLines = logLines.concat(lines).slice(-50);
  renderLog();
}

export function renderStatus() {
  const S = app.S;
  if (!S) return;
  const L = S.link;
  const rows = [
    [t("st_state"), connLabel()],
    [t("st_message"), tMsg(S.message) || "-"],
    [t("st_device"), (S.device && S.device.name) || "-"],
    [t("st_last_reply"), S.status_age != null ? t("ago_s", { v: nf(S.status_age, 1) }) : "-"],
    [t("st_latency"), L.latency_ms != null ? `${L.latency_ms} ms` : "-"],
    [t("st_since"), L.connected_since ? timeFmt(L.connected_since) : "-"],
    [t("st_packets"), nf(L.packets)],
    [t("st_drops"), `${L.connects} / ${L.drops}`],
    [t("st_attempts"), String(S.attempt)],
  ];
  const kv = $("#statusKv");
  const html = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("");
  if (kv.dataset.h !== html) {
    kv.innerHTML = html;
    kv.dataset.h = html;
  }
  setText($("#connBtn"), S.want_connected ? t("disconnect") : t("connect"));
  $("#connBtn").classList.toggle("primary", !S.want_connected);
  const ev = S.events.map((e) => {
    const cls = e.level === "warning" ? "w" : e.level === "error" ? "e" : "";
    return `<span class="${cls}">${new Date(e.t * 1000).toLocaleTimeString(locale())}  ${esc(t(e.key, e.args))}</span>`;
  }).join("\n") || esc(t("nothing_yet"));
  const evEl = $("#events");
  if (evEl.dataset.h !== ev) {
    evEl.innerHTML = ev;
    evEl.dataset.h = ev;
  }
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
}
