// Shared app state. The server is the source of truth, the browser keeps only display preferences.

function readPref(key, fallback, allowed) {
  try {
    const value = localStorage.getItem(key);
    return allowed.includes(value) ? value : fallback;
  } catch (e) {
    return fallback;
  }
}

export function savePref(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    // storage blocked: the choice lasts until the page is reloaded
  }
}

const browserLang = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";

export const app = {
  S: null,               // last state from the server
  lastStateAt: 0,
  lang: readPref("dw-lang", browserLang, ["en", "de"]),
  units: readPref("dw-units", "metric", ["metric", "imperial"]),
  theme: readPref("dw-theme", "system", ["system", "dark", "light"]),
  view: "training",
};

export const connected = () => !!(app.S && app.S.connected);
export const running = () => !!(app.S && app.S.belt && app.S.belt.running);
