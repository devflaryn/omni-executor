/* Single source of truth for engine state.

   The sidebar lamp, the accounts list and the launch panel all read the same
   store, so they can never disagree about whether an instance is running. */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api, hasBackend, onEngineEvent } from "./api.js";

// The frozen contract version this app was built against (omnidroid-api.md v1).
const EXPECTED_CONTRACT = "1.0";

// engine_modes() is the source of truth for which ids exist; this only
// prettifies the ones we know, so a new engine build can surface a new mode
// without a frontend change.
// Two modes, and that is the whole list. `playable`, `hard` and `brutal` were
// retired in the engine (they were all "gaming with different numbers"); they
// stay here only so an account whose saved launch settings still name one
// renders as something other than a raw id.
const MODE_INFO = {
  gaming: {
    label: "Gaming",
    spec: "4 GB · 4 CPU · GPU",
    note: "Default. Everything the host can give one instance.",
  },
  farming: {
    label: "Farming",
    spec: "2 GB · low RAM",
    note: "Unattended. Squeezed for running many at once.",
  },
  playable: { label: "Gaming", spec: "4 GB · 4 CPU · GPU", note: "Renamed to Gaming." },
  hard: { label: "Gaming", spec: "4 GB · 4 CPU · GPU", note: "Retired — runs as Gaming." },
  brutal: { label: "Gaming", spec: "4 GB · 4 CPU · GPU", note: "Retired — runs as Gaming." },
};

const FALLBACK_MODES = ["gaming", "farming"];

export function describeMode(id) {
  return MODE_INFO[id] || { label: id, spec: "", note: "" };
}

// The display/acceleration setting. `auto` is right for almost everyone: you
// never see a QEMU window either way, because where a window is the only route
// to a GL context it is opened HIDDEN and the viewer hosts it (measured 58 fps
// embedded, against 3.2 with software rendering). The other three exist for
// hosts and situations where that trade should be made differently.
export const GPU_INFO = {
  auto: {
    label: "Automatic (recommended)",
    note: "Hardware rendering, and you still only ever see this app's viewer.",
  },
  headless: {
    label: "Software rendering",
    note: "No GPU. Much slower, but the lightest possible on the host — what farming uses.",
  },
  window: {
    label: "Show the QEMU window",
    note: "For debugging: puts QEMU's own window on screen instead of hiding it.",
  },
  off: { label: "No acceleration", note: "Software rendering, GPU untouched." },
};

export function describeGpu(id) {
  return GPU_INFO[id] || { label: id, note: "" };
}

function normalizeModes(list) {
  return list
    .map((m) => (typeof m === "string" ? m : m && typeof m.id === "string" ? m.id : null))
    .filter(Boolean);
}

export const errText = (res) => res?.message || res?.error || "Engine error";

const EngineContext = createContext(null);

export function useEngine() {
  const store = useContext(EngineContext);
  if (!store) throw new Error("useEngine must be used inside <EngineProvider>");
  return store;
}

export function EngineProvider({ activeTab, showToast, children }) {
  const [backend, setBackend] = useState(null); // null while probing
  const [version, setVersion] = useState(null);
  const [doctor, setDoctor] = useState(null);
  const [modes, setModes] = useState(FALLBACK_MODES);
  const [accounts, setAccounts] = useState([]);
  const [busy, setBusy] = useState({}); // name -> what it is doing
  const [progress, setProgress] = useState({}); // scope -> latest engine stderr line
  const [settingUp, setSettingUp] = useState(false);

  const setBusyFor = useCallback((name, label) => {
    setBusy((prev) => {
      const next = { ...prev };
      if (label) next[name] = label;
      else delete next[name];
      return next;
    });
  }, []);

  const clearProgress = useCallback((scope) => {
    setProgress((prev) => {
      const next = { ...prev };
      delete next[scope];
      return next;
    });
  }, []);

  const [presence, setPresence] = useState({});

  const refreshList = useCallback(async () => {
    const res = await api("engine_list");
    if (res.ok && Array.isArray(res.accounts)) {
      setAccounts(res.accounts.filter((a) => a && typeof a.name === "string"));
    }
  }, []);

  // Where each account is running ACROSS the user's machines. Kept separate
  // from the engine's list because the two answer different questions: the
  // engine knows only about this computer, and an account running on the Mac
  // must not look stopped just because this PC is not running it.
  const refreshPresence = useCallback(async () => {
    const res = await api("cloud_presence");
    if (res?.ok && res.presence) setPresence(res.presence);
  }, []);

  const refreshDoctor = useCallback(async () => setDoctor(await api("engine_doctor")), []);

  // Probe once: backend present? -> contract handshake + readiness + list.
  useEffect(() => {
    hasBackend().then((ok) => {
      setBackend(ok);
      if (!ok) return;
      api("engine_version").then(setVersion);
      api("engine_modes").then((res) => {
        if (Array.isArray(res) && res.length) setModes(normalizeModes(res));
      });
      refreshDoctor();
      refreshList();
      refreshPresence();
    });
  }, [refreshDoctor, refreshList, refreshPresence]);

  // Poll `list` — briskly while the accounts panel is open, lazily otherwise
  // (each poll spawns an engine process, so idle tabs stay cheap).
  useEffect(() => {
    if (!backend) return;
    const interval = activeTab === "accounts" ? 4000 : 20000;
    const timer = setInterval(refreshList, interval);
    return () => clearInterval(timer);
  }, [backend, activeTab, refreshList]);

  // Presence is one cheap HTTP call, not a process spawn, but it is also
  // remote — polling it as hard as the local list would be pointless traffic
  // when the other machine only renews its lease every 25 s.
  useEffect(() => {
    if (!backend) return;
    const timer = setInterval(refreshPresence, activeTab === "accounts" ? 10000 : 45000);
    return () => clearInterval(timer);
  }, [backend, activeTab, refreshPresence]);

  // Events pushed from Python: stderr progress lines and lifecycle changes.
  useEffect(
    () =>
      onEngineEvent((event, payload) => {
        if (event === "engine-progress" && payload?.scope) {
          setProgress((prev) => ({ ...prev, [payload.scope]: payload.line }));
        } else if (event === "accounts-changed") {
          refreshList();
          refreshPresence();
        }
      }),
    [refreshList, refreshPresence]
  );

  // ---- actions ----

  const openViewer = useCallback(
    async (name) => {
      const res = await api("engine_view", name);
      if (!res.ok) showToast(errText(res), "error");
    },
    [showToast]
  );

  const start = useCallback(
    async (name, launch) => {
      const account = accounts.find((a) => a.name === name);
      if (account?.running) return openViewer(name);
      const far = presence[name];
      if (far?.state === "running" && !far.isLocal) {
        // Same Roblox account in two places logs one of them out. Say where it
        // already is rather than silently starting a session that will fight
        // the other one.
        showToast(`${name} is already ${far.label.toLowerCase()}. Stop it there first.`, "error");
        return;
      }
      if (!launch.multiInstance && accounts.some((a) => a.running && a.name !== name)) {
        showToast("Another instance is already running. Turn on Multi-instance to run several.", "error");
        return;
      }
      setBusyFor(name, "Starting");
      const res = await api("engine_start", name, launch.mode, launch.place, launch.gpu);
      setBusyFor(name, null);
      clearProgress(name);
      if (res.ok) {
        showToast(
          res.first_boot ? `${name} is booting — first boot takes a while` : `${name} is running`,
          "success"
        );
        await refreshList();
        refreshPresence();
        openViewer(name);
      } else {
        showToast(errText(res), "error");
        refreshList();
      }
    },
    [accounts, clearProgress, openViewer, presence, refreshList, refreshPresence, setBusyFor, showToast]
  );

  const stop = useCallback(
    async (name) => {
      setBusyFor(name, "Stopping");
      const res = await api("engine_stop", name);
      setBusyFor(name, null);
      clearProgress(name);
      showToast(res.ok ? `Stopped ${name}` : errText(res), res.ok ? "success" : "error");
      refreshList();
      refreshPresence();
    },
    [clearProgress, refreshList, refreshPresence, setBusyFor, showToast]
  );

  const remove = useCallback(
    async (name) => {
      setBusyFor(name, "Removing");
      const res = await api("engine_remove", name);
      setBusyFor(name, null);
      clearProgress(name);
      showToast(res.ok ? `Removed ${name} and deleted its data` : errText(res), res.ok ? "success" : "error");
      refreshList();
      return res.ok;
    },
    [clearProgress, refreshList, setBusyFor, showToast]
  );

  const runSetup = useCallback(async () => {
    setSettingUp(true);
    const res = await api("engine_setup");
    setSettingUp(false);
    clearProgress("setup");
    showToast(res.ok ? "Engine setup complete" : errText(res), res.ok ? "success" : "error");
    refreshDoctor();
    refreshList();
  }, [clearProgress, refreshDoctor, refreshList, showToast]);

  const loginBrowser = useCallback(async () => {
    const res = await api("engine_login_browser");
    if (res.ok) showToast(res.name ? `Added ${res.name}` : "Account added", "success");
    refreshList();
    return res;
  }, [refreshList, showToast]);

  const loginToken = useCallback(
    async (token) => {
      const res = await api("engine_login_token", token);
      if (res.ok) showToast(res.name ? `Added ${res.name}` : "Account added", "success");
      refreshList();
      return res;
    },
    [refreshList, showToast]
  );

  // ---- derived ----

  const running = useMemo(() => accounts.filter((a) => a.running), [accounts]);

  /* One list the UI can render directly, with each account carrying where it
     is running. The LOCAL engine wins when it says an account is up here — it
     is the ground truth for this machine, and it is fresher than a lease that
     is renewed every 25 s. The remote lease only fills in the rows this
     machine is NOT running. */
  const accountsWithPresence = useMemo(
    () =>
      accounts.map((a) => {
        if (a.running) return { ...a, where: { state: "running", label: "Running", isLocal: true } };
        const far = presence[a.name];
        if (far?.state === "running" && !far.isLocal) return { ...a, where: far };
        return { ...a, where: { state: "stopped", label: "Stopped", isLocal: false } };
      }),
    [accounts, presence]
  );

  /* Accounts running on ANOTHER machine. Starting one of these here would
     boot a second copy of the same Roblox session, which Roblox itself
     resolves by kicking one of them out — so the UI warns instead. */
  const runningElsewhere = useMemo(
    () => accountsWithPresence.filter((a) => a.where.state === "running" && !a.where.isLocal),
    [accountsWithPresence]
  );

  // The contract version alone does not tell you the engine can do what this
  // app asks of it: an engine can speak 1.0 and still lack `login`/`view`.
  // main.py compares its required calls against the engine's own advertised
  // command list, so a stale engine is named here instead of failing later
  // with an argparse error the moment someone clicks "Add account".
  const missingCommands = version?.missing_commands || [];
  const contractMismatch =
    version &&
    (!version.ok ||
      version.contract !== EXPECTED_CONTRACT ||
      version.arch_aware !== true ||
      missingCommands.length > 0);

  /** One line the whole app can trust for "is the engine usable right now". */
  const health = useMemo(() => {
    if (backend === null) return { tone: "off", label: "Connecting" };
    if (backend === false) return { tone: "off", label: "Preview mode" };
    if (doctor?.error === "engine_missing") return { tone: "fault", label: "Engine missing" };
    if (contractMismatch) return { tone: "busy", label: "Version mismatch" };
    if (doctor && !doctor.ready) return { tone: "busy", label: "Setup needed" };
    if (!doctor) return { tone: "off", label: "Checking" };
    if (running.length) return { tone: "live", label: `${running.length} running` };
    return { tone: "live", label: "Ready" };
  }, [backend, doctor, contractMismatch, running.length]);

  /** The problem worth interrupting the user about, or null. */
  const issue = useMemo(() => {
    if (backend === false) {
      return {
        tone: "info",
        text: "Preview mode. Instances need the desktop app — run python main.py.",
      };
    }
    if (doctor?.error === "engine_missing") {
      return { tone: "error", text: doctor.message };
    }
    if (contractMismatch) {
      if (version.ok && missingCommands.length > 0) {
        return {
          tone: "warn",
          text: `This omnidroid build is missing ${missingCommands.join(", ")} — ${
            missingCommands.length === 1 ? "that feature" : "those features"
          } will not work. Update the engine next to the app.`,
        };
      }
      return {
        tone: "warn",
        text: version.ok
          ? `This engine speaks contract ${version.contract || "?"} (arch-aware: ${
              version.arch_aware ? "yes" : "no"
            }); the app expects ${EXPECTED_CONTRACT}. Update omnidroid to get every feature.`
          : `Couldn't read the engine contract. ${version.message || ""}`,
      };
    }
    if (doctor && !doctor.ready) {
      const problems = [];
      if (doctor.missing_files?.length) {
        problems.push(`base images missing from ${doctor.images_dir}: ${doctor.missing_files.join(", ")}`);
      }
      if (doctor.qemu_present === false) problems.push("QEMU isn't installed yet");
      if (doctor.adb_present === false) problems.push("adb isn't on PATH");
      return {
        tone: "warn",
        text: `The engine isn't ready — ${problems.join("; ") || doctor.message || "run setup to finish installing"}.`,
        canSetup: true,
      };
    }
    return null;
  }, [backend, doctor, contractMismatch, version, missingCommands]);

  const value = {
    backend,
    version,
    doctor,
    modes,
    accounts: accountsWithPresence,
    running,
    runningElsewhere,
    presence,
    refreshPresence,
    busy,
    progress,
    settingUp,
    health,
    issue,
    refreshList,
    refreshDoctor,
    start,
    stop,
    remove,
    openViewer,
    runSetup,
    loginBrowser,
    loginToken,
  };

  return <EngineContext.Provider value={value}>{children}</EngineContext.Provider>;
}
