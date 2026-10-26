// Entry point: live data, rendering, views, theme, keyboard.
import { cmd, setStateHandler } from "./api.js";
import { closeCalibration, calibrationOpen, initCalibration } from "./calibration.js";
import { drawChart, initChart, liveChart, loadChart } from "./chart.js";
import { $, $$, ICONS, setPressed, setText } from "./dom.js";
import { locale } from "./format.js";
import { initHistory, liveHistory, loadHistory, loadRecords, translateHistory } from "./history.js";
import { t, tMsg, translateStatic } from "./i18n.js";
import { initPrograms, renderProgram, renderProgramList, translatePrograms } from "./programs.js";
import { checkReminder, fillSettings, initSettings, renderSettingsLive, translateSettings } from "./settings.js";
import { app, connected, savePref } from "./state.js";
import { addLogLines, connLabel, initStatus, renderLog, renderStatus, translateStatus } from "./status.js";
import { initTraining, nudge, renderSpeedUi, renderTraining, resetTarget, toggleStartStop, translateTraining } from "./training.js";

const VIEWS = ["training", "history", "records", "programs", "settings", "status"];

// ── top bar and banner ──────────────────────────────────
function showBanner(text, error, withButton) {
  const b = $("#banner");
  b.classList.toggle("show", !!text);
  b.classList.toggle("error", !!error);
  setText($("#bannerText"), text || "");
  $("#bannerBtn").classList.toggle("hidden", !withButton);
}

function renderShell() {
  const S = app.S;
  const c = S.connected;
  const belt = S.belt || {};
  $("#dot").className = "dot" + (c ? " ok" : "") + (["searching", "connecting"].includes(S.conn) ? " busy" : "");
  setText($("#connText"), connLabel());
  setText($("#modeLabel"), c && belt.mode ? t("mode_" + belt.mode) : "-");
  $("#simBadge").classList.toggle("hidden", !S.simulation);
  const info = S.device_info;
  if (!c && !S.want_connected) showBanner(t("banner_disconnected"), false, true);
  else if (!c) showBanner(tMsg(S.message), ["error", "waiting"].includes(S.conn), S.conn === "waiting");
  else if (S.stale) showBanner(t("banner_stale"), false, false);
  else if (belt.state_name === "lock_off") showBanner(t("banner_lock_off"), true, false);
  else if (belt.error) showBanner(errorText(belt.error), true, false);
  else if (info && info.firmware_too_old) showBanner(t("banner_firmware_old", { model: info.model, fw: info.firmware }), false, false);
  else showBanner("");
}

const KNOWN_CODES = [1, 2, 5, 6, 8, 10, 11];
function errorText(code) {
  const name = `E${String(code).padStart(2, "0")}`;
  return t("banner_belt_error", { code: name, text: KNOWN_CODES.includes(code) ? t("code_" + name) : t("code_unknown") });
}

function renderOffline() {
  $("#dot").className = "dot";
  setText($("#connText"), t("server_down"));
  showBanner(t("server_down_banner"), true, false);
}

// ── rendering ───────────────────────────────────────────
function render(state) {
  const first = !app.S;
  app.S = state;
  app.lastStateAt = Date.now();
  if (document.body.classList.contains("preload")) setTimeout(() => document.body.classList.remove("preload"), 300);
  renderShell();
  renderTraining();
  renderProgram();
  liveChart();
  if (first && app.view === "settings") fillSettings();
  if (first && app.view === "programs") renderProgramList();
  if (app.view === "settings") renderSettingsLive();
  if (app.view === "status") renderStatus();
  liveHistory();
  checkReminder();
}
setStateHandler(render);

function translateAll() {
  translateStatic();
  translateTraining();
  translateHistory();
  translateSettings();
  translatePrograms();
  translateStatus();
  setText($("#keysHint"), t("keys_hint"));
  renderThemeButton();
  if (app.S) {
    render(app.S);
    renderLog();
  }
  if (app.view === "history") loadHistory();
  if (app.view === "records") loadRecords();
  if (app.view === "programs") renderProgramList();
  if (app.view === "settings") fillSettings();
  if (app.view === "training") drawChart();
}

// ── views ───────────────────────────────────────────────
function showView(name, focus = false) {
  if (!VIEWS.includes(name)) name = "training";
  app.view = name;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $$(".nav-btn").forEach((b) => {
    if (b.dataset.view === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "history") loadHistory();
  if (name === "records") loadRecords();
  if (name === "programs") renderProgramList();
  if (name === "settings") fillSettings();
  if (name === "status") {
    renderStatus();
    renderLog();
  }
  if (name === "training") setTimeout(drawChart);
  window.scrollTo({ top: 0 });
  if (focus) $("#main").focus({ preventScroll: true });
}

// ── theme ───────────────────────────────────────────────
const THEME_ICON = { system: "auto", dark: "moon", light: "sun" };
const THEME_NEXT = { system: "dark", dark: "light", light: "system" };

function renderThemeButton() {
  $("#themeBtn use").setAttribute("href", `${ICONS}#i-${THEME_ICON[app.theme]}`);
  $("#themeBtn").setAttribute("aria-label", t("theme_now", { name: t("theme_" + app.theme) }));
  $$("#themeSeg button").forEach((b) => setPressed(b, b.dataset.value === app.theme));
}

function setTheme(name) {
  app.theme = name;
  savePref("dw-theme", name);
  document.documentElement.dataset.theme = name;
  const dark = name === "dark" || (name === "system" && !matchMedia("(prefers-color-scheme: light)").matches);
  $('meta[name="theme-color"]').setAttribute("content", dark ? "#0B0D10" : "#F3F4F6");
  renderThemeButton();
}

// ── keyboard ────────────────────────────────────────────
// Shortcuts only when focus is not on a control that uses these keys itself.
const ownsKeys = (el) => !!(el && el.closest && el.closest("input, textarea, select, button, a, [role=dialog], [contenteditable]"));

function onKeyDown(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (calibrationOpen()) {
    if (e.key === "Escape") closeCalibration();
    return;
  }
  if (ownsKeys(e.target)) return;
  if (e.code === "Space") {
    e.preventDefault();
    if (!e.repeat) toggleStartStop();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    nudge(1);
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    nudge(-1);
  } else if ((e.key === "m" || e.key === "M") && !e.repeat && connected()) {
    cmd("meeting");
  } else if ((e.key === "p" || e.key === "P") && !e.repeat && connected()) {
    cmd("pause");
  }
}

// ── live data via Server-Sent Events ───────────────────
function connectStream() {
  const es = new EventSource("/events");
  es.addEventListener("state", (e) => {
    try {
      render(JSON.parse(e.data));
    } catch (err) {
      console.error(err);
    }
  });
  es.addEventListener("log", (e) => {
    try {
      addLogLines(JSON.parse(e.data));
    } catch (err) {
      console.error(err);
    }
  });
}

// ── screen wake lock while the page is visible ─────────
let wakeLock = null;
async function requestWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock || document.visibilityState !== "visible") return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => {
      wakeLock = null;
    });
  } catch (e) {
    // not allowed without a user gesture or on this connection, try again on the next tap
  }
}

// ── start ───────────────────────────────────────────────
function init() {
  initTraining();
  initChart();
  initHistory();
  initPrograms((name) => showView(name, true));
  initCalibration();
  initStatus();
  initSettings({
    onLang: (lang) => {
      app.lang = lang;
      savePref("dw-lang", lang);
      translateAll();
    },
    onUnits: (units) => {
      app.units = units;
      savePref("dw-units", units);
      resetTarget();
      translateAll();
      renderSpeedUi();
    },
    onTheme: setTheme,
  });

  $$("[data-view]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view, true)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.goto, true)));
  window.addEventListener("hashchange", () => {
    if (location.hash.slice(1) !== app.view) showView(location.hash.slice(1));
  });
  $("#bannerBtn").addEventListener("click", () => cmd("connect"));
  $("#themeBtn").addEventListener("click", () => setTheme(THEME_NEXT[app.theme]));
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => setTheme(app.theme));
  document.addEventListener("keydown", onKeyDown);
  // a mouse or touch click leaves no focus behind, so Space keeps working as start/stop;
  // keyboard users keep their focus (detail is 0 for keyboard activation)
  document.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest("button");
    if (el && e.detail > 0) el.blur();
  });
  $("#slider").addEventListener("pointerup", () => setTimeout(() => $("#slider").blur(), 0));
  document.addEventListener("pointerdown", requestWakeLock);
  document.addEventListener("visibilitychange", requestWakeLock);

  const tickClock = () => setText($("#clock"), new Date().toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" }));
  setInterval(tickClock, 1000);
  setInterval(() => {
    if (Date.now() - app.lastStateAt > 4000) renderOffline();
  }, 1000);

  setTheme(app.theme);
  translateAll();
  tickClock();
  showView(location.hash.slice(1) || "training");
  loadChart();
  connectStream();
}

init();
