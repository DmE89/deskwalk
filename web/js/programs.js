// Interval programs: card on the training view, list and editor in the programs view.
import { cmd, saveSettings } from "./api.js";
import { $, $$, ICONS, armed, esc, setText } from "./dom.js";
import { U, clampKmh, fmtSpeed, ms, numVal } from "./format.js";
import { t } from "./i18n.js";
import { app, connected } from "./state.js";

let programsKey = "";
let editing = null;

const programs = () => (app.S ? app.S.settings.programs : []);

function segBarHtml(segs) {
  const max = Math.max(6, ...segs.map((s) => s.speed));
  return segs.map((s) => `<span style="--w:${s.seconds};--h:${Math.max(18, (s.speed / max) * 100)}%" title="${esc(fmtSpeed(s.speed))} · ${ms(s.seconds)}"></span>`).join("");
}

function progInfo(p) {
  const speeds = p.segments.map((s) => s.speed);
  return t("prog_info", {
    n: p.segments.length,
    time: ms(p.segments.reduce((a, s) => a + s.seconds, 0)),
    min: fmtSpeed(Math.min(...speeds), false),
    max: fmtSpeed(Math.max(...speeds)),
  });
}

export function fillProgramSelect() {
  const sel = $("#progSelect");
  const prev = sel.value;
  const list = programs();
  sel.innerHTML = list.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  if (list.some((p) => p.id === prev)) sel.value = prev;
  const p = list.find((x) => x.id === sel.value);
  setText($("#progInfo"), p ? progInfo(p) : "");
}

// training view card
export function renderProgram() {
  const key = JSON.stringify(programs());
  if (key !== programsKey) {
    programsKey = key;
    fillProgramSelect();
    if (app.view === "programs") renderProgramList();
  }
  const p = app.S.program;
  $("#progIdle").classList.toggle("hidden", !!p);
  $("#progActive").classList.toggle("hidden", !p);
  $("#progStart").disabled = !connected() || !programs().length;
  $$("[data-pstart]").forEach((b) => {
    b.disabled = !connected();
  });
  if (!p) return;
  setText($("#progName"), p.name);
  setText($("#progStep"), t("prog_step", { i: p.index + 1, n: p.count }));
  setText($("#progTotal"), t("prog_left", { time: ms(p.total_remaining) }));
  setText($("#progRemain"), ms(p.seg_remaining));
  setText($("#progSpeed"), fmtSpeed(p.seg_speed));
  const bar = $("#progBar");
  const barKey = p.id + JSON.stringify(p.segments) + app.units + app.lang;
  if (bar.dataset.key !== barKey) {
    bar.innerHTML = segBarHtml(p.segments);
    bar.dataset.key = barKey;
  }
  $$("span", bar).forEach((s, i) => {
    s.className = i < p.index ? "done" : i === p.index ? "now" : "";
    if (i === p.index) s.style.setProperty("--fill", `${(1 - p.seg_remaining / p.seg_seconds) * 100}%`);
  });
}

// programs view
export function renderProgramList() {
  const c = connected();
  $("#programList").innerHTML = programs().map((p) => {
    const total = p.segments.reduce((a, s) => a + s.seconds, 0);
    return `<div class="card program"><div class="card-head"><h2 class="prog-name">${esc(p.name)}</h2><span class="muted num">${ms(total)} min</span></div>
      <p class="hint">${esc(progInfo(p))}</p><div class="segbar" aria-hidden="true">${segBarHtml(p.segments)}</div>
      <div class="row-gap end"><button type="button" class="btn small danger" data-pdel="${esc(p.id)}">${t("delete")}</button><button type="button" class="btn small" data-pedit="${esc(p.id)}">${t("edit")}</button><button type="button" class="btn small primary" data-pstart="${esc(p.id)}"${c ? "" : " disabled"}>${t("start_program")}</button></div></div>`;
  }).join("") || `<div class="card empty">${t("no_programs")}</div>`;
}

function openEditor(p) {
  editing = p ? JSON.parse(JSON.stringify(p)) : { id: null, name: t("new_program_name"), segments: [{ speed: 3, seconds: 300 }, { speed: 4.5, seconds: 300 }] };
  setText($("#progEditorTitle"), p ? t("edit_program") : t("new_program"));
  $("#peName").value = editing.name;
  renderEditorRows();
  $("#progEditor").classList.remove("hidden");
  $("#peName").focus();
}

function renderEditorRows() {
  $("#peRows").innerHTML = editing.segments.map((s, i) => `<div class="seg-row" data-i="${i}"><span class="idx">${i + 1}</span>
    <label>${t("seg_speed", { unit: U.speed() })}<input type="number" step="0.1" inputmode="decimal" value="${Math.round(U.speedOut(s.speed) * 10) / 10}" data-k="speed"></label>
    <label>${t("seg_minutes")}<input type="number" min="0.25" max="180" step="0.25" inputmode="decimal" value="${Math.round((s.seconds / 60) * 100) / 100}" data-k="minutes"></label>
    <button type="button" class="icon-btn" data-rm="${i}" aria-label="${esc(t("remove_segment", { n: i + 1 }))}"${editing.segments.length > 1 ? "" : " disabled"}><svg class="i" aria-hidden="true"><use href="${ICONS}#i-x"/></svg></button></div>`).join("");
  updateEditorTotal();
}

function readEditor() {
  editing.name = $("#peName").value.trim();
  $$("#peRows .seg-row").forEach((row) => {
    const speed = numVal($("[data-k=speed]", row));
    const minutes = numVal($("[data-k=minutes]", row));
    editing.segments[Number(row.dataset.i)] = {
      speed: clampKmh(U.speedIn(Number.isFinite(speed) ? speed : 3)),
      seconds: Math.round((Number.isFinite(minutes) ? minutes : 0) * 60),
    };
  });
}

function updateEditorTotal() {
  setText($("#peTotal"), t("total_min", { time: ms(editing.segments.reduce((a, s) => a + (s.seconds || 0), 0)) }));
}

export function translatePrograms() {
  fillProgramSelect();
  if (editing && !$("#progEditor").classList.contains("hidden")) {
    readEditor();
    renderEditorRows();
  }
}

export function initPrograms(showView) {
  $("#peRows").addEventListener("input", () => {
    readEditor();
    updateEditorTotal();
  });
  $("#peRows").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-rm]");
    if (!rm) return;
    readEditor();
    if (editing.segments.length > 1) editing.segments.splice(Number(rm.dataset.rm), 1);
    renderEditorRows();
  });
  $("#peAdd").addEventListener("click", () => {
    readEditor();
    editing.segments.push({ ...editing.segments[editing.segments.length - 1] });
    renderEditorRows();
  });
  $("#peCancel").addEventListener("click", () => $("#progEditor").classList.add("hidden"));
  $("#peSave").addEventListener("click", async () => {
    readEditor();
    const list = programs().slice();
    const idx = list.findIndex((p) => p.id === editing.id);
    if (idx >= 0) list[idx] = editing;
    else list.push({ ...editing, id: null });
    if (await saveSettings({ programs: list })) $("#progEditor").classList.add("hidden");
  });
  $("#progNew").addEventListener("click", () => openEditor(null));
  $("#programList").addEventListener("click", async (e) => {
    const ed = e.target.closest("[data-pedit]");
    const st = e.target.closest("[data-pstart]");
    const dl = e.target.closest("[data-pdel]");
    if (ed) openEditor(programs().find((p) => p.id === ed.dataset.pedit));
    if (st && (await cmd("program_start", { id: st.dataset.pstart }))) showView("training");
    if (dl && armed(dl, t("confirm_delete"), t("delete"))) await saveSettings({ programs: programs().filter((p) => p.id !== dl.dataset.pdel) });
  });
  $("#progSelect").addEventListener("change", fillProgramSelect);
  $("#progStart").addEventListener("click", () => cmd("program_start", { id: $("#progSelect").value }));
  $("#progStop").addEventListener("click", () => cmd("program_stop"));
}
