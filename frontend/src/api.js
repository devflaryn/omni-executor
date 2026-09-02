/* Bridge to the Python backend, plus the engine event bus.

   The backend is a child process of the Tauri shell, reached over its stdio
   (see rpc.py). `invoke("call", …)` hands Rust a method name and its
   arguments; Rust writes a JSON line to the backend and resolves with the
   reply. What the page sees is unchanged from the pywebview days: `api(name,
   …args)` still returns whatever the Python method returned, and engine events
   still arrive through `window.omniEvent`.

   A plain browser (`npm run dev` without the shell) has no Tauri global at
   all. Every call there resolves to a `no_backend` result, and devMock.js can
   stand in for the whole surface. */

const TAURI = "__TAURI_INTERNALS__";

/** Is this page inside the Tauri shell? Synchronous — the global is injected
    before any of our script runs, so there is nothing to wait for. This is the
    detail that replaced pywebview's `pywebviewready` event and its 1.5s
    fallback timer. */
function inShell() {
  return typeof window !== "undefined" && Boolean(window[TAURI]);
}

/* Resolved lazily and once. The module is only imported when it exists, so a
   browser preview never pays for a failed dynamic import. */
let tauriApi = null;
async function shell() {
  if (!inShell()) return null;
  if (!tauriApi) {
    const [core, event] = await Promise.all([
      import("@tauri-apps/api/core"),
      import("@tauri-apps/api/event"),
    ]);
    tauriApi = { invoke: core.invoke, listen: event.listen };
  }
  return tauriApi;
}

/** True when a real backend is reachable — the desktop app rather than a
    browser tab. devMock installs itself as a stand-in, and counts. */
export async function hasBackend() {
  return inShell() || Boolean(window.__omniMock);
}

const noBackend = {
  ok: false,
  error: "no_backend",
  message: "Engine unavailable — run the desktop app (npm run tauri dev).",
};

/** Call a backend method. Engine-style calls always resolve to an object with
    an `ok` flag, so callers never need try/catch. */
export async function api(method, ...args) {
  if (window.__omniMock) {
    const handler = window.__omniMock[method];
    if (typeof handler !== "function") return noBackend;
    return handler(...args);
  }
  const bridge = await shell();
  if (!bridge) return noBackend;
  try {
    const reply = await bridge.invoke("call", { method, args });
    // rpc.py answers every call with {ok, result} or {ok, error, message}.
    // A failure is handed back in the same shape the engine calls use, so a
    // dead backend and a failed engine command read the same at the call site.
    if (reply && reply.ok === false) {
      return { ok: false, error: reply.error, message: reply.message };
    }
    return reply?.result;
  } catch (err) {
    // The Rust side refused: the backend never started, or it died mid-call.
    return { ok: false, error: "bridge_error", message: String(err?.message || err) };
  }
}

// ---- settings (Python persists to a JSON file; localStorage in a browser) ----

export async function loadSettings() {
  if (!(await hasBackend())) {
    try {
      return JSON.parse(localStorage.getItem("omni-settings")) || {};
    } catch {
      return {};
    }
  }
  const settings = await api("get_settings");
  return settings && !settings.error ? settings : {};
}

export function saveSettings(patch) {
  hasBackend().then(async (desktop) => {
    try {
      if (desktop) {
        await api("save_settings", patch);
      } else {
        const cur = JSON.parse(localStorage.getItem("omni-settings")) || {};
        localStorage.setItem("omni-settings", JSON.stringify({ ...cur, ...patch }));
      }
    } catch (err) {
      console.error("Failed to save settings:", err);
    }
  });
}

// ---- engine event bus ----

const listeners = new Set();

window.omniEvent = (event, payload) => {
  for (const listener of listeners) {
    try {
      listener(event, payload);
    } catch (err) {
      console.error("omniEvent listener failed:", err);
    }
  }
};

/* Python's Api._push writes {"event", "payload"} frames; Rust re-emits them on
   one Tauri event. Unwrapping them back into window.omniEvent here means every
   subscriber in the app is untouched by the move off pywebview. */
if (inShell()) {
  shell().then((bridge) =>
    bridge.listen("omni://event", ({ payload }) => {
      window.omniEvent(payload?.event, payload?.payload);
    })
  );
}

/** Subscribe to engine events ("engine-progress", "accounts-changed").
    Returns an unsubscribe function. */
export function onEngineEvent(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// ---- first-boot bootstrap (bake the runtime, fetch artifacts) ----

export function bootstrapStatus() {
  return api("bootstrap_status");
}

export function bootstrapStart() {
  return api("bootstrap_start");
}

/** Turn on Windows Hypervisor Platform (one UAC prompt, then a restart).
    Only reachable when the probe reported it explicitly off. */
export function enableVirtualization() {
  return api("enable_virtualization");
}

export function restartWindows() {
  return api("restart_windows");
}
