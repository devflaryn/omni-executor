import { useCallback, useEffect, useRef, useState } from "react";
import { loadSettings, saveSettings } from "./api.js";
import TitleBar from "./components/TitleBar.jsx";
import EditorView from "./components/EditorView.jsx";
import AccountsView from "./components/AccountsView.jsx";
import SettingsView from "./components/SettingsView.jsx";
import Toast from "./components/Toast.jsx";

const DEFAULT_LAUNCH = { mode: "playable", multiInstance: false, minimizeOnLaunch: false };

export default function App() {
  const [tab, setTab] = useState("editor");
  const [theme, setTheme] = useState("dark");
  const [profile, setProfile] = useState({ name: "Guest", tag: "" });
  const [launchOpts, setLaunchOpts] = useState(DEFAULT_LAUNCH);
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);

  const showToast = useCallback((message) => {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2200);
  }, []);

  // Load persisted settings once on startup.
  useEffect(() => {
    loadSettings().then((settings) => {
      const loadedTheme = settings.theme === "light" ? "light" : "dark";
      setTheme(loadedTheme);
      document.documentElement.classList.toggle("dark", loadedTheme === "dark");
      if (["editor", "accounts", "settings"].includes(settings.activeTab)) {
        setTab(settings.activeTab);
      }
      if (settings.profile) setProfile({ ...{ name: "Guest", tag: "" }, ...settings.profile });
      if (settings.launch) setLaunchOpts({ ...DEFAULT_LAUNCH, ...settings.launch });
    });
  }, []);

  const switchTab = useCallback((name) => {
    setTab(name);
    saveSettings({ activeTab: name });
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.classList.toggle("dark", next === "dark");
      saveSettings({ theme: next });
      return next;
    });
  }, []);

  const updateProfile = useCallback((next) => {
    setProfile(next);
    saveSettings({ profile: next });
  }, []);

  const updateLaunchOpts = useCallback((next) => {
    setLaunchOpts(next);
    saveSettings({ launch: next });
  }, []);

  return (
    <div
      className="flex h-screen flex-col overflow-hidden bg-[#eef0f6] font-sans text-slate-800
                 antialiased transition-colors duration-300 select-none
                 dark:bg-[#14151d] dark:text-slate-200"
    >
      <TitleBar tab={tab} onTab={switchTab} />

      <div className="flex min-h-0 flex-1 flex-col p-5">
        {/* All views stay mounted so the editor keeps its buffer and the
            accounts view keeps its state; only visibility changes. */}
        <EditorView active={tab === "editor"} showToast={showToast} />
        <AccountsView
          active={tab === "accounts"}
          showToast={showToast}
          launchOpts={launchOpts}
          onLaunchOpts={updateLaunchOpts}
        />
        <SettingsView
          active={tab === "settings"}
          theme={theme}
          onToggleTheme={toggleTheme}
          profile={profile}
          onProfile={updateProfile}
        />
      </div>

      <Toast message={toast} />
    </div>
  );
}
