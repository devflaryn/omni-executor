/* Instances panel: one row per account, one launch bay on the right. */

import { useEffect, useMemo, useRef, useState } from "react";
import { describeMode, errText, useEngine } from "../engine.jsx";
import {
  Button,
  Field,
  IconButton,
  Lamp,
  Modal,
  Notice,
  Panel,
  PanelHead,
  Toggle,
} from "./ui.jsx";
import {
  AlertIcon,
  InfoIcon,
  LayersIcon,
  MonitorIcon,
  PlayIcon,
  PlusIcon,
  RocketIcon,
  SearchIcon,
  StopIcon,
  TrashIcon,
  UsersIcon,
} from "./icons.jsx";

/** Two characters, always: one per word, or the first two letters of a single
    run-together name like "admn1b12farm2". */
function initials(name) {
  const parts = String(name).trim().split(/[\s_-]+/).filter(Boolean);
  if (!parts.length) return "?";
  const chars = parts.length > 1 ? parts.slice(0, 2).map((p) => p[0]) : [parts[0].slice(0, 2)];
  return chars.join("").toUpperCase();
}

export default function AccountsView({ active, launch, onLaunch, showToast }) {
  const engine = useEngine();
  const { accounts, busy, progress, backend, issue, settingUp, modes } = engine;

  const [selected, setSelected] = useState(null);
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [removeTarget, setRemoveTarget] = useState(null);

  const usable = backend === true;

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? accounts.filter((a) => a.name.toLowerCase().includes(q)) : accounts;
  }, [accounts, query]);

  // Keep the selection pointing at something real.
  useEffect(() => {
    if (selected && !accounts.some((a) => a.name === selected)) setSelected(null);
    if (!selected && accounts.length === 1) setSelected(accounts[0].name);
  }, [accounts, selected]);

  const selectedAccount = accounts.find((a) => a.name === selected) || null;
  const selectedBusy = selected ? busy[selected] : null;

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-4">
        {issue && (
          <Notice
            tone={issue.tone}
            icon={issue.tone === "info" ? InfoIcon : AlertIcon}
            action={
              issue.canSetup && (
                <Button variant="solid" size="sm" onClick={engine.runSetup} disabled={settingUp}>
                  {settingUp ? "Installing…" : "Run setup"}
                </Button>
              )
            }
          >
            <p>{issue.text}</p>
            {settingUp && (
              <p className="mt-1 truncate font-mono text-[11px] opacity-70">
                {progress.setup || "Working…"}
              </p>
            )}
          </Notice>
        )}

        <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_308px]">
          {/* ---- Instances ---- */}
          <Panel className="animate-rise overflow-hidden">
            <PanelHead
              icon={UsersIcon}
              title="Instances"
              count={accounts.length}
              right={
                <>
                  <div className="relative hidden sm:block">
                    <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
                    <input
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Filter"
                      aria-label="Filter instances by name"
                      spellCheck={false}
                      className="input h-7 w-[132px] py-0 pl-8 text-[12px]"
                    />
                  </div>
                  <Button variant="solid" size="sm" onClick={() => setAdding(true)} disabled={!usable}>
                    <PlusIcon className="h-3.5 w-3.5" />
                    Add account
                  </Button>
                </>
              }
            />

            {visible.length > 0 ? (
              <ul>
                {visible.map((account) => (
                  <AccountRow
                    key={account.name}
                    account={account}
                    selected={selected === account.name}
                    busyLabel={busy[account.name]}
                    progressLine={busy[account.name] ? progress[account.name] : null}
                    onSelect={() => setSelected(account.name)}
                    onStart={() => engine.start(account.name, launch)}
                    onStop={() => engine.stop(account.name)}
                    onOpen={() => engine.openViewer(account.name)}
                    onRemove={() => setRemoveTarget(account)}
                  />
                ))}
              </ul>
            ) : (
              <EmptyState
                filtered={accounts.length > 0}
                query={query}
                usable={usable}
                onAdd={() => setAdding(true)}
                onClear={() => setQuery("")}
              />
            )}
          </Panel>

          {/* ---- Launch bay ---- */}
          <Panel className="animate-rise overflow-hidden">
            <PanelHead icon={RocketIcon} title="Launch" />
            <div className="flex flex-col gap-4 p-3.5">
              <Field label="Performance mode" htmlFor="launch-mode">
                <select
                  id="launch-mode"
                  value={launch.mode}
                  onChange={(e) => onLaunch({ ...launch, mode: e.target.value })}
                  className="input cursor-pointer"
                >
                  {modes.map((id) => {
                    const m = describeMode(id);
                    return (
                      <option key={id} value={id}>
                        {m.spec ? `${m.label} — ${m.spec}` : m.label}
                      </option>
                    );
                  })}
                </select>
                <p className="text-[11px] leading-snug text-ink-3">{describeMode(launch.mode).note}</p>
              </Field>

              <Field
                label="Place ID"
                htmlFor="launch-place"
                hint="Leave empty to land on the Roblox home screen."
              >
                <input
                  id="launch-place"
                  type="text"
                  inputMode="numeric"
                  value={launch.place || ""}
                  onChange={(e) => onLaunch({ ...launch, place: e.target.value })}
                  className="input font-mono text-[12.5px]"
                  placeholder="8737899170"
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>

              <div className="rule-t pt-3.5">
                <Toggle
                  id="launch-multi"
                  checked={launch.multiInstance}
                  onChange={(v) => onLaunch({ ...launch, multiInstance: v })}
                  label="Multi-instance"
                  hint="Run more than one account at the same time."
                />
              </div>

              <div className="flex flex-col gap-2">
                <Button
                  variant="solid"
                  size="lg"
                  className="w-full"
                  onClick={() => selectedAccount && engine.start(selectedAccount.name, launch)}
                  disabled={!selectedAccount || Boolean(selectedBusy)}
                >
                  {selectedAccount?.running ? (
                    <>
                      <MonitorIcon className="h-4 w-4" />
                      Open viewer
                    </>
                  ) : (
                    <>
                      <PlayIcon className="h-3.5 w-3.5" />
                      Launch
                    </>
                  )}
                </Button>
                <p className="text-center text-[11px] leading-snug text-ink-3">
                  {selectedBusy
                    ? `${selectedBusy} ${selectedAccount.name}…`
                    : selectedAccount
                      ? selectedAccount.running
                        ? `${selectedAccount.name} is running`
                        : `${selectedAccount.name} is selected`
                      : "Pick an instance from the list"}
                </p>
              </div>
            </div>
          </Panel>
        </div>
      </div>

      {adding && <AddAccountModal onClose={() => setAdding(false)} onAdded={setSelected} />}

      {removeTarget && (
        <RemoveModal
          account={removeTarget}
          onClose={() => setRemoveTarget(null)}
          onConfirm={async () => {
            const name = removeTarget.name;
            setRemoveTarget(null);
            const ok = await engine.remove(name);
            if (ok && selected === name) setSelected(null);
          }}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function AccountRow({
  account,
  selected,
  busyLabel,
  progressLine,
  onSelect,
  onStart,
  onStop,
  onOpen,
  onRemove,
}) {
  const running = Boolean(account.running);
  // `where` is the cross-machine view (engine.jsx): running here, running on
  // another of your devices, or stopped everywhere.
  const where = account.where || { state: running ? "running" : "stopped", isLocal: running };
  const remote = where.state === "running" && !where.isLocal;

  let tone = "off";
  let status = "Stopped";
  if (busyLabel) {
    tone = "busy";
    status = `${busyLabel}…`;
  } else if (running) {
    tone = "live";
    status = `VNC ${account.vnc_port ?? "—"} · ADB ${account.adb_port ?? "—"}`;
  } else if (remote) {
    // Not "off": it IS running, just not here. Showing this row as stopped is
    // what made people launch the same account twice.
    tone = "busy";
    status = where.label;
  }

  return (
    <li
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={onSelect}
      onDoubleClick={() => running && onOpen()}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={`ring-focus rule-b flex cursor-pointer items-center gap-3 rounded-lg px-3.5 py-3
                  transition-colors duration-150 last:after:hidden hover:bg-raised/70
                  ${selected ? "bg-accent/8" : ""}`}
    >
      <span
        className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border font-mono
                    text-[11px] font-bold transition-colors duration-150
                    ${
                      selected
                        ? "border-accent/60 bg-accent/15 text-accent"
                        : "border-line bg-raised text-ink-3"
                    }`}
        aria-hidden="true"
      >
        {initials(account.name)}
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-ink">{account.name}</span>
          {account.arch && (
            <span
              title={`CPU architecture: ${account.arch}`}
              className={`chip ${account.arch === "arm" ? "border-accent/40 text-accent" : ""}`}
            >
              {account.arch}
            </span>
          )}
          {account.base && <span className="chip hidden md:inline-block">{account.base}</span>}
          {running && account.mode && (
            <span className="chip border-live/40 text-live">{account.mode}</span>
          )}
          {remote && (
            <span
              className="chip border-accent/40 text-accent"
              title={`This account is running on ${where.device?.deviceName || "another device"}`}
            >
              {where.device?.deviceName || "elsewhere"}
            </span>
          )}
        </div>
        <div className="mt-1 flex items-center gap-2">
          <Lamp tone={tone} pulse={tone === "busy"} size={6} />
          <span className="truncate font-mono text-[11px] text-ink-3">
            {progressLine || status}
          </span>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-0.5" onClick={(e) => e.stopPropagation()}>
        {running ? (
          <>
            {/* Deliberately NOT disabled while busy. `busyLabel` stays
                "Starting" for the whole boot -- minutes -- but QEMU's VNC
                server binds the moment the process spawns, and `running`
                flips as soon as its run.json exists. Watching the boot is
                the one thing you most want during it, and disabling this
                was the difference between "nothing is happening" and
                seeing Android come up. */}
            <IconButton label="Open viewer" tone="accent" onClick={onOpen}>
              <MonitorIcon className="h-4 w-4" />
            </IconButton>
            <IconButton label="Stop instance" onClick={onStop} disabled={Boolean(busyLabel)}>
              <StopIcon className="h-3.5 w-3.5" />
            </IconButton>
          </>
        ) : (
          <IconButton
            label={
              remote
                ? `Already ${where.label.toLowerCase()} — stop it there first`
                : "Start instance"
            }
            tone="accent"
            onClick={onStart}
            disabled={Boolean(busyLabel) || Boolean(account.creating) || remote}
          >
            <PlayIcon className="h-3.5 w-3.5" />
          </IconButton>
        )}
        <IconButton
          label="Remove account and delete its data"
          tone="danger"
          onClick={onRemove}
          disabled={Boolean(busyLabel)}
        >
          <TrashIcon className="h-4 w-4" />
        </IconButton>
      </div>
    </li>
  );
}

function EmptyState({ filtered, query, usable, onAdd, onClear }) {
  if (filtered) {
    return (
      <div className="flex flex-col items-center gap-2.5 px-4 py-14 text-center">
        <SearchIcon className="h-6 w-6 text-ink-3" />
        <p className="text-[13px] text-ink-2">
          No instance matches “<span className="font-mono">{query}</span>”
        </p>
        <Button size="sm" onClick={onClear}>
          Clear filter
        </Button>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-3 px-4 py-14 text-center">
      <LayersIcon className="h-7 w-7 text-ink-3" strokeWidth={1.4} />
      <div>
        <p className="text-[13px] font-medium text-ink">No instances yet</p>
        <p className="mx-auto mt-1 max-w-[36ch] text-[11.5px] leading-relaxed text-ink-3">
          Each account gets its own Android instance with its own storage, ports and game data.
        </p>
      </div>
      <Button variant="solid" size="sm" onClick={onAdd} disabled={!usable}>
        <PlusIcon className="h-3.5 w-3.5" />
        Add your first account
      </Button>
    </div>
  );
}

function AddAccountModal({ onClose, onAdded }) {
  const engine = useEngine();
  const [method, setMethod] = useState("browser");
  const [token, setToken] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e) => {
    e?.preventDefault();
    if (method === "paste" && !token.trim()) {
      setError("Paste your .ROBLOSECURITY cookie to continue.");
      return;
    }
    setWorking(true);
    setError("");
    const res = method === "browser" ? await engine.loginBrowser() : await engine.loginToken(token.trim());
    setWorking(false);
    if (res.ok) {
      if (res.name) onAdded(res.name);
      onClose();
    } else {
      setError(errText(res));
    }
  };

  return (
    <Modal title="Add account" onClose={onClose}>
      <div className="flex gap-1 rounded-lg border border-line bg-raised p-1">
        {[
          { id: "browser", label: "Sign in with browser" },
          { id: "paste", label: "Paste cookie" },
        ].map((option) => (
          <button
            key={option.id}
            type="button"
            onClick={() => {
              setMethod(option.id);
              setError("");
            }}
            className={`ring-focus flex-1 rounded-md px-3 py-1.5 text-[12px] font-semibold transition-colors
                        duration-150 ${
                          method === option.id
                            ? "bg-accent text-accent-ink"
                            : "text-ink-2 hover:text-ink"
                        }`}
          >
            {option.label}
          </button>
        ))}
      </div>

      <form onSubmit={submit} className="mt-4 flex flex-col gap-3">
        {method === "browser" ? (
          <p className="text-[12.5px] leading-relaxed text-ink-2">
            A browser window opens for Roblox sign-in. When it completes, the engine provisions a
            fresh Android instance named after the account.
          </p>
        ) : (
          <Field label="Roblox cookie" htmlFor="token">
            <textarea
              id="token"
              value={token}
              onChange={(e) => {
                setToken(e.target.value);
                setError("");
              }}
              className="input min-h-[86px] resize-y font-mono text-[11.5px]"
              placeholder=".ROBLOSECURITY=…"
              spellCheck={false}
            />
          </Field>
        )}

        <p className={`text-[11px] leading-relaxed ${error ? "text-danger" : "text-ink-3"}`}>
          {error || "First-time provisioning takes roughly 3–15 minutes."}
        </p>

        <div className="mt-1 flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="solid" type="submit" disabled={working}>
            {working ? (method === "browser" ? "Waiting for browser…" : "Adding…") : "Add account"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RemoveModal({ account, onClose, onConfirm }) {
  const [typed, setTyped] = useState("");
  const inputRef = useRef(null);
  const matches = typed === account.name;

  return (
    <Modal
      title="Remove account"
      tone="danger"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" onClick={onConfirm} disabled={!matches}>
            Remove and delete data
          </Button>
        </>
      }
    >
      <p className="text-[12.5px] leading-relaxed text-ink-2">
        Removing <span className="font-mono font-semibold text-ink">{account.name}</span> deletes its
        Android instance, storage and saves. This cannot be undone
        {account.running ? "; the instance is running and will be stopped first" : ""}.
      </p>
      <div className="mt-3.5">
        <Field label="Type the account name to confirm" htmlFor="confirm-name">
          <input
            id="confirm-name"
            ref={inputRef}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && matches && onConfirm()}
            className="input font-mono"
            placeholder={account.name}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
      </div>
    </Modal>
  );
}
