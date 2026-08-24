/* The app's own titlebar, on the operating system's own window.

   VS Code / Discord style: a slim strip that blends into the sheet, with the
   section name and one fact about it. It is NOT a frameless hack — the window
   keeps its native frame (resize borders, snap, shadows, double-click), and
   the Python side only removes the OS caption (windowchrome.py). So every
   gesture here asks the OS to do the real thing:

     press on a drag surface  -> begin_window_drag     (native move loop)
     double-click             -> titlebar_double_click (OS caption rule)
     press on the top edge    -> begin_window_resize   (native size loop)

   Button placement follows each OS: minimise / maximise / close on the RIGHT
   on Windows and Linux; on macOS the native traffic lights sit top-LEFT over
   the sidebar, so nothing renders here. In a plain browser the OS draws its
   own chrome and none of this shows. */

import { useCallback, useEffect, useState } from "react";
import { api, onEngineEvent } from "../api.js";
import { MinusIcon, MaximizeIcon, RestoreIcon, CloseIcon } from "./icons.jsx";

const INTERACTIVE = "button, a, input, select, textarea, [role='button'], [data-no-drag]";

/** Mouse handlers that turn an element into a window drag surface. Put them
    on anything that should move the window: the titlebar, the sidebar's
    identity block, empty rail. Clicks on controls inside are left alone. */
export function useWindowDrag(chrome) {
  const onMouseDown = useCallback(
    (e) => {
      if (!chrome?.desktop || e.button !== 0 || e.detail > 1) return;
      if (e.target.closest(INTERACTIVE)) return;
      // The OS takes the mouse from here; stop the page from also selecting.
      e.preventDefault();
      api("begin_window_drag");
    },
    [chrome]
  );
  const onDoubleClick = useCallback(
    (e) => {
      if (!chrome?.desktop) return;
      if (e.target.closest(INTERACTIVE)) return;
      api("titlebar_double_click");
    },
    [chrome]
  );
  return chrome?.desktop ? { onMouseDown, onDoubleClick } : {};
}

/** Is the window maximised? Seeded from the backend, then kept current by the
    `window-state` event Python pushes on every OS maximise / restore, so a
    Win+Up or a drag to the top edge updates the glyph too. */
export function useMaximized(chrome) {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    if (!chrome?.desktop) return undefined;
    api("get_window_state").then((s) => {
      if (s && typeof s.maximized === "boolean") setMaximized(s.maximized);
    });
    return onEngineEvent((event, payload) => {
      if (event === "window-state" && typeof payload?.maximized === "boolean") {
        setMaximized(payload.maximized);
      }
    });
  }, [chrome]);
  return [maximized, setMaximized];
}

export default function TitleBar({ title, subtitle, chrome, leading = null }) {
  const drag = useWindowDrag(chrome);
  const [maximized, setMaximized] = useMaximized(chrome);

  const toggleMaximize = async () => {
    const result = await api("toggle_maximize");
    if (typeof result === "boolean") setMaximized(result);
  };

  return (
    <header className="flex h-12 shrink-0 items-stretch">
      <div className="app-drag flex min-w-0 flex-1 items-center gap-2.5 px-4" {...drag}>
        {/* macOS gate screens: leave room for the traffic lights, which
            otherwise sit over the sidebar's corner. */}
        {leading}
        <h1 className="silk truncate text-ink">{title}</h1>
        {subtitle && (
          <>
            <span className="text-ink-3" aria-hidden="true">
              ·
            </span>
            <span className="truncate text-[11.5px] text-ink-3">{subtitle}</span>
          </>
        )}
      </div>

      {chrome.desktop && !chrome.mac && (
        <div className="flex items-center pr-1.5" data-no-drag>
          <WinButton label="Minimise" onClick={() => api("minimize")}>
            <MinusIcon className="h-3.5 w-3.5" />
          </WinButton>
          <WinButton label={maximized ? "Restore" : "Maximise"} onClick={toggleMaximize}>
            {maximized ? <RestoreIcon className="h-3 w-3" /> : <MaximizeIcon className="h-3 w-3" />}
          </WinButton>
          <WinButton label="Close" danger onClick={() => api("close")}>
            <CloseIcon className="h-3.5 w-3.5" />
          </WinButton>
        </div>
      )}
    </header>
  );
}

function WinButton({ label, danger, children, ...rest }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={`ring-focus flex h-8 w-10 items-center justify-center rounded-lg text-ink-3
                  transition-colors duration-150
                  ${danger ? "hover:bg-danger hover:text-white" : "hover:bg-raised hover:text-ink"}`}
      {...rest}
    >
      {children}
    </button>
  );
}

/* Resize handles over the web content. The client area IS the whole window
   (Windows: windowchrome keeps it that way; Linux: the window is undecorated),
   so the OS has no border of its own to hit-test — these strips hand the
   press back to the OS's own size loop, which still does the resizing, the
   snapping and the cursor-capture. They disappear while maximised (a
   maximised window has no edges). macOS resizes from its own frame. */
const EDGE_PX = 5;
const CORNER_PX = 14;

export function ResizeEdges({ chrome }) {
  const [maximized] = useMaximized(chrome);
  if (!chrome?.desktop || chrome.mac || maximized) return null;

  const begin = (edge) => (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    api("begin_window_resize", edge);
  };
  const edge = (name, cursor, style) => (
    <div key={name} className={`resize-edge ${cursor}`} style={style} onMouseDown={begin(name)} />
  );

  return (
    <>
      {edge("top", "cursor-ns-resize", { top: 0, left: CORNER_PX, right: CORNER_PX, height: EDGE_PX })}
      {edge("top-left", "cursor-nwse-resize", { top: 0, left: 0, width: CORNER_PX, height: EDGE_PX })}
      {edge("top-right", "cursor-nesw-resize", { top: 0, right: 0, width: CORNER_PX, height: EDGE_PX })}
      {
        <>
          {edge("bottom", "cursor-ns-resize", { bottom: 0, left: CORNER_PX, right: CORNER_PX, height: EDGE_PX })}
          {edge("left", "cursor-ew-resize", { top: CORNER_PX, bottom: CORNER_PX, left: 0, width: EDGE_PX })}
          {edge("right", "cursor-ew-resize", { top: CORNER_PX, bottom: CORNER_PX, right: 0, width: EDGE_PX })}
          {edge("bottom-left", "cursor-nesw-resize", { bottom: 0, left: 0, width: CORNER_PX, height: EDGE_PX })}
          {edge("bottom-right", "cursor-nwse-resize", { bottom: 0, right: 0, width: CORNER_PX, height: EDGE_PX })}
        </>
      }
    </>
  );
}
