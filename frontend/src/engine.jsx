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
const MODE_INFO = {
  playable: { label: "Playable", spec: "4 GB · 4 CPU", note: "Default. Smoothest gameplay." },
  hard: { label: "Hard", spec: "3 GB · 4 CPU", note: "Leaner memory, same cores." },
  brutal: { label: "Brutal", spec: "2 GB · 2 CPU", note: "Minimum footprint." },
  farming: { label: "Farming", spec: "tuned", note: "Optimised for unattended automation." },
};

const FALLBACK_MODES = Object.keys(MODE_INFO);

export function describeMode(id) {
  return MODE_INFO[id] || { label: id, spec: "", note: "" };
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

  const refreshList = useCallback(async () => {
    const res = await api("engine_list");
    if (res.ok && Array.isArray(res.accounts)) {
      setAccounts(res.accounts.filter((a) => a && typeof a.name === "string"));
    }
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
    });
  }, [refreshDoctor, refreshList]);

  // Poll `list` — briskly while the accounts panel is open, lazily otherwise
  // (each poll spawns an engine process, so idle tabs stay cheap).
  useEffect(() => {
    if (!backend) return;
    const interval = activeTab === "accounts" ? 4000 : 20000;
    const timer = setInterval(refreshList, interval);
    return () => clearInterval(timer);
  }, [backend, activeTab, refreshList]);

  // Events pushed from Python: stderr progress lines and lifecycle changes.
  useEffect(
    () =>
      onEngineEvent((event, payload) => {
        if (event === "engine-progress" && payload?.scope) {
          setProgress((prev) => ({ ...prev, [payload.scope]: payload.line }));
        } else if (event === "accounts-changed") {
          refreshList();
        }
      }),
    [refreshList]
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
      if (!launch.multiInstance && accounts.some((a) => a.running && a.name !== name)) {
        showToast("Another instance is already running. Turn on Multi-instance to run several.", "error");
        return;
      }
      setBusyFor(name, "Starting");
      const res = await api("engine_start", name, launch.mode, launch.place);
      setBusyFor(name, null);
      clearProgress(name);
      if (res.ok) {
        showToast(
          res.first_boot ? `${name} is booting — first boot takes a while` : `${name} is running`,
          "success"
        );
        await refreshList();
        openViewer(name);
      } else {
        showToast(errText(res), "error");
        refreshList();
      }
    },
    [accounts, clearProgress, openViewer, refreshList, setBusyFor, showToast]
  );

  const stop = useCallback(
    async (name) => {
      setBusyFor(name, "Stopping");
      const res = await api("engine_stop", name);
      setBusyFor(name, null);
      clearProgress(name);
      showToast(res.ok ? `Stopped ${name}` : errText(res), res.ok ? "success" : "error");
      refreshList();
    },
    [clearProgress, refreshList, setBusyFor, showToast]
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
    accounts,
    running,
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
