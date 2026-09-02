/* The window itself: drag, resize, minimise, maximise, close.

   These used to be Python methods (`api("begin_window_drag")` and friends)
   that reached down into Win32, AppKit or GTK. Tauri does all of it, so this
   module is a thin, lazily-loaded wrapper over its window API — and the Python
   backend no longer knows the window exists.

   Every call is a no-op outside the shell, so a browser preview (`npm run dev`)
   renders the same titlebar without throwing. */

const TAURI = "__TAURI_INTERNALS__";

export function inShell() {
  return typeof window !== "undefined" && Boolean(window[TAURI]);
}

let cached = null;
async function tauri() {
  if (!inShell()) return null;
  if (!cached) {
    const [win, core] = await Promise.all([
      import("@tauri-apps/api/window"),
      import("@tauri-apps/api/core"),
    ]);
    cached = { current: win.getCurrentWindow(), ResizeDirection: win.ResizeDirection, invoke: core.invoke };
  }
  return cached;
}

/* Our edge names (TitleBar's resize strips) -> Tauri's compass directions. */
const DIRECTIONS = {
  top: "North",
  bottom: "South",
  left: "West",
  right: "East",
  "top-left": "NorthWest",
  "top-right": "NorthEast",
  "bottom-left": "SouthWest",
  "bottom-right": "SouthEast",
};

/** Hand the press to the OS's own move loop, so snapping, drag-to-top and the
    window manager's gestures all still apply. */
export async function startDrag() {
  const t = await tauri();
  await t?.current.startDragging();
}

/** Hand the press to the OS's own size loop from one edge or corner. */
export async function startResize(edge) {
  const direction = DIRECTIONS[edge];
  if (!direction) return;
  const t = await tauri();
  await t?.current.startResizeDragging(direction);
}

export async function minimize() {
  const t = await tauri();
  await t?.current.minimize();
}

/** Toggle maximise; resolves to the resulting state. */
export async function toggleMaximize() {
  const t = await tauri();
  if (!t) return false;
  await t.current.toggleMaximize();
  return t.current.isMaximized();
}

export async function close() {
  const t = await tauri();
  await t?.current.close();
}

export async function isMaximized() {
  const t = await tauri();
  return t ? t.current.isMaximized() : false;
}

/** Watch the maximised state. The OS changes it without asking us — Win+Up, a
    drag to the top edge, a double-click on the titlebar — and the titlebar's
    glyph and the sheet's corner radius both follow it, so this listens to the
    window rather than to our own button.

    Returns a promise for an unsubscribe function. */
export function onMaximizeChange(handler) {
  const stop = tauri().then(async (t) => {
    if (!t) return () => {};
    handler(await t.current.isMaximized());
    return t.current.onResized(async () => handler(await t.current.isMaximized()));
  });
  return () => stop.then((off) => off());
}

/** Tell the shell the page has painted, so it can reveal the window. Until
    this lands the window is hidden, which is what keeps a transparent
    unpainted rectangle off the screen at launch. */
export async function signalReady() {
  const t = await tauri();
  await t?.invoke("app_ready");
}
