/* Left rail: identity, navigation, and a permanent engine readout.

   The rail has no background of its own — it sits on the same sheet as the
   rest of the app, separated only by a floating spine that fades out before
   it reaches the top or bottom edge. It collapses to icons only; the engine
   lamp survives the collapse because "is the engine alive" is the one thing
   you should never have to open a panel to find out. */

import { useEngine } from "../engine.jsx";
import { Lamp } from "./ui.jsx";
import { useWindowDrag } from "./TitleBar.jsx";
import { ChevronLeftIcon, ChevronRightIcon } from "./icons.jsx";

export default function Sidebar({ nav, tab, onTab, collapsed, onCollapse, chrome }) {
  const { health } = useEngine();
  const mac = Boolean(chrome?.mac);
  // The identity block and the empty rail move the window, like a titlebar.
  const drag = useWindowDrag(chrome);

  return (
    <aside
      className={`spine-r flex shrink-0 flex-col transition-[width] duration-200
                  ${collapsed ? (mac ? "w-[76px]" : "w-[62px]") : "w-[204px]"}`}
    >
      {/* macOS: the native traffic lights float over this corner, so clear
          a strip for them (it doubles as extra drag surface). The collapsed
          rail is also a touch wider there so the lights never cross its edge. */}
      {mac && <div className="h-9 shrink-0" {...drag} />}

      {/* Identity — doubles as the window drag handle on this side. */}
      <div
        className={`flex h-12 shrink-0 items-center gap-2.5 ${
          collapsed ? "justify-center px-0" : "px-4"
        }`}
        {...drag}
      >
        <span
          className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[7px] bg-accent
                     font-mono text-[14px] leading-none font-bold text-accent-ink"
          aria-hidden="true"
        >
          Ω
        </span>
        {!collapsed && (
          <span className="min-w-0 leading-none">
            <span className="silk block text-[11px] tracking-[0.2em] text-ink">Omni</span>
            <span className="silk mt-[3px] block text-[8.5px] tracking-[0.3em] text-ink-3">
              Executor
            </span>
          </span>
        )}
      </div>

      <nav
        aria-label="Sections"
        className={`flex flex-col gap-1 py-2 ${collapsed ? "items-center px-2" : "pr-2.5 pl-3.5"}`}
      >
        {nav.map(({ id, label, Icon, hint }, i) => {
          const active = tab === id;
          return (
            <button
              key={id}
              onClick={() => onTab(id)}
              aria-current={active ? "page" : undefined}
              title={collapsed ? `${label}  (Ctrl+${i + 1})` : undefined}
              className={`ring-focus group relative flex h-9 items-center rounded-lg text-[12.5px]
                          font-medium transition-colors duration-150
                          ${collapsed ? "w-9 justify-center" : "gap-2.5 px-2.5"}
                          ${
                            active
                              ? "bg-accent/12 text-ink"
                              : "text-ink-2 hover:bg-raised hover:text-ink"
                          }`}
            >
              {active && !collapsed && (
                <span className="absolute top-1/2 -left-[11px] h-[18px] w-[3px] -translate-y-1/2 rounded-full bg-accent" />
              )}
              <Icon className={`h-4 w-4 shrink-0 ${active ? "text-accent" : ""}`} />
              {!collapsed && (
                <>
                  <span className="truncate">{label}</span>
                  {hint && (
                    <span className="silk ml-auto text-[9px] text-ink-3 opacity-0 transition-opacity group-hover:opacity-100">
                      {hint}
                    </span>
                  )}
                </>
              )}
            </button>
          );
        })}
      </nav>

      <div className="flex-1" {...drag} />

      {/* Engine readout */}
      <div className={`rule-t ${collapsed ? "px-2 py-3" : "px-3.5 py-3"}`}>
        <div
          className={`flex items-center gap-2.5 ${collapsed ? "justify-center" : ""}`}
          title={collapsed ? `Engine: ${health.label}` : undefined}
        >
          <Lamp tone={health.tone} pulse={health.tone === "busy"} />
          {!collapsed && (
            <span className="min-w-0 leading-none">
              <span className="silk block text-[8.5px] text-ink-3">Engine</span>
              <span className="mt-[3px] block truncate text-[11.5px] leading-[1.3] text-ink-2">{health.label}</span>
            </span>
          )}
        </div>
      </div>

      <div className={`rule-t ${collapsed ? "px-2 py-2" : "px-2.5 py-2"}`}>
        <button
          onClick={() => onCollapse(!collapsed)}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={`ring-focus flex h-8 items-center rounded-lg text-ink-3 transition-colors
                      duration-150 hover:bg-raised hover:text-ink
                      ${collapsed ? "w-full justify-center" : "w-full gap-2.5 px-2.5"}`}
        >
          {collapsed ? (
            <ChevronRightIcon className="h-4 w-4" />
          ) : (
            <>
              <ChevronLeftIcon className="h-4 w-4" />
              <span className="text-[12px]">Collapse</span>
            </>
          )}
        </button>
      </div>
    </aside>
  );
}
