// Requests to the server. Every POST answer carries the new state, which is rendered at once.
import { toast } from "./dom.js";
import { tMsg } from "./i18n.js";

let onState = () => {};
export function setStateHandler(fn) {
  onState = fn;
}

async function post(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({ ok: false, key: "err_server" }));
    if (data.state) onState(data.state);
    return data;
  } catch (e) {
    return { ok: false, key: "err_server" };
  }
}

// Belt or app command. Errors always show a toast, success only when not quiet.
export async function cmd(name, args = {}, quiet = false) {
  const d = await post("/cmd", { cmd: name, ...args });
  if (!d.ok) toast(tMsg(d), true);
  else if (!quiet) toast(tMsg(d));
  return !!d.ok;
}

export async function saveSettings(body, quiet = false) {
  const d = await post("/settings", body);
  if (!d.ok) toast(tMsg(d), true);
  else if (!quiet) toast(tMsg(d));
  return !!d.ok;
}

export async function getJSON(path) {
  try {
    const r = await fetch(path, { cache: "no-store" });
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  }
}
