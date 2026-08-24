import { useCallback, useEffect, useRef, useState } from "react";
import { api, bootstrapStatus, hasBackend, loadSettings, saveSettings } from "./api.js";
import { EngineProvider, useEngine } from "./engine.jsx";
import { EditorStoreProvider } from "./editorStore.jsx";
import Sidebar from "./components/Sidebar.jsx";
import TitleBar, { ResizeEdges } from "./components/TitleBar.jsx";
import HomeView from "./components/HomeView.jsx";
import EditorView from "./components/EditorView.jsx";
import AccountsView from "./components/AccountsView.jsx";
import SettingsView from "./components/SettingsView.jsx";
import BootstrapView from "./components/BootstrapView.jsx";
import AuthView from "./components/AuthView.jsx";
import { UpdateBanner, useUpdates } from "./components/UpdateBanner.jsx";
import Toast from "./components/Toast.jsx";
import { CodeIcon, GearIcon, HomeIcon, UsersIcon } from "./components/icons.jsx";

const NAV = [
  { id: "home", label: "Home", Icon: HomeIcon, hint: "1" },
  { id: "editor", label: "Editor", Icon: CodeIcon, hint: "2" },
  { id: "accounts", label: "Instances", Icon: UsersIcon, hint: "3" },
  { id: "settings", label: "Settings", Icon: GearIcon, hint: "4" },
];

const DEFAULT_LAUNCH = { mode: "gaming", gpu: "auto", place: "", multiInstance: false };
const DEFAULT_PROFILE = { name: "Guest", tag: "" };

export default function App() {
  const [tab, setTab] = useState("home");
  // Home's "Add account" opens the Instances tab's dialog: a counter, so each
  // press opens it again even if the last one was dismissed.
  const [addRequest, setAddRequest] = useState(0);
  const [theme, setTheme] = useState("dark");
  const [compact, setCompact] = useState(false);
  const [profile, setProfile] = useState(DEFAULT_PROFILE);
  const [launch, setLaunch] = useState(DEFAULT_LAUNCH);
  const [toast, setToast] = useState(null);
  // Window chrome: nothing in a browser, native traffic lights on macOS,
  // our own buttons on Windows/Linux. Resolved once from the backend.
  const [chrome, setChrome] = useState({ desktop: false, mac: false, platform: "browser" });
  // First-boot gate: null = unknown (render nothing yet), false = show
  // BootstrapView, true = runtime ready (or no backend — dev/browser).
  const [ready, setReady] = useState(null);
  // Sign-in gate: null = still asking the backend, otherwise the auth_status
  // report. Accounts and script execution belong to an Omni user, so the shell
  // does not mount until one is signed in.
  const [auth, setAuth] = useState(null);
  const toastTimer = useRef(null);
  // Launch-time update state. main.py pushes an "update-status" event on every
  // start, so this is populated without the UI asking.
  const updates = useUpdates();

  const refreshAuth = useCallback(async () => {
    const status = await api("auth_status");
    setAuth(status?.ok ? status : { ok: true, signedIn: false });
    return status;
  }, []);

  useEffect(() => {
    hasBackend().then(async (desktop) => {
      if (!desktop) {
        // Browser preview: there is no Python side to hold a session, so the
        // gate would be unpassable. Skip it rather than show a dead form.
        setAuth({ ok: true, signedIn: true, preview: true });
        setReady(true);
        return;
      }
      const platform = await api("get_platform");
      setChrome({
        desktop: true,
        mac: platform === "darwin",
        platform: platform === "darwin" ? "mac" : platform === "win32" ? "windows" : "linux",
      });
      await refreshAuth();
      const s = await bootstrapStatus();
      setReady(Boolean(s?.ready));
    });
  }, [refreshAuth]);

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

  // Ctrl/Cmd+1..4 jumps between sections, as long as you aren't typing.
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

  if (ready === null || auth === null) return null;
  // Sign-in comes BEFORE the runtime download: the multi-gigabyte base images
  // are worth fetching only for someone who can actually use them, and the
  // license check is the cheap question to ask first. Both gates sit under
  // the same titlebar as the app: the window must be movable and closable
  // before anyone has signed in.
  const gate = !auth.signedIn ? (
    <AuthView apiBase={auth.apiBase} deviceName={auth.device?.name} onSignedIn={() => refreshAuth()} />
  ) : ready === false ? (
    <BootstrapView onReady={() => setReady(true)} />
  ) : null;
  if (gate) {
    return (
      <div className="flex h-screen flex-col overflow-hidden bg-canvas font-sans text-ink antialiased select-none">
        <ResizeEdges chrome={chrome} />
        <TitleBar
          title="Omni Executor"
          subtitle={!auth.signedIn ? "Sign in" : "Setup"}
          chrome={chrome}
          leading={chrome.mac ? <span className="w-[64px]" aria-hidden="true" /> : null}
        />
        <div className="flex min-h-0 flex-1 flex-col">{gate}</div>
      </div>
    );
  }

  return (
    <EngineProvider activeTab={tab} showToast={showToast}>
      <EditorStoreProvider>
        <div className="flex h-screen overflow-hidden bg-canvas font-sans text-ink antialiased select-none">
          <ResizeEdges chrome={chrome} />
          <Sidebar
            nav={NAV}
            tab={tab}
            onTab={switchTab}
            collapsed={compact}
            onCollapse={applyCompact}
            chrome={chrome}
          />

          <div className="flex min-w-0 flex-1 flex-col">
            <ContextBar tab={tab} profile={profile} chrome={chrome} />
            <UpdateBanner updates={updates} onOpenSettings={() => switchTab("settings")} />

            {/* Every view stays mounted so the editor keeps its buffer and the
                instances list keeps its selection; only visibility changes. */}
            <main className="flex min-h-0 flex-1 flex-col">
              <HomeView
                active={tab === "home"}
                profile={profile}
                launch={launch}
                onGo={switchTab}
                onAddAccount={() => {
                  switchTab("accounts");
                  setAddRequest((n) => n + 1);
                }}
                showToast={showToast}
              />
              <EditorView active={tab === "editor"} showToast={showToast} />
              <AccountsView
                active={tab === "accounts"}
                launch={launch}
                onLaunch={updateLaunch}
                showToast={showToast}
                addRequest={addRequest}
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
                auth={auth}
                onAuthChange={refreshAuth}
                updates={updates}
              />
            </main>
          </div>

          <Toast toast={toast} />
        </div>
      </EditorStoreProvider>
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
        : tab === "home"
          ? running.length
            ? `${running.length} running`
            : `Engine ${health.label.toLowerCase()}`
          : profile.name || "Guest";

  return <TitleBar title={nav?.label ?? "Omni Executor"} subtitle={subtitle} chrome={chrome} />;
}
