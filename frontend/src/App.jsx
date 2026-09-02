import { useCallback, useEffect, useRef, useState } from "react";
import { api, bootstrapStatus, hasBackend, loadSettings, saveSettings } from "./api.js";
import { EngineProvider, useEngine } from "./engine.jsx";
import { EditorStoreProvider } from "./editorStore.jsx";
import Sidebar from "./components/Sidebar.jsx";
import TitleBar, { ResizeEdges, WindowShell } from "./components/TitleBar.jsx";
import { signalReady } from "./window.js";
import HomeView from "./components/HomeView.jsx";
import EditorView from "./components/EditorView.jsx";
import AccountsView from "./components/AccountsView.jsx";
import SettingsView from "./components/SettingsView.jsx";
import FarmingView from "./components/FarmingView.jsx";
import StatTrackView from "./components/StatTrackView.jsx";
import NetworkView from "./components/NetworkView.jsx";
import BootstrapView from "./components/BootstrapView.jsx";
import AuthView from "./components/AuthView.jsx";
import { UpdateBanner, UpdateModal, useUpdates } from "./components/UpdateBanner.jsx";
import Toast from "./components/Toast.jsx";
import {
  ChartDuoIcon,
  CodeDuoIcon,
  GearDuoIcon,
  GridDuoIcon,
  HomeDuoIcon,
  SignalDuoIcon,
  UsersDuoIcon,
} from "./components/icons.jsx";

// `premium: true` marks a section the free tier cannot use. The rail renders a
// gold pip on it and the section itself shows its own locked state — it is NOT
// hidden, because a tab nobody can see cannot explain what a plan buys.
const NAV = [
  { id: "home", label: "Home", Icon: HomeDuoIcon, hint: "1" },
  { id: "editor", label: "Editor", Icon: CodeDuoIcon, hint: "2" },
  { id: "accounts", label: "Accounts", Icon: UsersDuoIcon, hint: "3" },
  { id: "farming", label: "Farming", Icon: GridDuoIcon, hint: "4", premium: true },
  // Stat Track sits next to Farming because it answers Farming's question —
  // "is the fleet actually earning anything" — and the two are read together.
  { id: "stattrack", label: "Stat Track", Icon: ChartDuoIcon, hint: "5", premium: true },
  // Network sits before Settings, not inside it: what it reports is a live
  // condition of the machine — the same kind of thing as "3 running" — and
  // the proxy it owns is a knob you set while watching that reading, not a
  // preference you file away next to the theme.
  { id: "network", label: "Network", Icon: SignalDuoIcon, hint: "6" },
  { id: "settings", label: "Settings", Icon: GearDuoIcon, hint: "7" },
];

const DEFAULT_LAUNCH = { mode: "gaming", gpu: "auto", place: "" };
const DEFAULT_PROFILE = { name: "Guest", tag: "" };

export default function App() {
  const [tab, setTab] = useState("home");
  // Home's "Add account" opens the Accounts tab's dialog: a counter, so each
  // press opens it again even if the last one was dismissed.
  const [addRequest, setAddRequest] = useState(0);
  const [theme, setTheme] = useState("dark");
  const [profile, setProfile] = useState(DEFAULT_PROFILE);
  const [launch, setLaunch] = useState(DEFAULT_LAUNCH);
  const [toast, setToast] = useState(null);
  // The Network tab's own verdict, reported upward so the context bar can
  // carry it. Nothing else reads it: the bar states one true fact per tab,
  // and for Network the only true fact is the last measurement.
  const [netSummary, setNetSummary] = useState(null);
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
  // The staged-update version the user said "Later" to this session, so the
  // startup popup does not reappear on every tick after it is dismissed.
  const [updateDismissed, setUpdateDismissed] = useState(null);

  const refreshAuth = useCallback(async () => {
    const status = await api("auth_status");
    setAuth(status?.ok ? status : { ok: true, signedIn: false });
    return status;
  }, []);

  // The shell creates the window HIDDEN, so the first frame anyone sees is a
  // painted sheet rather than an empty transparent rectangle. Reveal it as
  // soon as React has committed — before the backend has answered anything,
  // because the gate screens draw fine without it and a window that waits on
  // the network to appear reads as a launch that failed. (Rust shows it
  // anyway after 2.5s, in case this never runs.)
  useEffect(() => {
    signalReady();
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
      // Always open on Home. The active tab is no longer restored across
      // launches: Home is the overview (accounts, running, autoexec, recent
      // scripts), which is what a fresh launch should land on rather than
      // wherever the last session happened to leave off.
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

  const updateProfile = useCallback((next) => {
    setProfile(next);
    saveSettings({ profile: next });
  }, []);

  const updateLaunch = useCallback((next) => {
    setLaunch(next);
    saveSettings({ launch: next });
  }, []);

  // Ctrl/Cmd+1..N jumps between sections, as long as you aren't typing.
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
      <WindowShell chrome={chrome}>
        <div className="flex h-full flex-1 flex-col overflow-hidden bg-canvas font-sans text-ink antialiased select-none">
          <ResizeEdges chrome={chrome} />
          <TitleBar
            title="Omni Executor"
            subtitle={!auth.signedIn ? "Sign in" : "Setup"}
            chrome={chrome}
            leading={chrome.mac ? <span className="w-[64px]" aria-hidden="true" /> : null}
          />
          <div className="flex min-h-0 flex-1 flex-col">{gate}</div>
        </div>
      </WindowShell>
    );
  }

  return (
    <WindowShell chrome={chrome}>
      <EngineProvider activeTab={tab} showToast={showToast}>
        <EditorStoreProvider>
        <div className="flex h-full flex-1 overflow-hidden bg-canvas font-sans text-ink antialiased select-none">
          <ResizeEdges chrome={chrome} />
          {(updates.applying ||
            (updates.status?.staged && updates.status.staged.version !== updateDismissed)) && (
            <UpdateModal
              updates={updates}
              onDismiss={() => setUpdateDismissed(updates.status?.staged?.version || true)}
            />
          )}
          <Sidebar
            nav={NAV}
            tab={tab}
            onTab={switchTab}
            chrome={chrome}
            premium={auth?.subscription?.tier === "premium"}
          />

          <div className="flex min-w-0 flex-1 flex-col">
            <ContextBar tab={tab} profile={profile} chrome={chrome} netSummary={netSummary} />
            <UpdateBanner updates={updates} onOpenSettings={() => switchTab("settings")} />

            {/* Every view stays mounted so the editor keeps its buffer and the
                accounts list keeps its selection; only visibility changes. */}
            <main className="flex min-h-0 flex-1 flex-col">
              <HomeView
                active={tab === "home"}
                auth={auth}
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
              <FarmingView
                active={tab === "farming"}
                auth={auth}
                launch={launch}
                onGo={switchTab}
                showToast={showToast}
              />
              <StatTrackView
                active={tab === "stattrack"}
                auth={auth}
                onGo={switchTab}
                showToast={showToast}
              />
              <NetworkView active={tab === "network"} onSummary={setNetSummary} />
              <SettingsView
                active={tab === "settings"}
                theme={theme}
                onTheme={applyTheme}
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
    </WindowShell>
  );
}

/** The strip reports what you're looking at and one true fact about it. */
function ContextBar({ tab, profile, chrome, netSummary }) {
  const { accounts, running, health } = useEngine();

  const nav = NAV.find((n) => n.id === tab);
  const subtitle =
    tab === "accounts"
      ? `${accounts.length} ${accounts.length === 1 ? "account" : "accounts"}${
          running.length ? ` · ${running.length} running` : ""
        }`
      : tab === "editor"
        ? `Engine ${health.label.toLowerCase()}`
        : tab === "farming"
          ? `Engine ${health.label.toLowerCase()}`
          : // The bar states one TRUE fact per tab, and the only one this side
            // of the app knows for certain is how many instances are up HERE.
            // "How many are reporting" is the server's answer and the tab
            // itself carries it — repeating a guess at it here would be a
            // second number that can disagree with the first.
            tab === "stattrack"
            ? running.length
              ? `${running.length} running`
              : "Nothing running here"
            : tab === "network"
            ? netSummary || "Checking the link…"
            : tab === "home"
              ? running.length
                ? `${running.length} running`
                : `Engine ${health.label.toLowerCase()}`
              : profile.name || "Guest";

  return <TitleBar title={nav?.label ?? "Omni Executor"} subtitle={subtitle} chrome={chrome} />;
}
