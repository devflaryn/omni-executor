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

// There is one kind of launch. The QEMU engine offered Gaming and Farming
// presets, a GPU policy and a warm pool, because each was a different virtual
// machine with a different cost; native omnidroid has none of those trades, so
// none of them is offered any more.

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
  const [accounts, setAccounts] = useState([]);
  const [busy, setBusy] = useState({}); // name -> what it is doing
  const [progress, setProgress] = useState({}); // scope -> latest engine stderr line

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

  /* `bulk` is set by startMany: several instances were asked for at once, so
     one toast at the end says how it went instead of one per account. Each
     instance opens its own window when its app draws -- omnidroid's, not ours
     -- so there is no viewer to pop. */
  const start = useCallback(
    async (name, launch, { bulk = false } = {}) => {
      const account = accounts.find((a) => a.name === name);
      if (account?.running) return true;
      const far = presence[name];
      if (far?.state === "running" && !far.isLocal) {
        // Same Roblox account in two places logs one of them out. Say where it
        // already is rather than silently starting a session that will fight
        // the other one.
        showToast(`${name} is already ${far.label.toLowerCase()}. Stop it there first.`, "error");
        return false;
      }
      // Running several at once is the normal case, not an opt-in: there used
      // to be a Multi-instance toggle gating this, and every account it
      // blocked was one the machine could have run. The only launch this still
      // refuses is the same ROBLOX account twice (checked above), because that
      // one genuinely logs itself out.
      setBusyFor(name, "Starting");
      const res = await api("engine_start", name, launch?.place);
      setBusyFor(name, null);
      clearProgress(name);
      if (res.ok) {
        if (!bulk) {
          showToast(`${name} is starting — its window opens when the game draws`, "success");
        }
        await refreshList();
        refreshPresence();
        return true;
      }
      showToast(bulk ? `${name}: ${errText(res)}` : errText(res), "error");
      refreshList();
      return false;
    },
    [accounts, clearProgress, presence, refreshList, refreshPresence, setBusyFor, showToast]
  );

  /* Launch several. Each start is its own bridge call on its own thread, so
     they run concurrently; the stagger just keeps N launches from reaching
     omnidroid in the same millisecond. Resolves when the last one has reported. */
  const startMany = useCallback(
    async (names, launch) => {
      const list = names.filter((n, i) => names.indexOf(n) === i);
      if (!list.length) return { ok: 0, failed: 0 };
      const results = await Promise.all(
        list.map(
          (name, i) =>
            new Promise((resolve) =>
              setTimeout(() => start(name, launch, { bulk: true }).then(resolve), i * 1200)
            )
        )
      );
      const ok = results.filter(Boolean).length;
      const failed = list.length - ok;
      showToast(
        failed === 0
          ? `Launching ${ok} ${ok === 1 ? "instance" : "instances"}`
          : `${ok} launched, ${failed} failed`,
        failed === 0 ? "success" : "error"
      );
      return { ok, failed };
    },
    [start, showToast]
  );

  const stop = useCallback(
    async (name, { bulk = false } = {}) => {
      setBusyFor(name, "Stopping");
      const res = await api("engine_stop", name);
      setBusyFor(name, null);
      clearProgress(name);
      if (!bulk || !res.ok) showToast(res.ok ? `Stopped ${name}` : errText(res), res.ok ? "success" : "error");
      refreshList();
      refreshPresence();
      return Boolean(res.ok);
    },
    [clearProgress, refreshList, refreshPresence, setBusyFor, showToast]
  );

  const stopMany = useCallback(
    async (names) => {
      if (!names.length) return { ok: 0, failed: 0 };
      const results = await Promise.all(names.map((n) => stop(n, { bulk: true })));
      const ok = results.filter(Boolean).length;
      showToast(
        ok === names.length ? `Stopped ${ok} ${ok === 1 ? "instance" : "instances"}` : `Stopped ${ok} of ${names.length}`,
        ok === names.length ? "success" : "error"
      );
      return { ok, failed: names.length - ok };
    },
    [stop, showToast]
  );

  const remove = useCallback(
    async (name) => {
      setBusyFor(name, "Removing");
      const res = await api("engine_remove", name);
      setBusyFor(name, null);
      clearProgress(name);
      showToast(res.ok ? `Removed ${name} from this computer` : errText(res), res.ok ? "success" : "error");
      refreshList();
      return res.ok;
    },
    [clearProgress, refreshList, setBusyFor, showToast]
  );

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
    if (doctor?.error === "engine_missing" || doctor?.error === "apk_missing") return { tone: "fault", label: "Setup needed" };
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
    if (doctor?.error === "engine_missing" || doctor?.error === "apk_missing") {
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
      return { tone: "warn", text: doctor.message || "omnidroid isn't ready yet." };
    }
    return null;
  }, [backend, doctor, contractMismatch, version, missingCommands]);

  const value = {
    backend,
    version,
    doctor,
    accounts: accountsWithPresence,
    running,
    runningElsewhere,
    presence,
    refreshPresence,
    busy,
    progress,
    health,
    issue,
    refreshList,
    refreshDoctor,
    start,
    startMany,
    stop,
    stopMany,
    remove,
    loginBrowser,
    loginToken,
  };

  return <EngineContext.Provider value={value}>{children}</EngineContext.Provider>;
}
