/* The app's own titlebar, on a frameless window.

   VS Code / Discord style: a slim strip that blends into the sheet, with the
   section name and one fact about it. The window has no OS caption on any
   platform, so every gesture here asks the OS to do the real thing through
   Tauri:

     press on a drag surface  -> startDragging       (native move loop)
     double-click             -> toggleMaximize      (the OS caption rule)
     press on a resize strip  -> startResizeDragging (native size loop)

   Button placement follows each OS: minimise / maximise / close on the RIGHT
   on Windows and Linux; on macOS the native traffic lights sit top-LEFT over
   the sidebar (the window is `titleBarStyle: Overlay` with a hidden title), so
   nothing renders here. In a plain browser none of this shows. */

import { useCallback, useEffect, useState } from "react";
import * as win from "../window.js";
import { MinusIcon, MaximizeIcon, RestoreIcon, CloseIcon } from "./icons.jsx";

const INTERACTIVE = "button, a, input, select, textarea, [role='button'], [data-no-drag]";

/** Mouse handlers that turn an element into a window drag surface.

    ONLY the titlebar gets these. The rest of the window — the sidebar body,
    the content, every panel — is inert to dragging on purpose: a window you
    can throw across the desktop by grabbing a list row is a window that fights
    you. Clicks on controls inside a drag surface are left alone. */
export function useWindowDrag(chrome) {
  const onMouseDown = useCallback(
    (e) => {
      if (!chrome?.desktop || e.button !== 0 || e.detail > 1) return;
      if (e.target.closest(INTERACTIVE)) return;
      // The OS takes the mouse from here; stop the page from also selecting.
      e.preventDefault();
      win.startDrag();
    },
    [chrome]
  );
  const onDoubleClick = useCallback(
    (e) => {
      if (!chrome?.desktop) return;
      if (e.target.closest(INTERACTIVE)) return;
      win.toggleMaximize();
    },
    [chrome]
  );
  return chrome?.desktop ? { onMouseDown, onDoubleClick } : {};
}

/** Is the window maximised? Seeded from the window and kept current by its own
    resize events, so a Win+Up or a drag to the top edge updates the glyph and
    the sheet's corner radius too. */
export function useMaximized(chrome) {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    if (!chrome?.desktop) return undefined;
    return win.onMaximizeChange(setMaximized);
  }, [chrome]);
  return [maximized, setMaximized];
}

/** The window's outermost surface: a sheet that draws the app's rounded edge
    and clips its content to it. The pixels outside that arc are left unpainted
    so the transparent window shows nothing there — on Windows, chrome.rs cuts
    the same radius out of the HWND (see the .window-shell comment in
    styles.css). Maximised drops the radius, because every OS squares a
    maximised window. The resize strips anchor to this. */
export function WindowShell({ chrome, children }) {
  const [maximized] = useMaximized(chrome);
  return (
    <div className={`window-shell${maximized ? " is-maximized" : ""}`}>
      <div className="window-sheet">{children}</div>
    </div>
  );
}

export default function TitleBar({ title, subtitle, chrome, leading = null }) {
  const drag = useWindowDrag(chrome);
  const [maximized, setMaximized] = useMaximized(chrome);

  const toggleMaximize = async () => {
    setMaximized(await win.toggleMaximize());
  };

  return (
    <header className="flex h-14 shrink-0 items-stretch">
      <div className="app-drag flex min-w-0 flex-1 items-center gap-2.5 px-5" {...drag}>
        {/* macOS: leave room for the traffic lights, which the OS draws over
            the sidebar's corner. */}
        {leading}
        {/* Read as a breadcrumb: the section, a separator, then the one fact
            about it. Both sit at reading size — the strip names where you are,
            and a name you have to squint at is not doing that. */}
        <h1 className="truncate text-[14px] font-semibold tracking-[-0.01em] text-ink">{title}</h1>
        {subtitle && (
          <>
            <span className="text-ink-3/70" aria-hidden="true">
              ·
            </span>
            <span className="truncate text-[13px] text-ink-3">{subtitle}</span>
          </>
        )}
      </div>

      {chrome.desktop && !chrome.mac && (
        <div className="flex items-center pr-1.5" data-no-drag>
          <WinButton label="Minimise" onClick={() => win.minimize()}>
            <MinusIcon className="h-3.5 w-3.5" />
          </WinButton>
          <WinButton label={maximized ? "Restore" : "Maximise"} onClick={toggleMaximize}>
            {maximized ? <RestoreIcon className="h-3 w-3" /> : <MaximizeIcon className="h-3 w-3" />}
          </WinButton>
          <WinButton label="Close" danger onClick={() => win.close()}>
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
      className={`ring-focus flex h-8 w-10 items-center justify-center rounded-[10px] text-ink-3
                  transition-colors duration-150
                  ${danger ? "hover:bg-danger hover:text-white" : "hover:bg-raised hover:text-ink"}`}
      {...rest}
    >
      {children}
    </button>
  );
}

/* Resize handles over the web content. The window is undecorated on Windows
   and Linux, so the client area IS the whole window and the OS has no border
   of its own to hit-test — these strips hand the press back to its own size
   loop, which still does the resizing, the snapping and the cursor capture.
   They disappear while maximised (a maximised window has no edges). macOS
   keeps a real frame and resizes from it.

   THE NUMBERS FOLLOW THE CORNER RADIUS, and that is not cosmetic. A 33px
   round-rect is cut out of the window on Windows, so a small square handle
   pinned to the very corner would sit ENTIRELY in the clipped-away notch and
   never receive a press — corner resize would look implemented and be dead.
   Each corner is therefore an L of two arms as long as the radius, thick
   enough (12px) that the arc's 45-degree bulge — which is ~9.7px in from the
   corner on both axes — falls inside them. */
const EDGE_PX = 5;
const CORNER_LEN = 33; // = --window-radius / chrome.rs CORNER_RADIUS
const CORNER_THICK = 12;

export function ResizeEdges({ chrome }) {
  const [maximized] = useMaximized(chrome);
  if (!chrome?.desktop || chrome.mac || maximized) return null;

  const begin = (edge) => (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    win.startResize(edge);
  };
  const strip = (key, edge, cursor, style) => (
    <div
      key={key}
      className={`resize-edge ${cursor}`}
      style={style}
      onMouseDown={begin(edge)}
    />
  );
  /* Two arms per corner, tracing the arc rather than boxing it in. */
  const corner = (edge, cursor, { top, bottom, left, right }) => {
    const v = top !== undefined ? { top: 0 } : { bottom: 0 };
    const h = left !== undefined ? { left: 0 } : { right: 0 };
    return [
      strip(`${edge}-h`, edge, cursor, { ...v, ...h, width: CORNER_LEN, height: CORNER_THICK }),
      strip(`${edge}-v`, edge, cursor, { ...v, ...h, width: CORNER_THICK, height: CORNER_LEN }),
    ];
  };

  return (
    <>
      {strip("top", "top", "cursor-ns-resize", {
        top: 0, left: CORNER_LEN, right: CORNER_LEN, height: EDGE_PX,
      })}
      {strip("bottom", "bottom", "cursor-ns-resize", {
        bottom: 0, left: CORNER_LEN, right: CORNER_LEN, height: EDGE_PX,
      })}
      {strip("left", "left", "cursor-ew-resize", {
        left: 0, top: CORNER_LEN, bottom: CORNER_LEN, width: EDGE_PX,
      })}
      {strip("right", "right", "cursor-ew-resize", {
        right: 0, top: CORNER_LEN, bottom: CORNER_LEN, width: EDGE_PX,
      })}
      {corner("top-left", "cursor-nwse-resize", { top: 0, left: 0 })}
      {corner("top-right", "cursor-nesw-resize", { top: 0, right: 0 })}
      {corner("bottom-left", "cursor-nesw-resize", { bottom: 0, left: 0 })}
      {corner("bottom-right", "cursor-nwse-resize", { bottom: 0, right: 0 })}
    </>
  );
}
