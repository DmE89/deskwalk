// Number, time and unit formatting. The belt and the server work in metric,
// the browser converts only for display and input.
import { app } from "./state.js";

export const locale = () => (app.lang === "de" ? "de-DE" : "en-US");

const nfCache = {};
export function nf(value, digits = 0) {
  const key = locale() + digits;
  nfCache[key] ||= new Intl.NumberFormat(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return nfCache[key].format(value);
}

const pad = (n) => String(n).padStart(2, "0");
const whole = (s) => Math.max(0, Math.round(s));
export const hms = (s) => { s = whole(s); return `${Math.floor(s / 3600)}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`; };
export const ms = (s) => { s = whole(s); return `${Math.floor(s / 60)}:${pad(s % 60)}`; };
export const hm = (s) => { s = whole(s); return `${Math.floor(s / 3600)}:${pad(Math.floor(s / 60) % 60)}`; };

export const dateFmt = (ts) => new Date(ts * 1000).toLocaleDateString(locale(), { weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit" });
export const timeFmt = (ts) => new Date(ts * 1000).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
export const dayFmt = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(locale(), { day: "2-digit", month: "2-digit", year: "numeric" });
export const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

const KM_PER_MI = 1.609344;
const KG_PER_LB = 0.45359237;
const CM_PER_IN = 2.54;

export const imp = () => app.units === "imperial";
export const U = {
  speed: () => (imp() ? "mph" : "km/h"),
  dist: () => (imp() ? "mi" : "km"),
  weight: () => (imp() ? "lb" : "kg"),
  len: () => (imp() ? "in" : "cm"),
  speedOut: (kmh) => (imp() ? kmh / KM_PER_MI : kmh),
  speedIn: (v) => (imp() ? v * KM_PER_MI : v),
  distOut: (km) => (imp() ? km / KM_PER_MI : km),
  distIn: (v) => (imp() ? v * KM_PER_MI : v),
  weightOut: (kg) => (imp() ? kg / KG_PER_LB : kg),
  weightIn: (v) => (imp() ? v * KG_PER_LB : v),
  lenOut: (cm) => (imp() ? cm / CM_PER_IN : cm),
};

export const fmtSpeed = (kmh, unit = true) => nf(U.speedOut(kmh), 1) + (unit ? " " + U.speed() : "");
export const fmtDist = (km, digits = 2, unit = true) => nf(U.distOut(km), digits) + (unit ? " " + U.dist() : "");
export const fmtLen = (m) => nf(U.lenOut(m * 100), 1) + " " + U.len();

export const SPEED_MIN = 0.5;
export const SPEED_MAX = 6.0;
export const speedLimits = () => (imp() ? { min: 0.3, max: 3.7 } : { min: SPEED_MIN, max: SPEED_MAX });
export const QUICK = { metric: [2, 3, 4, 5], imperial: [1.5, 2, 2.5, 3] };
export const clampKmh = (v) => Math.round(Math.min(SPEED_MAX, Math.max(SPEED_MIN, v)) * 10) / 10;
export const round1 = (v) => Math.round(v * 10) / 10;

// Number input that accepts a comma as decimal separator. NaN when empty or invalid.
export function numVal(input) {
  const raw = String(input.value).trim().replace(",", ".");
  return raw === "" ? NaN : Number(raw);
}
