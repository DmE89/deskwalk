// Training view: speed ring, tiles, start/stop, speed controls.
import { cmd } from "./api.js";
import { $, $$, setPressed, setText, tween } from "./dom.js";
import { QUICK, U, clampKmh, fmtDist, hm, hms, ms, nf, speedLimits, timeFmt } from "./format.js";
import { plural, t } from "./i18n.js";
import { app, connected, running } from "./state.js";

const RING_LEN = 2 * Math.PI * 94;

// speed the user just chose, shown until the belt reports it (or 6 s passed)
let target = null;
let targetUntil = 0;
let sendTimer = null;
let dragging = false;
let busy = false;

function baseSpeed() {
  if (target !== null) return target;
  if (running()) return app.S.belt.speed;
  return app.S ? app.S.last_speed : 2;
}

export function resetTarget() {
  target = null;
}

export function setTarget(kmh, delay = 300) {
  target = clampKmh(kmh);
  targetUntil = Date.now() + 6000;
  renderSpeedUi();
  clearTimeout(sendTimer);
  sendTimer = setTimeout(() => cmd("speed", { speed: target }, true), delay);
}

// plus/minus 0.1 in the displayed unit, then back to km/h (belt resolution is 0.1 km/h)
export function nudge(dir) {
  if (!connected()) return;
  const shown = Math.round(U.speedOut(baseSpeed()) * 10) / 10;
  let next = clampKmh(U.speedIn(shown + dir * 0.1));
  if (next === baseSpeed()) next = clampKmh(next + dir * 0.1);
  setTarget(next);
}

export function toggleStartStop() {
  if (!connected() || busy) return;
  busy = true;
  cmd(running() ? "stop" : "start").finally(() => {
    busy = false;
  });
}

const quickKmh = (b) => clampKmh(U.speedIn(QUICK[app.units][b.dataset.quick]));

export function renderSpeedUi() {
  const S = app.S;
  const sl = $("#slider");
  if (!dragging) sl.value = U.speedOut(baseSpeed());
  const lim = speedLimits();
  sl.style.setProperty("--p", `${((sl.value - lim.min) / (lim.max - lim.min)) * 100}%`);
  const shown = `${nf(Number(sl.value), 1)} ${U.speed()}`;
  setText($("#sliderVal"), shown);
  sl.setAttribute("aria-valuetext", shown);
  let txt = "";
  let on = false;
  if (target !== null) {
    txt = t("target_to", { speed: target });
    on = true;
  } else if (S && S.paused.active) {
    txt = t("target_paused", { speed: S.paused.speed });
  } else if (S && S.meeting.active) {
    txt = t("target_meeting", { speed: S.meeting.restore_speed });
  } else if (connected() && !running() && S.settings.restore_speed) {
    txt = t("target_start", { speed: S.last_speed });
  }
  setText($("#target"), txt);
  $("#target").classList.toggle("on", on);
}

// labels that depend on language or units
export function translateTraining() {
  setText($("#speedUnit"), U.speed());
  const lim = speedLimits();
  const sl = $("#slider");
  sl.min = lim.min;
  sl.max = lim.max;
  setText($("#sliderMin"), nf(lim.min, 1));
  setText($("#sliderMax"), nf(lim.max, 1));
  $$("[data-quick]").forEach((b) => {
    const v = QUICK[app.units][b.dataset.quick];
    setText(b, `${nf(v, v % 1 ? 1 : 0)} ${U.speed()}`);
  });
}

let limitsKey = "";

export function renderTraining() {
  const S = app.S;
  const key = JSON.stringify(S.limits) + app.units + app.lang;
  if (key !== limitsKey) {
    limitsKey = key;
    translateTraining();
  }
  const c = S.connected;
  const belt = S.belt || {};
  const run = running();

  const hero = $("#hero");
  hero.classList.toggle("running", run);
  hero.classList.toggle("stale", c && S.stale);
  tween($("#speed"), c && belt.speed != null ? U.speedOut(belt.speed) : 0, (v) => nf(v, 1), 250);
  if (target !== null && (Date.now() > targetUntil || (run && Math.abs(belt.speed - target) < 0.05))) target = null;
  renderSpeedUi();

  const g = S.goal;
  $("#ringProg").style.strokeDashoffset = RING_LEN * (1 - g.progress);
  $("#ringProg").classList.toggle("zero", g.progress <= 0);
  if (g.type === "km") {
    tween($("#goalCur"), U.distOut(g.current), (v) => nf(v, 2));
    setText($("#goalMax"), nf(U.distOut(g.value), 1));
    setText($("#goalUnit"), U.dist());
  } else {
    tween($("#goalCur"), g.current, (v) => nf(Math.round(v)));
    setText($("#goalMax"), nf(g.value));
    setText($("#goalUnit"), t("steps"));
  }
  setText($("#goalPct"), `${nf(Math.floor(g.progress * 100))} %`);
  setText($("#streakNum"), String(S.streak.current));
  setText($("#streakUnit"), plural(S.streak.current));
  const sess = S.session;
  let line = "";
  if (sess && sess.ends_in != null) line = t("session_ends", { time: ms(sess.ends_in) });
  else if (sess) line = t("session_since", { time: timeFmt(sess.start_ts) });
  setText($("#sessionLine"), line);

  // tiles
  const td = S.today;
  tween($("#tTime"), sess ? sess.active_s : 0, hms, 300);
  tween($("#tDist"), U.distOut(sess ? sess.distance_km : 0), (v) => nf(v, 2));
  tween($("#tSteps"), sess ? sess.own_steps : 0, (v) => nf(Math.round(v)));
  tween($("#tKcal"), sess ? sess.calories : 0, (v) => nf(Math.round(v)));
  setText($("#tTimeSub"), t("today_val", { v: hms(td.seconds) }));
  setText($("#tDistSub"), `${U.dist()} · ${t("today_val", { v: nf(U.distOut(td.km), 2) })}`);
  setText($("#tStepsSub"), t("cadence_val", { v: nf(S.cadence) }));
  $("#tStepsSub").title = t("belt_steps_title", { v: nf(sess ? sess.belt_steps : 0) });
  setText($("#tKcalSub"), `kcal · ${t("today_val", { v: nf(td.kcal) })}`);

  // controls
  setText($("#startStopText"), run ? t("stop") : t("start"));
  $("#startStop").classList.toggle("stop", run);
  $("#startStop").disabled = !c;
  $("#minus").disabled = !c;
  $("#plus").disabled = !c;
  $("#slider").disabled = !c;
  $$("#quick .chip").forEach((b) => {
    b.disabled = !c;
  });
  const meeting = S.meeting.active;
  const paused = S.paused.active;
  setPressed($("#meetingBtn"), meeting);
  setText($("#meetingBtn"), meeting ? t("meeting_off") : t("meeting"));
  setPressed($("#pauseBtn"), paused);
  setText($("#pauseBtn"), paused ? t("resume") : t("pause"));
  $("#pauseBtn").disabled = !c || (!run && !paused);
  $("#meetingBtn").disabled = !c || (!run && !meeting);
  $$("[data-quick]").forEach((b) => setPressed(b, run && Math.abs(belt.speed - quickKmh(b)) < 0.05));

  // right column
  tween($("#dDist"), U.distOut(td.km), (v) => `${nf(v, 2)} ${U.dist()}`);
  tween($("#dSteps"), td.steps, (v) => nf(Math.round(v)));
  setText($("#dTime"), `${hm(td.seconds)} h`);
  setText($("#dSessions"), String(td.sessions));
  const m = S.maintenance;
  $("#oilCard").classList.toggle("hidden", !m.oil_due);
  if (m.oil_due_reason === "days") setText($("#oilText"), t("oil_text_days", { d: m.days_since_oil, i: m.oil_interval_days }));
  else if (m.oil_due) setText($("#oilText"), t("oil_text", { d: fmtDist(m.since_oil_km, 0), i: fmtDist(m.oil_interval_km, 0) }));
  const guide = S.guide;
  $("#noviceCard").classList.toggle("hidden", !(c && (S.novice_limit || guide)));
  $("#noviceInfo").classList.toggle("hidden", !!guide);
  $("#guideBox").classList.toggle("hidden", !guide);
  if (guide) {
    const text = guide.phase === "stopping" ? t("guide_stopping")
      : t("guide_walk", { i: guide.index + 1, n: guide.count, speed: guide.speed, s: Math.max(0, Math.round(guide.hold - guide.held)) });
    setText($("#guideText"), text);
    $("#guideMeter").style.setProperty("--w", `${guide.phase === "stopping" ? 100 : (guide.held / guide.hold) * 100}%`);
  }
}

export function initTraining() {
  $("#startStop").addEventListener("click", toggleStartStop);
  $("#minus").addEventListener("click", () => nudge(-1));
  $("#plus").addEventListener("click", () => nudge(1));
  $$("[data-quick]").forEach((b) => b.addEventListener("click", () => setTarget(quickKmh(b), 0)));
  $("#meetingBtn").addEventListener("click", () => cmd("meeting"));
  $("#pauseBtn").addEventListener("click", () => cmd("pause"));
  $("#oiledBtn2").addEventListener("click", () => cmd("oiled"));
  $("#noviceUnlock").addEventListener("click", () => cmd("guide_start"));
  $("#guideCancel").addEventListener("click", () => cmd("guide_cancel"));
  const slider = $("#slider");
  // the slider sends only when released, keyboard changes are sent after a short pause
  slider.addEventListener("pointerdown", () => {
    dragging = true;
  });
  slider.addEventListener("input", () => {
    target = clampKmh(U.speedIn(parseFloat(slider.value)));
    targetUntil = Date.now() + 6000;
    renderSpeedUi();
    if (!dragging) setTarget(target, 600);
  });
  slider.addEventListener("change", () => {
    const fromPointer = dragging;
    dragging = false;
    setTarget(U.speedIn(parseFloat(slider.value)), fromPointer ? 0 : 400);
  });
  window.addEventListener("pointerup", () => {
    dragging = false;
  });
}
