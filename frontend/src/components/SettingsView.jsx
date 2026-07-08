import { useEffect, useRef, useState } from "react";
import { UserIcon, SunIcon, MoonIcon } from "./icons.jsx";

function initials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean);
  const chars = parts.slice(0, 2).map((p) => p[0]);
  return (chars.join("") || "?").toUpperCase();
}

export default function SettingsView({ active, theme, onToggleTheme, profile, onProfile }) {
  const [name, setName] = useState(profile.name);
  const [tag, setTag] = useState(profile.tag);
  const [saved, setSaved] = useState(false);
  const saveTimer = useRef(null);
  const savedTimer = useRef(null);

  // Keep local fields in sync when settings finish loading.
  useEffect(() => {
    setName(profile.name);
    setTag(profile.tag);
  }, [profile.name, profile.tag]);

  const scheduleSave = (nextName, nextTag) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      onProfile({ name: nextName.trim(), tag: nextTag.trim() });
      setSaved(true);
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    }, 500);
  };

  return (
    <section className={`min-h-0 flex-1 overflow-y-auto ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-2xl flex-col gap-4">
        {/* Profile */}
        <div className="card overflow-hidden">
          <div
            className="flex items-center gap-2 border-b border-slate-200 px-4 py-3
                       transition-colors duration-300 dark:border-white/[.06]"
          >
            <UserIcon className="h-4 w-4 text-slate-400" />
            <h2 className="text-[13px] font-semibold">Profile</h2>
            <span
              className={`ml-auto text-[11px] font-medium text-emerald-500 transition-opacity duration-300 ${saved ? "opacity-100" : "opacity-0"}`}
            >
              Saved ✓
            </span>
          </div>
          <div className="flex items-start gap-5 p-5">
            <span
              className="flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br
                         from-indigo-500 to-fuchsia-500 text-xl font-bold text-white shadow-md"
            >
              {initials(name || "?")}
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium tracking-wider text-slate-400 uppercase dark:text-slate-500">
                  Display name
                </span>
                <input
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    scheduleSave(e.target.value, tag);
                  }}
                  className="input"
                  type="text"
                  maxLength={40}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium tracking-wider text-slate-400 uppercase dark:text-slate-500">
                  Status
                </span>
                <input
                  value={tag}
                  onChange={(e) => {
                    setTag(e.target.value);
                    scheduleSave(name, e.target.value);
                  }}
                  className="input"
                  type="text"
                  maxLength={60}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="e.g. Scripting away…"
                />
              </label>
            </div>
          </div>
        </div>

        {/* Appearance */}
        <div className="card flex items-center justify-between p-4">
          <div>
            <div className="text-[13px] font-semibold">Theme</div>
            <div className="text-[11.5px] text-slate-400 dark:text-slate-500">
              Switch between light and dark mode
            </div>
          </div>

          <button
            onClick={onToggleTheme}
            aria-label="Toggle dark / light mode"
            title="Toggle theme"
            className="relative h-8 w-[60px] shrink-0 rounded-full border border-slate-300 bg-white
                       transition-colors duration-300 outline-none focus-visible:ring-2
                       focus-visible:ring-indigo-400/60 dark:border-white/10 dark:bg-[#232636]"
          >
            <span
              key={theme} // re-mount to replay the pop animation on toggle
              className={`animate-pop absolute top-1 left-1 flex h-6 w-6 items-center justify-center
                          rounded-full bg-indigo-500 text-white shadow-md transition-transform
                          duration-300 ease-out ${theme === "dark" ? "translate-x-[28px]" : "translate-x-0"}`}
            >
              {theme === "dark" ? <MoonIcon className="h-3.5 w-3.5" /> : <SunIcon className="h-3.5 w-3.5" />}
            </span>
          </button>
        </div>

        <p className="text-center text-[11px] text-slate-400 dark:text-slate-600">Omni Executor · v2.0</p>
      </div>
    </section>
  );
}
