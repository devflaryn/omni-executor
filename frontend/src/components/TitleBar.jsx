import { useEffect, useState } from "react";
import { api, hasBackend } from "../api.js";
import { CodeIcon, UsersIcon, GearIcon, MinusIcon, MaximizeIcon, RestoreIcon, CloseIcon } from "./icons.jsx";

const TABS = [
  { id: "editor", label: "Editor", Icon: CodeIcon },
  { id: "accounts", label: "Accounts", Icon: UsersIcon },
  { id: "settings", label: "Settings", Icon: GearIcon },
];

export default function TitleBar({ tab, onTab }) {
  const [maximized, setMaximized] = useState(false);
  const [desktop, setDesktop] = useState(true);

  // In a plain browser there is native window chrome already — hide ours.
  useEffect(() => {
    hasBackend().then(setDesktop);
  }, []);

  const toggleMaximize = async () => {
    const result = await api("toggle_maximize");
    if (typeof result === "boolean") setMaximized(result);
  };

  return (
    <header
      className="animate-fadein relative z-10 flex h-11 shrink-0 items-stretch border-b border-slate-200
                 bg-white/80 transition-colors duration-300 dark:border-white/[.06] dark:bg-[#181a24]"
    >
      {/* Brand (draggable) */}
      <div
        className="pywebview-drag-region flex items-center gap-2.5 pr-5 pl-4"
        onDoubleClick={toggleMaximize}
      >
        <span className="h-2.5 w-2.5 rounded-full bg-indigo-500 dark:bg-indigo-400" />
        <h1 className="text-[13px] font-semibold tracking-wide whitespace-nowrap">Omni Executor</h1>
        <span
          className="rounded-full border border-slate-300 px-2 py-0.5 text-[9.5px] font-medium tracking-wider
                     text-slate-500 uppercase transition-colors duration-300 dark:border-white/10 dark:text-slate-400"
        >
          Lua
        </span>
      </div>

      {/* Tabs */}
      <nav className="flex items-center gap-1 py-1.5">
        {TABS.map(({ id, label, Icon }) => (
          <button
            key={id}
            onClick={() => onTab(id)}
            className={`tab-btn ${
              tab === id
                ? "bg-indigo-500/10 text-indigo-600 dark:bg-indigo-400/15 dark:text-indigo-300"
                : "text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-white/[.06] dark:hover:text-slate-200"
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </nav>

      {/* Drag spacer */}
      <div className="pywebview-drag-region flex-1" onDoubleClick={toggleMaximize} />

      {/* Window controls */}
      {desktop && (
        <div className="flex items-stretch">
          <button
            onClick={() => api("minimize")}
            title="Minimize"
            className="win-btn hover:bg-slate-200/70 hover:text-slate-800 dark:hover:bg-white/[.08] dark:hover:text-slate-100"
          >
            <MinusIcon className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={toggleMaximize}
            title={maximized ? "Restore" : "Maximize"}
            className="win-btn hover:bg-slate-200/70 hover:text-slate-800 dark:hover:bg-white/[.08] dark:hover:text-slate-100"
          >
            {maximized ? <RestoreIcon className="h-3 w-3" /> : <MaximizeIcon className="h-3 w-3" />}
          </button>
          <button
            onClick={() => api("close")}
            title="Close"
            className="win-btn hover:bg-red-500 hover:text-white"
          >
            <CloseIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </header>
  );
}
