// Status view: connection details, events and the server log.
import { cmd } from "./api.js";
import { $, $$, esc, setPressed, setText, toast } from "./dom.js";
import { fmtDist, hms, locale, nf, numVal, timeFmt } from "./format.js";
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
  renderExperimental();
}

const ERROR_CODES = ["E01", "E02", "E05", "E06", "E08", "E10", "E11"];
const BITS = 7;

export function translateStatus() {
  $("#errorCodes").innerHTML = ERROR_CODES.map((c) => `<dt>${c}</dt><dd>${esc(t("code_" + c))}</dd>`).join("");
  if (!$("#displayBits").children.length) {
    $("#displayBits").innerHTML = Array.from({ length: BITS }, (_, i) => `<label><input type="checkbox" data-bit="${i}" checked>${i + 1}</label>`).join("");
  }
}

// experimental belt functions
function renderExperimental() {
  const S = app.S;
  const c = S.connected;
  const prefs = S.settings.prefs;
  const rec = S.belt_record;
  $("#recordRead").disabled = !c;
  $("#recordImport").disabled = !rec || !!S.session;
  $("#recordImport").title = S.session ? t("err_session_running") : "";
  $("#recordKv").classList.toggle("hidden", !rec);
  if (rec) {
    const rows = [
      [t("rec_time"), hms(rec.time)], [t("rec_dist"), fmtDist(rec.dist / 100)], [t("rec_steps"), nf(rec.steps)],
      [t("rec_read_at"), timeFmt(rec.read_at)], [t("rec_raw"), rec.raw],
    ];
    const html = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("");
    if ($("#recordKv").dataset.h !== html) {
      $("#recordKv").innerHTML = html;
      $("#recordKv").dataset.h = html;
    }
  }
  $$("[data-pref=units_miles] button").forEach((b) => {
    b.disabled = !c;
    setPressed(b, prefs.units_miles !== null && String(prefs.units_miles) === b.dataset.value);
  });
  setText($("#pv-units_miles"), prefs.units_miles == null ? t("pref_unknown") : t("pref_last", { v: prefs.units_miles ? "mi" : "km" }));
  setText($("#pv-display"), prefs.display == null ? t("pref_unknown") : t("pref_last", { v: prefs.display }));
  const tg = prefs.target;
  setText($("#pv-target"), tg == null ? t("pref_unknown") : t("pref_last", { v: tg.type === "none" ? t("target_none") : `${t(tg.type)} ${tg.value}` }));
  $("#displaySend").disabled = !c;
  $("#targetSend").disabled = !c;
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
  $("#recordRead").addEventListener("click", () => cmd("ask_record"));
  $("#recordImport").addEventListener("click", () => cmd("import_record"));
  $("#displaySend").addEventListener("click", () => {
    const mask = $$("[data-bit]").reduce((m, el) => (el.checked ? m | (1 << Number(el.dataset.bit)) : m), 0);
    cmd("pref", { name: "display", value: mask });
  });
  $("#targetSend").addEventListener("click", () => {
    const type = $("#targetType").value;
    const v = numVal($("#targetValue"));
    if (type !== "none" && !(Number.isInteger(v) && v >= 0)) {
      $("#targetValue").setAttribute("aria-invalid", "true");
      toast(t("enter_number"), true);
      return;
    }
    cmd("pref", { name: "target", value: { type, value: type === "none" ? 0 : v } });
  });
}
