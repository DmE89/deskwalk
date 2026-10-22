// DOM helpers, toast and number animation.
import { app } from "./state.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export const ICONS = "assets/icons/icons.svg";
export const icon = (name) => `<svg class="i" aria-hidden="true"><use href="${ICONS}#i-${name}"/></svg>`;

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

export function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

export function setPressed(el, on) {
  const value = on ? "true" : "false";
  if (el.getAttribute("aria-pressed") !== value) el.setAttribute("aria-pressed", value);
}

let toastTimer;
export function toast(message, error = false) {
  if (!message) return;
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), error ? 4500 : 2200);
}

// Count numbers smoothly to the new value. Language and units are part of the key,
// so a switch shows the new format at once instead of animating from the old unit.
export function tween(el, to, fmt, dur = 450) {
  if (!el) return;
  const key = fmt.toString() + app.lang + app.units;
  if (el._to === to && el._key === key) return;
  const from = el._to === undefined || el._key !== key ? to : el._cur ?? to;
  el._to = to;
  el._key = key;
  cancelAnimationFrame(el._raf);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (from === to || document.hidden || reduce) {
    el._cur = to;
    el.textContent = fmt(to);
    return;
  }
  const t0 = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    el._cur = from + (to - from) * e;
    el.textContent = fmt(el._cur);
    if (k < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

// Two-step confirm instead of a browser dialog.
export function armed(btn, confirmLabel, label) {
  if (btn.dataset.armed === "1") return true;
  btn.dataset.armed = "1";
  btn.textContent = confirmLabel;
  setTimeout(() => {
    btn.dataset.armed = "";
    btn.textContent = label;
  }, 3000);
  return false;
}
