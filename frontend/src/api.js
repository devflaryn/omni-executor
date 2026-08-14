/* Bridge to the Python backend (window.pywebview.api) plus the engine event
   bus (main.py pushes events by calling window.omniEvent). */

const pywebviewReady = new Promise((resolve) => {
  if (window.pywebview) return resolve();
  window.addEventListener("pywebviewready", () => resolve(), { once: true });
  setTimeout(resolve, 1500); // browser fallback: don't wait forever
});

export async function hasBackend() {
  await pywebviewReady;
  return Boolean(window.pywebview?.api);
}

/** Call a backend method; engine-style calls always resolve to an object
    with an `ok` flag so callers never need try/catch. */
export async function api(method, ...args) {
  await pywebviewReady;
  const bridge = window.pywebview?.api;
  if (!bridge || typeof bridge[method] !== "function") {
    return {
      ok: false,
      error: "no_backend",
      message: "Engine unavailable — run the desktop app (python main.py).",
    };
  }
  try {
    return await bridge[method](...args);
  } catch (err) {
    return { ok: false, error: "bridge_error", message: String(err?.message || err) };
  }
}

// ---- settings (Python persists to a JSON file; localStorage in a browser) ----

export async function loadSettings() {
  await pywebviewReady;
  try {
    if (window.pywebview?.api) return await window.pywebview.api.get_settings();
    return JSON.parse(localStorage.getItem("omni-settings")) || {};
  } catch {
    return {};
  }
}

export function saveSettings(patch) {
  pywebviewReady.then(async () => {
    try {
      if (window.pywebview?.api) {
        await window.pywebview.api.save_settings(patch);
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
