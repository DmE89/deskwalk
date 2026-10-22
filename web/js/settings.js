// Settings view, belt preferences and the movement reminder.
import { cmd, saveSettings } from "./api.js";
import { $, $$, esc, setPressed, setText, toast } from "./dom.js";
import { U, fmtDist, fmtLen, fmtSpeed, imp, nf, numVal, round1, speedLimits } from "./format.js";
import { t } from "./i18n.js";
import { app, connected, running } from "./state.js";

const PREF_TEXT = {
  inteli: (v) => t(v ? "on" : "off").toLowerCase(),
  sensitivity: (v) => t({ 1: "high", 2: "medium", 3: "low" }[v]).toLowerCase(),
  child_lock: (v) => t(v ? "on" : "off").toLowerCase(),
  start_speed: (v) => fmtSpeed(v),
  max_speed: (v) => fmtSpeed(v),
};

// values from the server into the form (on opening the view and after a unit switch)
export function fillSettings() {
  const S = app.S;
  if (!S) return;
  const st = S.settings;
  $("#weight").value = round1(U.weightOut(st.weight_kg));
  $$("#goalType button").forEach((b) => setPressed(b, b.dataset.value === st.goal.type));
  $("#goalValue").value = st.goal.type === "km" ? round1(U.distOut(st.goal.value)) : st.goal.value;
  $("#meetingSpeed").value = round1(U.speedOut(st.meeting_speed));
  $("#remEnabled").checked = st.reminder.enabled;
  $("#remMinutes").value = st.reminder.minutes;
  $("#oilInterval").value = Math.round(U.distOut(st.oil_interval_km));
  $("#beltKmInput").value = "";
  $("#beltKmInput").placeholder = nf(U.distOut(st.belt_km), 1);
  const lim = speedLimits();
  ["startSpeed", "maxSpeed", "meetingSpeed", "calSpeed"].forEach((id) => {
    $("#" + id).min = lim.min;
    $("#" + id).max = lim.max;
  });
  $("#startSpeed").placeholder = nf(U.speedOut(st.prefs.start_speed ?? 2), 1);
  $("#maxSpeed").placeholder = nf(U.speedOut(st.prefs.max_speed ?? 6), 1);
  renderCalibration();
  renderSettingsLive();
  renderPermission();
}

// parts that change while the view is open
export function renderSettingsLive() {
  const S = app.S;
  const c = connected();
  const st = S.settings;
  $$("[data-pref]").forEach((seg) => {
    const val = st.prefs[seg.dataset.pref];
    $$("button", seg).forEach((b) => {
      b.disabled = !c;
      setPressed(b, val !== null && String(val) === b.dataset.value);
    });
  });
  Object.entries(PREF_TEXT).forEach(([k, f]) => {
    const v = st.prefs[k];
    setText($(`#pv-${k}`), v == null ? t("pref_unknown") : t("pref_last", { v: f(v) }));
  });
  $$("[data-send]").forEach((b) => {
    b.disabled = !c;
  });
  $("#applyPrefs").disabled = !c || !Object.values(st.prefs).some((v) => v !== null);
  $$("[data-config]").forEach((el) => {
    el.checked = !!st[el.dataset.config];
  });
  const m = S.maintenance;
  setText($("#beltKm"), t("belt_total", { v: fmtDist(m.belt_km, 1) }));
  setText($("#oilSince"), `${nf(U.distOut(m.since_oil_km), 1)} / ${fmtDist(m.oil_interval_km, 0)}`);
  $("#oilMeter").classList.toggle("due", m.oil_due);
  $("#oilMeter i").style.setProperty("--w", `${m.oil_interval_km ? Math.min(100, (m.since_oil_km / m.oil_interval_km) * 100) : 0}%`);
  setText($("#strideNow"), t("stride_now", { v: fmtLen(S.stride_m) }));
  const calKey = JSON.stringify(st.calibration) + app.units + app.lang;
  if ($("#calList").dataset.key !== calKey) renderCalibration();
  $("#calOpen").disabled = !c;
}

function renderCalibration() {
  const pts = app.S.settings.calibration;
  $("#calList").dataset.key = JSON.stringify(pts) + app.units + app.lang;
  $("#calList").innerHTML = pts.map((p, i) => `<div class="cal-item"><span class="num">${esc(t("cal_point", { speed: p.speed, steps: p.steps, s: p.seconds, stride: fmtLen(p.stride_m) }))}</span><button type="button" class="btn small danger" data-caldel="${i}">${t("remove")}</button></div>`).join("")
    || `<p class="hint">${t("no_calibration")}</p>`;
}

export function translateSettings() {
  setText($("#lblStartSpeed"), t("start_speed_lbl", { unit: U.speed() }));
  setText($("#lblMaxSpeed"), t("max_speed_lbl", { unit: U.speed() }));
  setText($("#lblWeight"), t("weight_lbl", { unit: U.weight() }));
  setText($("#lblMeeting"), t("meeting_lbl", { unit: U.speed() }));
  setText($("#lblOil"), t("oil_interval_lbl", { unit: U.dist() }));
  setText($("#lblBeltKm"), t("belt_km_lbl", { unit: U.dist() }));
  setText($("#lblCalSpeed"), t("cal_speed_lbl", { unit: U.speed() }));
  setText($("#goalTypeDist"), t(imp() ? "goal_miles" : "goal_km"));
  $$("#langSeg button").forEach((b) => setPressed(b, b.dataset.value === app.lang));
  $$("#unitSeg button").forEach((b) => setPressed(b, b.dataset.value === app.units));
}

// read a number field, mark it when invalid
function readNumber(input) {
  const v = numVal(input);
  const ok = Number.isFinite(v);
  input.setAttribute("aria-invalid", ok ? "false" : "true");
  if (!ok) {
    input.focus();
    toast(t("enter_number"), true);
  }
  return ok ? v : null;
}

// movement reminder
let lastReminder = 0;

function renderPermission() {
  const n = "Notification" in window && window.isSecureContext;
  setText($("#remPerm"), !n ? t("perm_none") : t("perm_" + Notification.permission));
  $("#remAsk").disabled = !n || Notification.permission !== "default";
}

function notify(text) {
  if ("Notification" in window && window.isSecureContext && Notification.permission === "granted") {
    try {
      new Notification(t("reminder_title"), { body: text, tag: "deskwalk-reminder", icon: "assets/icons/icon-192.png" });
      return;
    } catch (e) {
      // some mobile browsers only allow notifications from a service worker: fall back to the toast
    }
  }
  toast(`${t("reminder_title")}: ${text}`);
}

export function checkReminder() {
  const S = app.S;
  const rem = S.settings.reminder;
  if (!rem.enabled || running()) return;
  const now = Date.now() / 1000;
  const last = Math.max(S.last_walk_ts || 0, S.app_started_ts || 0);
  const limit = rem.minutes * 60;
  if (now - last >= limit && now - lastReminder >= limit) {
    lastReminder = now;
    notify(t("reminder_text", { m: rem.minutes }));
  }
}

export function initSettings({ onLang, onUnits, onTheme }) {
  $("#calList").addEventListener("click", (e) => {
    const b = e.target.closest("[data-caldel]");
    if (b) cmd("delete_calibration", { index: Number(b.dataset.caldel) }, true);
  });
  $$("[data-pref]").forEach((seg) => $$("button", seg).forEach((b) => b.addEventListener("click", () => {
    cmd("pref", { name: seg.dataset.pref, value: JSON.parse(b.dataset.value) });
  })));
  $$("[data-send]").forEach((b) => b.addEventListener("click", () => {
    const input = $(`#${b.dataset.input}`);
    const v = readNumber(input);
    if (v === null) return;
    cmd("pref", { name: b.dataset.send, value: round1(U.speedIn(v)) }).then((ok) => {
      if (ok) input.value = "";
    });
  }));
  $("#applyPrefs").addEventListener("click", () => cmd("apply_prefs"));
  $$("[data-config]").forEach((el) => el.addEventListener("change", () => saveSettings({ [el.dataset.config]: el.checked }, true)));
  $$("#goalType button").forEach((b) => b.addEventListener("click", () => {
    $$("#goalType button").forEach((x) => setPressed(x, x === b));
    $("#goalValue").value = b.dataset.value === "km" ? (imp() ? 3 : 5) : 8000;
  }));
  $("#saveBody").addEventListener("click", () => {
    const pressed = $("#goalType [aria-pressed=true]");
    const type = pressed ? pressed.dataset.value : "steps";
    const weight = readNumber($("#weight"));
    const gv = readNumber($("#goalValue"));
    const meeting = readNumber($("#meetingSpeed"));
    if (weight === null || gv === null || meeting === null) return;
    saveSettings({
      weight_kg: round1(U.weightIn(weight)),
      meeting_speed: round1(U.speedIn(meeting)),
      goal: { type, value: type === "km" ? Math.round(U.distIn(gv) * 100) / 100 : Math.round(gv) },
    });
  });
  const saveReminder = () => {
    const minutes = readNumber($("#remMinutes"));
    if (minutes !== null) saveSettings({ reminder: { enabled: $("#remEnabled").checked, minutes } });
  };
  $("#remSave").addEventListener("click", saveReminder);
  $("#remEnabled").addEventListener("change", saveReminder);
  $("#oilSave").addEventListener("click", () => {
    const v = readNumber($("#oilInterval"));
    if (v !== null) saveSettings({ oil_interval_km: Math.round(U.distIn(v) * 10) / 10 });
  });
  $("#beltKmSave").addEventListener("click", async () => {
    const v = readNumber($("#beltKmInput"));
    if (v !== null && (await saveSettings({ belt_km: Math.round(U.distIn(v) * 100) / 100 }))) fillSettings();
  });
  $("#oiledBtn").addEventListener("click", () => cmd("oiled"));
  $("#remAsk").addEventListener("click", async () => {
    if ("Notification" in window) await Notification.requestPermission();
    renderPermission();
  });
  $("#remTest").addEventListener("click", () => notify(t("reminder_test")));
  $$("#langSeg button").forEach((b) => b.addEventListener("click", () => onLang(b.dataset.value)));
  $$("#unitSeg button").forEach((b) => b.addEventListener("click", () => onUnits(b.dataset.value)));
  $$("#themeSeg button").forEach((b) => b.addEventListener("click", () => onTheme(b.dataset.value)));
  $$("input[type=number]").forEach((input) => input.addEventListener("input", () => input.removeAttribute("aria-invalid")));
}
