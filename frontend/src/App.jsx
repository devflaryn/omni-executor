import { useCallback, useEffect, useRef, useState } from "react";
import { api, bootstrapStatus, hasBackend, loadSettings, saveSettings } from "./api.js";
import { EngineProvider, useEngine } from "./engine.jsx";
import Sidebar from "./components/Sidebar.jsx";
import WindowBar from "./components/WindowBar.jsx";
import EditorView from "./components/EditorView.jsx";
import AccountsView from "./components/AccountsView.jsx";
import SettingsView from "./components/SettingsView.jsx";
import BootstrapView from "./components/BootstrapView.jsx";
import Toast from "./components/Toast.jsx";
import { CodeIcon, GearIcon, UsersIcon } from "./components/icons.jsx";

const NAV = [
  { id: "editor", label: "Editor", Icon: CodeIcon, hint: "1" },
  { id: "accounts", label: "Instances", Icon: UsersIcon, hint: "2" },
  { id: "settings", label: "Settings", Icon: GearIcon, hint: "3" },
];

const DEFAULT_LAUNCH = { mode: "playable", place: "", multiInstance: false };
const DEFAULT_PROFILE = { name: "Guest", tag: "" };

export default function App() {
  const [tab, setTab] = useState("editor");
  const [theme, setTheme] = useState("dark");
  const [compact, setCompact] = useState(false);
  const [profile, setProfile] = useState(DEFAULT_PROFILE);
  const [launch, setLaunch] = useState(DEFAULT_LAUNCH);
  const [toast, setToast] = useState(null);
  // Window chrome: nothing in a browser, native traffic lights on macOS,
  // our own buttons on Windows/Linux. Resolved once from the backend.
  const [chrome, setChrome] = useState({ desktop: false, mac: false });
  // First-boot gate: null = unknown (render nothing yet), false = show
  // BootstrapView, true = runtime ready (or no backend — dev/browser).
  const [ready, setReady] = useState(null);
  const toastTimer = useRef(null);

  useEffect(() => {
    hasBackend().then(async (desktop) => {
      if (!desktop) {
        setReady(true);
        return;
      }
      const platform = await api("get_platform");
      setChrome({ desktop: true, mac: platform === "darwin" });
      const s = await bootstrapStatus();
      setReady(Boolean(s?.ready));
    });
  }, []);

  const showToast = useCallback((message, tone = "info") => {
    setToast({ message, tone });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  }, []);

  // Restore persisted settings once on startup.
  useEffect(() => {
    loadSettings().then((settings) => {
      const nextTheme = settings.theme === "light" ? "light" : "dark";
      setTheme(nextTheme);
      document.documentElement.classList.toggle("light", nextTheme === "light");
      if (NAV.some((n) => n.id === settings.activeTab)) setTab(settings.activeTab);
      setCompact(settings.sidebar === "compact");
      if (settings.profile) setProfile({ ...DEFAULT_PROFILE, ...settings.profile });
      if (settings.launch) setLaunch({ ...DEFAULT_LAUNCH, ...settings.launch });
    });
  }, []);

  const switchTab = useCallback((id) => {
    setTab(id);
    saveSettings({ activeTab: id });
  }, []);

  const applyTheme = useCallback((next) => {
    setTheme(next);
    document.documentElement.classList.toggle("light", next === "light");
    saveSettings({ theme: next });
  }, []);

  const applyCompact = useCallback((next) => {
    setCompact(next);
    saveSettings({ sidebar: next ? "compact" : "expanded" });
  }, []);

  const updateProfile = useCallback((next) => {
    setProfile(next);
    saveSettings({ profile: next });
  }, []);

  const updateLaunch = useCallback((next) => {
    setLaunch(next);
    saveSettings({ launch: next });
  }, []);

  // Ctrl/Cmd+1..3 jumps between sections, as long as you aren't typing.
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const index = Number(e.key) - 1;
      if (NAV[index]) {
        e.preventDefault();
        switchTab(NAV[index].id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [switchTab]);

  if (ready === null) return null;
  if (ready === false) return <BootstrapView onReady={() => setReady(true)} />;

  return (
    <EngineProvider activeTab={tab} showToast={showToast}>
      <div className="flex h-screen overflow-hidden bg-canvas font-sans text-ink antialiased select-none">
        <Sidebar
          nav={NAV}
          tab={tab}
          onTab={switchTab}
          collapsed={compact}
          onCollapse={applyCompact}
          mac={chrome.mac}
        />

        <div className="flex min-w-0 flex-1 flex-col">
          <ContextBar tab={tab} profile={profile} chrome={chrome} />

          {/* Every view stays mounted so the editor keeps its buffer and the
              instances list keeps its selection; only visibility changes. */}
          <main className="flex min-h-0 flex-1 flex-col">
            <EditorView active={tab === "editor"} showToast={showToast} />
            <AccountsView
              active={tab === "accounts"}
              launch={launch}
              onLaunch={updateLaunch}
              showToast={showToast}
            />
            <SettingsView
              active={tab === "settings"}
              theme={theme}
              onTheme={applyTheme}
              compact={compact}
              onCompact={applyCompact}
              profile={profile}
              onProfile={updateProfile}
              showToast={showToast}
            />
          </main>
        </div>

        <Toast toast={toast} />
      </div>
    </EngineProvider>
  );
}

/** The strip reports what you're looking at and one true fact about it. */
function ContextBar({ tab, profile, chrome }) {
  const { accounts, running, health } = useEngine();

  const nav = NAV.find((n) => n.id === tab);
  const subtitle =
    tab === "accounts"
      ? `${accounts.length} ${accounts.length === 1 ? "instance" : "instances"}${
          running.length ? ` · ${running.length} running` : ""
        }`
      : tab === "editor"
        ? `Engine ${health.label.toLowerCase()}`
        : profile.name || "Guest";

  return <WindowBar title={nav?.label ?? "Omni Executor"} subtitle={subtitle} chrome={chrome} />;
}
