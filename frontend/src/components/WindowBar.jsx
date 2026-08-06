/* Slim strip above the content column: where you are, and window controls.
   It has no background of its own — it blends into the canvas so the app
   reads as one surface, not a taskbar. On macOS the native traffic lights
   (top-left, over the sidebar) do the window work, so no buttons render;
   Windows and Linux share the same minimise/maximise/close set on the
   right. In a plain browser the OS draws its own chrome, so nothing shows. */

import { useState } from "react";
import { api } from "../api.js";
import { MinusIcon, MaximizeIcon, RestoreIcon, CloseIcon } from "./icons.jsx";

export default function WindowBar({ title, subtitle, chrome }) {
  const [maximized, setMaximized] = useState(false);

  const toggleMaximize = async () => {
    const result = await api("toggle_maximize");
    if (typeof result === "boolean") setMaximized(result);
  };

  return (
    <header className="flex h-12 shrink-0 items-stretch">
      <div
        className="pywebview-drag-region flex min-w-0 flex-1 items-center gap-2.5 px-4"
        onDoubleClick={chrome.desktop ? toggleMaximize : undefined}
      >
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
        <div className="flex items-center gap-1 pr-2">
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
