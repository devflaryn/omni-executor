import { useCallback, useEffect, useRef, useState } from "react";
import { api, hasBackend, onEngineEvent } from "../api.js";
import {
  UsersIcon, RocketIcon, PlayIcon, StopIcon, MonitorIcon, TrashIcon, PlusIcon, AlertIcon,
} from "./icons.jsx";

// Engine contract (omnidroid-api.md): account names are [A-Za-z0-9_-]+.
const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;
const NAME_HINT = "Only letters, digits, '_' and '-' — no spaces.";

// The frozen contract version this app was built against (omnidroid-api.md v1).
const EXPECTED_CONTRACT = "1.0";

const MODES = [
  { id: "playable", label: "Playable — 4 GB RAM · 4 CPUs (default)" },
  { id: "hard", label: "Hard — 3 GB RAM · 4 CPUs" },
  { id: "brutal", label: "Brutal — 2 GB RAM · 2 CPUs" },
];

function initials(name) {
  const parts = String(name).trim().split(/[\s_-]+/).filter(Boolean);
  const chars = parts.slice(0, 2).map((p) => p[0]);
  return (chars.join("") || "?").toUpperCase();
}

const errText = (res) => res?.message || res?.error || "Engine error";

export default function AccountsView({ active, showToast, launchOpts, onLaunchOpts }) {
  const [backend, setBackend] = useState(null); // null = probing
  const [doctor, setDoctor] = useState(null);
  const [version, setVersion] = useState(null); // engine handshake (contract §4)
  const [accounts, setAccounts] = useState([]);
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState({}); // name -> label
  const [progress, setProgress] = useState({}); // scope -> latest stderr line
  const [formOpen, setFormOpen] = useState(false);
  const [formName, setFormName] = useState("");
  const [formError, setFormError] = useState("");
  const [removeTarget, setRemoveTarget] = useState(null);
  const [removeInput, setRemoveInput] = useState("");
  const [setupRunning, setSetupRunning] = useState(false);

  const busyRef = useRef(busy);
  busyRef.current = busy;

  const setBusyFor = useCallback((name, label) => {
    setBusy((prev) => {
      const next = { ...prev };
      if (label) next[name] = label;
      else delete next[name];
      return next;
    });
  }, []);

  const refreshList = useCallback(async () => {
    const res = await api("engine_list");
    if (res.ok && Array.isArray(res.accounts)) {
      setAccounts(res.accounts.filter((a) => a && typeof a.name === "string"));
    }
  }, []);

  const refreshDoctor = useCallback(async () => {
    setDoctor(await api("engine_doctor"));
  }, []);

  // Initial probe: backend? -> handshake (version) + doctor + list.
  useEffect(() => {
    hasBackend().then((ok) => {
      setBackend(ok);
      if (ok) {
        api("engine_version").then(setVersion); // contract handshake, gated below
        refreshDoctor();
        refreshList();
      }
    });
  }, [refreshDoctor, refreshList]);

  // Live state: poll `list` while the tab is visible.
  useEffect(() => {
    if (!active || !backend) return;
    refreshList();
    const timer = setInterval(refreshList, 4000);
    return () => clearInterval(timer);
  }, [active, backend, refreshList]);

  // Engine events pushed from Python (stderr progress, lifecycle changes).
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

  const runSetup = async () => {
    setSetupRunning(true);
    const res = await api("engine_setup");
    setSetupRunning(false);
    setProgress((prev) => ({ ...prev, setup: undefined }));
    showToast(res.ok ? "Engine setup complete" : errText(res));
    refreshDoctor();
    refreshList();
  };

  const submitCreate = async (e) => {
    e.preventDefault();
    const name = formName.trim();
    if (!NAME_RE.test(name)) {
      setFormError(NAME_HINT);
      return;
    }
    if (accounts.some((a) => a.name === name)) {
      setFormError("That account already exists.");
      return;
    }
    setFormOpen(false);
    setFormName("");
    setFormError("");
    setBusyFor(name, "Creating — first provisioning can take 3–15 min");
    // Optimistic placeholder row so the long create is visible immediately.
    setAccounts((prev) => [...prev, { name, running: false, creating: true }]);
    const res = await api("engine_create", name);
    setBusyFor(name, null);
    setProgress((prev) => ({ ...prev, [name]: undefined }));
    if (res.ok) {
      showToast(`Created ${name}`);
      setSelected(name);
    } else {
      showToast(errText(res));
      setAccounts((prev) => prev.filter((a) => !(a.name === name && a.creating)));
    }
    refreshList();
  };

  const openViewer = async (name) => {
    const res = await api("open_viewer", name);
    if (!res.ok) showToast(errText(res));
    else if (launchOpts.minimizeOnLaunch) api("minimize");
  };

  const start = async (name) => {
    const account = accounts.find((a) => a.name === name);
    if (account?.running) return openViewer(name);
    if (!launchOpts.multiInstance && accounts.some((a) => a.running && a.name !== name)) {
      showToast("Another account is running — enable Multi-instance to run several at once");
      return;
    }
    setBusyFor(name, "Starting…");
    const res = await api("engine_start", name, launchOpts.mode);
    setBusyFor(name, null);
    setProgress((prev) => ({ ...prev, [name]: undefined }));
    if (res.ok) {
      showToast(res.first_boot ? `${name} is booting (first boot takes longer)` : `${name} is running`);
      await refreshList();
      openViewer(name);
    } else {
      showToast(errText(res));
      refreshList();
    }
  };

  const stop = async (name) => {
    setBusyFor(name, "Stopping…");
    const res = await api("engine_stop", name);
    setBusyFor(name, null);
    setProgress((prev) => ({ ...prev, [name]: undefined }));
    showToast(res.ok ? `Stopped ${name}` : errText(res));
    refreshList();
  };

  const confirmRemove = async () => {
    const name = removeTarget?.name;
    setRemoveTarget(null);
    setRemoveInput("");
    if (!name) return;
    setBusyFor(name, "Removing…");
    const res = await api("engine_remove", name);
    setBusyFor(name, null);
    setProgress((prev) => ({ ...prev, [name]: undefined }));
    if (res.ok) {
      showToast(`Removed ${name} and deleted its data`);
      if (selected === name) setSelected(null);
    } else {
      showToast(errText(res));
    }
    refreshList();
  };

  // ---- derived ----

  const selectedAccount = accounts.find((a) => a.name === selected) || null;
  const selectedBusy = selected ? busy[selected] : null;
  const anyBackend = backend === true;

  // ---- banner ----

  let banner = null;
  const contractMismatch =
    version && (!version.ok || version.contract !== EXPECTED_CONTRACT || version.arch_aware !== true);
  if (backend === false) {
    banner = { tone: "info", text: "Browser preview — engine features need the desktop app (python main.py)." };
  } else if (doctor && doctor.error === "engine_missing") {
    banner = { tone: "error", text: doctor.message };
  } else if (contractMismatch) {
    banner = {
      tone: "warn",
      text: version.ok
        ? `This engine reports contract ${version.contract || "?"} (arch-aware: ${version.arch_aware ? "yes" : "no"}); ` +
          `this app expects ${EXPECTED_CONTRACT}. Some features may not work — update omnidroid.`
        : `Couldn't read the engine contract (omni version) — the engine may be too old. ${version.message || ""}`,
    };
  } else if (doctor && !doctor.ready) {
    const problems = [];
    if (Array.isArray(doctor.missing_files) && doctor.missing_files.length) {
      problems.push(`missing base files in ${doctor.images_dir}: ${doctor.missing_files.join(", ")}`);
    }
    if (doctor.qemu_present === false) problems.push("QEMU is not set up yet");
    if (doctor.adb_present === false) problems.push("adb not found on PATH");
    banner = {
      tone: "warn",
      text: `Engine is not ready — ${problems.join("; ") || doctor.message || "see doctor output"}.`,
      setup: true,
    };
  }

  const bannerTones = {
    info: "border-slate-300 bg-slate-100 text-slate-600 dark:border-white/10 dark:bg-white/[.04] dark:text-slate-300",
    warn: "border-amber-400/40 bg-amber-50 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300",
    error: "border-red-400/40 bg-red-50 text-red-600 dark:bg-red-400/10 dark:text-red-300",
  };

  return (
    <section className={`min-h-0 flex-1 overflow-y-auto ${active ? "" : "hidden"}`}>
      {banner && (
        <div className={`animate-rise mb-4 flex items-start gap-2.5 rounded-2xl border px-4 py-3 text-[12.5px] ${bannerTones[banner.tone]}`}>
          <AlertIcon className="mt-px h-4 w-4 shrink-0" />
          <div className="min-w-0 flex-1">
            <p>{banner.text}</p>
            {setupRunning && (
              <p className="mt-1 truncate font-mono text-[11px] opacity-80">
                {progress.setup || "Running setup…"}
              </p>
            )}
          </div>
          {banner.setup && (
            <button onClick={runSetup} disabled={setupRunning} className="btn-primary shrink-0">
              {setupRunning ? "Setting up…" : "Run setup"}
            </button>
          )}
        </div>
      )}

      <div className="animate-rise grid items-start gap-4 md:grid-cols-[1fr_320px]">
        {/* -------- Account list -------- */}
        <div className="card overflow-hidden">
          <div
            className="flex items-center justify-between border-b border-slate-200 px-4 py-3
                       transition-colors duration-300 dark:border-white/[.06]"
          >
            <div className="flex items-center gap-2">
              <UsersIcon className="h-4 w-4 text-slate-400" />
              <h2 className="text-[13px] font-semibold">Accounts</h2>
              <span
                className="rounded-full bg-slate-100 px-2 py-0.5 text-[10.5px] font-semibold text-slate-500
                           dark:bg-white/[.06] dark:text-slate-400"
              >
                {accounts.length}
              </span>
            </div>
            <button
              onClick={() => {
                setFormOpen((open) => !open);
                setFormName("");
                setFormError("");
              }}
              disabled={!anyBackend}
              className="btn-primary"
            >
              <PlusIcon className="h-3.5 w-3.5" />
              Add account
            </button>
          </div>

          {formOpen && (
            <form
              onSubmit={submitCreate}
              className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-slate-50/60 px-4 py-3
                         transition-colors duration-300 dark:border-white/[.06] dark:bg-white/[.02]"
            >
              <input
                autoFocus
                value={formName}
                onChange={(e) => {
                  setFormName(e.target.value);
                  setFormError("");
                }}
                onKeyDown={(e) => e.key === "Escape" && setFormOpen(false)}
                className="input flex-1"
                type="text"
                placeholder="Account name (e.g. player_01)"
                maxLength={64}
                autoComplete="off"
                spellCheck={false}
              />
              <button type="submit" className="btn-primary px-3 py-1.5">
                Create
              </button>
              <button type="button" onClick={() => setFormOpen(false)} className="btn-ghost px-3 py-1.5">
                Cancel
              </button>
              <p className={`w-full text-[11px] ${formError ? "text-red-400" : "text-slate-400 dark:text-slate-500"}`}>
                {formError || `${NAME_HINT} Creating provisions a fresh Android instance (first time: ~3–15 min).`}
              </p>
            </form>
          )}

          <div className="divide-y divide-slate-100 dark:divide-white/[.04]">
            {accounts.map((account) => (
              <AccountRow
                key={account.name}
                account={account}
                selected={selected === account.name}
                busyLabel={busy[account.name]}
                progressLine={busy[account.name] ? progress[account.name] : null}
                onSelect={() => setSelected(account.name)}
                onStart={() => start(account.name)}
                onStop={() => stop(account.name)}
                onOpen={() => openViewer(account.name)}
                onRemove={() => {
                  setRemoveTarget(account);
                  setRemoveInput("");
                }}
              />
            ))}
          </div>

          {accounts.length === 0 && (
            <div className="flex flex-col items-center gap-2 px-4 py-12 text-center">
              <UsersIcon className="h-8 w-8 text-slate-300 dark:text-slate-600" strokeWidth={1.6} />
              <p className="text-[13px] font-medium text-slate-500 dark:text-slate-400">No accounts yet</p>
              <p className="text-[11.5px] text-slate-400 dark:text-slate-500">
                "Add account" creates an isolated Android instance for one game.
              </p>
            </div>
          )}
        </div>

        {/* -------- Launch options -------- */}
        <div className="card overflow-hidden">
          <div
            className="flex items-center gap-2 border-b border-slate-200 px-4 py-3
                       transition-colors duration-300 dark:border-white/[.06]"
          >
            <RocketIcon className="h-4 w-4 text-slate-400" />
            <h2 className="text-[13px] font-semibold">Launch options</h2>
          </div>

          <div className="flex flex-col gap-4 p-4">
            <label className="flex flex-col gap-1.5">
              <span className="text-[11px] font-medium tracking-wider text-slate-400 uppercase dark:text-slate-500">
                Performance mode
              </span>
              <select
                value={launchOpts.mode}
                onChange={(e) => onLaunchOpts({ ...launchOpts, mode: e.target.value })}
                className="input cursor-pointer"
              >
                {MODES.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex cursor-pointer items-center justify-between gap-3">
              <span>
                <span className="block text-[12.5px] font-medium">Multi-instance</span>
                <span className="text-[11px] text-slate-400 dark:text-slate-500">Allow several accounts at once</span>
              </span>
              <input
                type="checkbox"
                checked={launchOpts.multiInstance}
                onChange={(e) => onLaunchOpts({ ...launchOpts, multiInstance: e.target.checked })}
                className="h-4 w-4 shrink-0 cursor-pointer accent-indigo-500"
              />
            </label>

            <label className="flex cursor-pointer items-center justify-between gap-3">
              <span>
                <span className="block text-[12.5px] font-medium">Minimize after launch</span>
                <span className="text-[11px] text-slate-400 dark:text-slate-500">Hide this window once the viewer opens</span>
              </span>
              <input
                type="checkbox"
                checked={launchOpts.minimizeOnLaunch}
                onChange={(e) => onLaunchOpts({ ...launchOpts, minimizeOnLaunch: e.target.checked })}
                className="h-4 w-4 shrink-0 cursor-pointer accent-indigo-500"
              />
            </label>

            <button
              onClick={() => selectedAccount && start(selectedAccount.name)}
              disabled={!selectedAccount || Boolean(selectedBusy)}
              className="mt-1 flex items-center justify-center gap-2 rounded-xl bg-indigo-500 px-4 py-2.5
                         text-[13px] font-semibold text-white shadow-sm transition-all duration-150 outline-none
                         hover:bg-indigo-600 focus-visible:ring-2 focus-visible:ring-indigo-400/60
                         active:scale-[.98] disabled:pointer-events-none disabled:opacity-50
                         dark:hover:bg-indigo-400"
            >
              <PlayIcon className="h-4 w-4" />
              {selectedAccount?.running ? "Open viewer" : "Launch"}
            </button>
            <p className="-mt-1 text-center text-[11px] text-slate-400 dark:text-slate-500">
              {selectedBusy ||
                (selectedAccount
                  ? selectedAccount.running
                    ? `${selectedAccount.name} is running`
                    : `Ready to launch ${selectedAccount.name}`
                  : "Select an account to launch")}
            </p>
          </div>
        </div>
      </div>

      {/* -------- Remove confirmation (DESTRUCTIVE) -------- */}
      {removeTarget && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4">
          <div className="card animate-rise w-full max-w-sm p-5">
            <h2 className="text-[14px] font-semibold text-red-500">Remove account</h2>
            <p className="mt-1.5 text-[12px] leading-relaxed text-slate-500 dark:text-slate-400">
              This permanently deletes <span className="font-semibold">{removeTarget.name}</span>
              {removeTarget.running ? " (currently running — it will be stopped first)" : ""} — its Android
              instance, game data and saves are gone forever. Type the account name to confirm.
            </p>
            <input
              autoFocus
              value={removeInput}
              onChange={(e) => setRemoveInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setRemoveTarget(null);
                if (e.key === "Enter" && removeInput === removeTarget.name) confirmRemove();
              }}
              className="input mt-3"
              placeholder={removeTarget.name}
              autoComplete="off"
              spellCheck={false}
            />
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setRemoveTarget(null)} className="btn-ghost px-3 py-1.5">
                Cancel
              </button>
              <button
                onClick={confirmRemove}
                disabled={removeInput !== removeTarget.name}
                className="rounded-lg bg-red-500 px-3 py-1.5 text-[12px] font-semibold text-white shadow-sm
                           transition-all duration-150 outline-none hover:bg-red-600 active:scale-95
                           focus-visible:ring-2 focus-visible:ring-red-400/60
                           disabled:pointer-events-none disabled:opacity-50"
              >
                Remove &amp; delete data
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function AccountRow({ account, selected, busyLabel, progressLine, onSelect, onStart, onStop, onOpen, onRemove }) {
  const running = Boolean(account.running);

  let dot = "bg-slate-300 dark:bg-slate-600";
  let statusText = "Stopped";
  if (busyLabel) {
    dot = "animate-pulse bg-amber-400";
    statusText = busyLabel;
  } else if (running) {
    dot = "bg-emerald-400";
    statusText = `Running · VNC ${account.vnc_port ?? "?"} · ADB ${account.adb_port ?? "?"}`;
  }

  return (
    <div
      onClick={onSelect}
      onDoubleClick={() => running && !busyLabel && onOpen()}
      className={`group flex cursor-pointer items-center gap-3 px-4 py-2.5 transition-colors duration-150
                  hover:bg-slate-50 dark:hover:bg-white/[.03]
                  ${selected ? "bg-indigo-500/[.05] dark:bg-indigo-400/[.08]" : ""}`}
    >
      <span
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br
                    from-indigo-500 to-fuchsia-500 text-[11px] font-bold text-white
                    ${selected ? "ring-2 ring-indigo-400 ring-offset-2 ring-offset-white dark:ring-offset-[#1c1e29]" : ""}`}
      >
        {initials(account.name)}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[13px] font-medium">{account.name}</span>
          {account.base && (
            <span
              className="shrink-0 rounded-full border border-slate-200 px-1.5 py-px text-[9.5px] font-semibold
                         tracking-wider text-slate-400 uppercase dark:border-white/10 dark:text-slate-500"
            >
              {account.base}
            </span>
          )}
          {account.arch && (
            <span
              title={`Architecture: ${account.arch}`}
              className={`shrink-0 rounded-full px-1.5 py-px text-[9.5px] font-semibold tracking-wider uppercase ${
                account.arch === "arm"
                  ? "bg-teal-500/15 text-teal-600 dark:text-teal-300"
                  : "bg-slate-500/10 text-slate-500 dark:text-slate-400"
              }`}
            >
              {account.arch}
            </span>
          )}
          {account.mode && running && (
            <span
              className="shrink-0 rounded-full bg-indigo-500/10 px-1.5 py-px text-[9.5px] font-semibold
                         tracking-wider text-indigo-500 uppercase dark:bg-indigo-400/15 dark:text-indigo-300"
            >
              {account.mode}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-slate-400 dark:text-slate-500">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
          <span className="truncate">{progressLine || statusText}</span>
        </div>
      </div>

      {running && !busyLabel && (
        <>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onOpen();
            }}
            title="Open viewer"
            className="row-icon-btn opacity-0 group-hover:opacity-100 focus-visible:opacity-100
                       hover:bg-indigo-500/10 hover:text-indigo-500"
          >
            <MonitorIcon className="h-4 w-4" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onStop();
            }}
            title="Stop instance"
            className="row-icon-btn opacity-0 group-hover:opacity-100 focus-visible:opacity-100
                       hover:bg-amber-500/10 hover:text-amber-500"
          >
            <StopIcon className="h-4 w-4" />
          </button>
        </>
      )}
      {!running && !busyLabel && !account.creating && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onStart();
          }}
          title="Start (headless)"
          className="row-icon-btn opacity-0 group-hover:opacity-100 focus-visible:opacity-100
                     hover:bg-indigo-500/10 hover:text-indigo-500"
        >
          <PlayIcon className="h-4 w-4" />
        </button>
      )}
      <button
        onClick={(e) => {
          e.stopPropagation();
          onRemove();
        }}
        disabled={Boolean(busyLabel)}
        title="Remove account (deletes its data)"
        className="row-icon-btn opacity-0 group-hover:opacity-100 focus-visible:opacity-100
                   hover:bg-red-500/10 hover:text-red-500"
      >
        <TrashIcon className="h-4 w-4" />
      </button>
    </div>
  );
}
