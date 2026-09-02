/* Left rail: identity and navigation.

   The rail is always compact — a fixed strip of icon tiles, never a list of
   labels. It carries its own ground (--color-rail) rather than sharing the
   canvas: a step up in value is what separates it, with the hairline spine
   only sharpening that edge. Each
   section is a rounded tile; the active one is a flat raised plate with a
   visible edge. Labels live in the tooltip (with the Ctrl+N hint) and in the
   context bar, which already names the section on every screen. Engine health
   is not repeated here: the context bar carries it on every screen that can
   act on it. */

import { useWindowDrag } from "./TitleBar.jsx";

export default function Sidebar({ nav, tab, onTab, chrome, premium = false }) {
  const mac = Boolean(chrome?.mac);
  // The identity block and the empty rail move the window, like a titlebar.
  const drag = useWindowDrag(chrome);

  return (
    <aside
      className={`spine-r flex shrink-0 flex-col bg-rail ${mac ? "w-[96px]" : "w-[90px]"}`}
    >
      {/* macOS: the native traffic lights float over this corner, so clear
          a strip for them (it doubles as extra drag surface). The rail is
          also a touch wider there so the lights never cross its edge. */}
      {mac && <div className="h-9 shrink-0" {...drag} />}

      {/* Identity — doubles as the window drag handle on this side. */}
      <div className="flex h-16 shrink-0 items-center justify-center" {...drag}>
        {/* The mark is the bare glyph on the rail — no tile behind it. The
            box keeps the same footprint so the drag surface and the nav
            column's rhythm don't move. */}
        <span
          className="flex h-11 w-11 shrink-0 items-center justify-center
                     font-mono text-[26px] leading-none font-bold text-ink"
          aria-hidden="true"
        >
          Ω
        </span>
      </div>

      <nav aria-label="Sections" className="flex flex-col items-center gap-2 px-2 py-2">
        {nav.map(({ id, label, Icon, premium: needsPremium }, i) => {
          const active = tab === id;
          // A locked section stays clickable — it explains itself when opened.
          // The pip is the only thing the rail says about it.
          const locked = Boolean(needsPremium) && !premium;
          return (
            <button
              key={id}
              onClick={() => onTab(id)}
              aria-current={active ? "page" : undefined}
              aria-label={label}
              title={`${label}${locked ? " — Premium" : ""}  (Ctrl+${i + 1})`}
              className={`ring-focus relative flex h-[54px] w-[54px] items-center justify-center rounded-[16px]
                          border transition-colors duration-150
                          ${
                            active
                              ? "border-line bg-raised text-ink"
                              : "border-transparent text-ink-3 hover:bg-raised/55 hover:text-ink"
                          }`}
            >
              <span className="relative shrink-0">
                <Icon className="h-[26px] w-[26px]" />
                {/* The label is in the tooltip, so the pip is the whole
                    signal — it rides the icon rather than the row. */}
                {locked && (
                  <span className="absolute -top-[2px] -right-[2px] h-[7px] w-[7px] rounded-full bg-premium" />
                )}
              </span>
            </button>
          );
        })}
      </nav>

      <div className="flex-1" {...drag} />
    </aside>
  );
}
