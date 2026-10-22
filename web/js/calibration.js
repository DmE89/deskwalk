// Calibration dialog: walk 60 s at a fixed speed, then enter the counted steps.
import { cmd } from "./api.js";
import { $, setText, toast } from "./dom.js";
import { U, clampKmh, numVal, round1 } from "./format.js";
import { t } from "./i18n.js";
import { app, connected, running } from "./state.js";

const DURATION = 60;
const cal = { phase: 0, timer: null, speeds: [], left: null, speed: 3, t0: 0, measured: 0, opener: null };

export const calibrationOpen = () => cal.phase !== 0;

function show(step) {
  [1, 2, 3].forEach((i) => $(`#calStep${i}`).classList.toggle("hidden", i !== step));
  setText($("#calNext"), step === 1 ? t("cal_go") : step === 2 ? t("cal_running") : t("save"));
  $("#calNext").disabled = step === 2;
  cal.phase = step;
}

function open() {
  if (!connected()) {
    toast(t("connect_first"), true);
    return;
  }
  cal.opener = document.activeElement;
  $("#calSpeed").value = round1(U.speedOut(running() ? app.S.belt.speed : 3));
  $("#calSteps").value = "";
  show(1);
  $("#calDialog").classList.add("show");
  $("#calSpeed").focus();
}

export function closeCalibration() {
  clearInterval(cal.timer);
  cal.timer = null;
  cal.phase = 0;
  $("#calDialog").classList.remove("show");
  if (cal.opener && cal.opener.focus) cal.opener.focus();
}

async function next() {
  if (cal.phase === 1) {
    const v = numVal($("#calSpeed"));
    cal.speed = clampKmh(U.speedIn(Number.isFinite(v) ? v : 3));
    show(2);
    setText($("#calHint"), t("cal_setting"));
    setText($("#calCount"), String(DURATION));
    $("#calMeter").style.setProperty("--w", "0%");
    await cmd("speed", { speed: cal.speed }, true);
    if (!running()) await cmd("start", {}, true);
    if (running() && Math.abs(app.S.belt.speed - cal.speed) > 0.05) await cmd("speed", { speed: cal.speed }, true);
    if (cal.phase !== 2) return; // cancelled meanwhile
    cal.left = null;
    cal.speeds = [];
    cal.timer = setInterval(tick, 250);
  } else if (cal.phase === 3) {
    const steps = parseInt($("#calSteps").value, 10);
    if (!(steps >= 10 && steps <= 400)) {
      $("#calSteps").setAttribute("aria-invalid", "true");
      toast(t("enter_steps"), true);
      return;
    }
    if (await cmd("calibrate", { speed: cal.measured, steps, seconds: DURATION })) closeCalibration();
  }
}

function tick() {
  if (!connected()) {
    closeCalibration();
    toast(t("cal_lost"), true);
    return;
  }
  const S = app.S;
  if (cal.left === null) {
    // the countdown starts once the belt runs at the chosen speed
    if (running() && Math.abs(S.belt.speed - cal.speed) < 0.15) {
      cal.left = DURATION;
      cal.t0 = Date.now();
      setText($("#calHint"), t("cal_count_now", { speed: cal.speed }));
    }
    return;
  }
  cal.speeds.push(S.belt.speed);
  cal.left = Math.max(0, DURATION - Math.floor((Date.now() - cal.t0) / 1000));
  setText($("#calCount"), String(cal.left));
  $("#calMeter").style.setProperty("--w", `${((DURATION - cal.left) / DURATION) * 100}%`);
  if (cal.left === 0) {
    clearInterval(cal.timer);
    cal.timer = null;
    cal.measured = cal.speeds.reduce((a, b) => a + b, 0) / cal.speeds.length;
    setText($("#calDone"), t("cal_done", { speed: cal.measured }));
    show(3);
    $("#calSteps").focus();
  }
}

// keep Tab inside the dialog
function trapFocus(e) {
  if (e.key !== "Tab") return;
  const items = Array.from($("#calDialog").querySelectorAll("button:not(:disabled), input"))
    .filter((el) => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

export function initCalibration() {
  $("#calOpen").addEventListener("click", open);
  $("#calCancel").addEventListener("click", closeCalibration);
  $("#calNext").addEventListener("click", next);
  $("#calDialog").addEventListener("keydown", trapFocus);
  $("#calDialog").addEventListener("click", (e) => {
    if (e.target === $("#calDialog") && cal.phase !== 2) closeCalibration();
  });
  $("#calSteps").addEventListener("keydown", (e) => {
    if (e.key === "Enter") next();
  });
  $("#calSpeed").addEventListener("keydown", (e) => {
    if (e.key === "Enter") next();
  });
}
