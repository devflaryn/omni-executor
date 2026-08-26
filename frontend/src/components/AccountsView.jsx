/* Accounts panel: one row per account, one launch bay on the right.

   The list is a scrolling canvas rather than a page that grows: the panel
   takes whatever height the window has left and every account lives inside
   it, so fifty accounts and two accounts produce the same layout and the
   launch bay never slides off the bottom of a long list. */

import { useEffect, useMemo, useState } from "react";
import { GPU_INFO, describeGpu, describeMode, errText, useEngine } from "../engine.jsx";
import {
  Button,
  Field,
  IconButton,
  Lamp,
  Modal,
  Notice,
  Panel,
  PanelHead,
} from "./ui.jsx";
import {
  AlertIcon,
  InfoIcon,
  LayersIcon,
  MonitorIcon,
  MonitorOffIcon,
  PlayIcon,
  PlusIcon,
  RocketIcon,
  SearchIcon,
  StopIcon,
  TrashIcon,
  UserPlusIcon,
  UsersIcon,
} from "./icons.jsx";
import CreateAccountModal from "./CreateAccountModal.jsx";

/** Two characters, always: one per word, or the first two letters of a single
    run-together name like "admn1b12farm2". */
function initials(name) {
  const parts = String(name).trim().split(/[\s_-]+/).filter(Boolean);
  if (!parts.length) return "?";
  const chars = parts.length > 1 ? parts.slice(0, 2).map((p) => p[0]) : [parts[0].slice(0, 2)];
  return chars.join("").toUpperCase();
}

export default function AccountsView({ active, launch, onLaunch, showToast, addRequest = 0 }) {
  const engine = useEngine();
  const { accounts, busy, progress, backend, issue, settingUp, modes } = engine;

  const [selected, setSelected] = useState(null);
  // Ticked rows, for bulk launch/stop. Separate from `selected` (the row the
  // launch bay describes): ticking is a plan, selecting is a look.
  const [checked, setChecked] = useState(() => new Set());
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [creating, setCreating] = useState(false);

  const usable = backend === true;

  // Home's "Add account" lands here.
  useEffect(() => {
    if (addRequest > 0) setAdding(true);
  }, [addRequest]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? accounts.filter((a) => a.name.toLowerCase().includes(q)) : accounts;
  }, [accounts, query]);

  // Keep the selection pointing at something real.
  useEffect(() => {
    if (selected && !accounts.some((a) => a.name === selected)) setSelected(null);
    if (!selected && accounts.length === 1) setSelected(accounts[0].name);
  }, [accounts, selected]);

  // Drop ticks for accounts that no longer exist.
  useEffect(() => {
    setChecked((prev) => {
      const next = new Set([...prev].filter((n) => accounts.some((a) => a.name === n)));
      return next.size === prev.size ? prev : next;
    });
  }, [accounts]);

  const selectedAccount = accounts.find((a) => a.name === selected) || null;
  const selectedBusy = selected ? busy[selected] : null;

  /* No confirmation dialog: Remove removes. engine.remove stops a running
     instance first and the toast reports what happened. */
  const removeAccount = async (account) => {
    const ok = await engine.remove(account.name);
    if (ok && selected === account.name) setSelected(null);
  };

  const toggleChecked = (name, on) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (on ?? !next.has(name)) next.add(name);
      else next.delete(name);
      return next;
    });
  const allVisibleChecked = visible.length > 0 && visible.every((a) => checked.has(a.name));
  const toggleAll = () =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (allVisibleChecked) visible.forEach((a) => next.delete(a.name));
      else visible.forEach((a) => next.add(a.name));
      return next;
    });

  const checkedAccounts = accounts.filter((a) => checked.has(a.name));
  const bulk = checkedAccounts.length > 0;
  const bulkStopped = checkedAccounts.filter(
    (a) => !a.running && a.where?.state !== "running" && !busy[a.name]
  );
  const bulkRunning = checkedAccounts.filter((a) => a.running && !busy[a.name]);
  const bulkBusy = checkedAccounts.some((a) => busy[a.name]);

  return (
    <div className={`min-h-0 flex-1 flex-col overflow-hidden px-5 py-5 ${active ? "flex" : "hidden"}`}>
      <div className="mx-auto flex min-h-0 w-full max-w-[1180px] flex-1 flex-col gap-4">
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

        <div className="grid min-h-0 flex-1 gap-8 lg:grid-cols-[minmax(0,1fr)_308px]">
          {/* ---- Accounts ---- */}
          <Panel className="animate-rise flex min-h-0 flex-col overflow-hidden">
            <PanelHead
              icon={UsersIcon}
              title="Accounts"
              count={accounts.length}
              right={
                <>
                  {accounts.length > 0 && (
                    <label
                      className="mr-1 flex cursor-pointer items-center gap-1.5 text-[11.5px] text-ink-3 select-none hover:text-ink-2"
                      title="Tick every account in the list"
                    >
                      <input
                        type="checkbox"
                        className="check"
                        checked={allVisibleChecked}
                        onChange={toggleAll}
                        aria-label="Select all accounts"
                      />
                      {checked.size ? `${checked.size} selected` : "Select all"}
                    </label>
                  )}
                  <div className="relative hidden sm:block">
                    <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
                    <input
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Filter"
                      aria-label="Filter accounts by name"
                      spellCheck={false}
                      className="input h-7 w-[132px] py-0 pl-8 text-[12px]"
                    />
                  </div>
                  <Button size="sm" onClick={() => setCreating(true)} disabled={!usable}>
                    <UserPlusIcon className="h-3.5 w-3.5" />
                    Create account
                  </Button>
                  <Button variant="solid" size="sm" onClick={() => setAdding(true)} disabled={!usable}>
                    <PlusIcon className="h-3.5 w-3.5" />
                    Add account
                  </Button>
                </>
              }
            />

            {/* The scrolling canvas. Every account is in here, however many
                there are; the panel itself never grows past the window. */}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {visible.length > 0 ? (
                <ul>
                  {visible.map((account) => (
                    <AccountRow
                      key={account.name}
                      account={account}
                      selected={selected === account.name}
                      checked={checked.has(account.name)}
                      onCheck={(on) => toggleChecked(account.name, on)}
                      busyLabel={busy[account.name]}
                      progressLine={busy[account.name] ? progress[account.name] : null}
                      onSelect={() => setSelected(account.name)}
                      onStart={() => engine.start(account.name, launch)}
                      onStop={() => engine.stop(account.name)}
                      onOpen={() => engine.openViewer(account.name)}
                      onHide={() => engine.hideViewer(account.name)}
                      onRemove={() => removeAccount(account)}
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
            </div>
          </Panel>

          {/* ---- Launch bay ---- */}
          <Panel className="animate-rise flex min-h-0 flex-col overflow-hidden">
            <PanelHead icon={RocketIcon} title="Launch" />
            <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-3.5">
              <Field label="Mode" htmlFor="launch-mode">
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

              {/* This used to be hidden for farming, on the grounds that
                  farming is headless by definition so the choice could not
                  take effect. That stopped being true: measured 2026-08-15 on
                  PS99, three quarters of a software farming instance's CPU is
                  llvmpipe rasterising frames nobody looks at (141% -> 72%
                  guest CPU once the host GPU does it), so farming now defaults
                  to `auto` and takes the GPU through a window it then hides.
                  Nothing appears on screen either way — what the setting now
                  trades is CPU against the VNC-based capture tools, which is a
                  real choice and belongs to the user. */}
              {(
                <Field label="Graphics" htmlFor="launch-gpu">
                  <select
                    id="launch-gpu"
                    value={launch.gpu || "auto"}
                    onChange={(e) => onLaunch({ ...launch, gpu: e.target.value })}
                    className="input cursor-pointer"
                  >
                    {Object.keys(GPU_INFO).map((id) => (
                      <option key={id} value={id}>
                        {describeGpu(id).label}
                      </option>
                    ))}
                  </select>
                  <p className="text-[11px] leading-snug text-ink-3">{describeGpu(launch.gpu || "auto").note}</p>
                </Field>
              )}

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

              <div className="rule-t flex flex-col gap-2 pt-3.5">
                {bulk ? (
                  <>
                    {/* Several rows are ticked: the bay's Mode / Graphics /
                        Place apply to all of them. */}
                    <Button
                      variant="solid"
                      size="lg"
                      className="w-full"
                      onClick={() => engine.startMany(bulkStopped.map((a) => a.name), launch)}
                      disabled={!bulkStopped.length || bulkBusy}
                    >
                      <PlayIcon className="h-3.5 w-3.5" />
                      Launch {bulkStopped.length} {bulkStopped.length === 1 ? "instance" : "instances"}
                    </Button>
                    {bulkRunning.length > 0 && (
                      <Button
                        size="lg"
                        className="w-full"
                        onClick={() => engine.stopMany(bulkRunning.map((a) => a.name))}
                        disabled={bulkBusy}
                      >
                        <StopIcon className="h-3.5 w-3.5" />
                        Stop {bulkRunning.length} running
                      </Button>
                    )}
                    <p className="text-center text-[11px] leading-snug text-ink-3">
                      {bulkBusy
                        ? "Working on the selection…"
                        : bulkStopped.length
                          ? `${checked.size} selected · ${describeMode(launch.mode).label} mode, all at once`
                          : `${checked.size} selected · all already running`}
                    </p>
                    <button
                      type="button"
                      onClick={() => setChecked(new Set())}
                      className="ring-focus mx-auto rounded px-1 text-[11px] text-ink-3 underline-offset-2 hover:text-ink hover:underline"
                    >
                      Clear selection
                    </button>
                  </>
                ) : (
                  <>
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
                          : accounts.length > 1
                            ? "Pick an account, or tick several to launch them together"
                            : "Pick an account from the list"}
                    </p>
                  </>
                )}
              </div>
            </div>
          </Panel>
        </div>
      </div>

      {adding && <AddAccountModal onClose={() => setAdding(false)} onAdded={setSelected} />}

      {creating && (
        <CreateAccountModal
          onClose={() => setCreating(false)}
          showToast={showToast}
          onCreated={() => engine.refreshList()}
        />
      )}

    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* -------------------------------------------------------------------------- */

function AccountRow({
  account,
  selected,
  checked,
  onCheck,
  busyLabel,
  progressLine,
  onSelect,
  onStart,
  onStop,
  onOpen,
  onHide,
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
    // A GPU boot has NO VNC server — QEMU refuses one beside a GL context —
    // so printing its vnc_port here named a port nothing was listening on,
    // which is the first thing anyone debugging "the viewer won't connect"
    // reaches for. `has_vnc` comes from the engine's own run.json. Say what
    // the instance actually has: a window, and the size the guest is being
    // shown at.
    const client = account.window_client;
    status =
      account.has_vnc === false
        ? `Window ${client ? `${client[0]}×${client[1]}` : "(no VNC)"} · ADB ${account.adb_port ?? "—"}`
        : `VNC ${account.vnc_port ?? "—"} · ADB ${account.adb_port ?? "—"}`;
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
      className={`ring-focus rule-b group flex cursor-pointer items-center gap-3 rounded-lg px-3 py-3
                  transition-colors duration-150 last:after:hidden hover:bg-raised/70
                  ${selected ? "bg-accent/8" : ""}`}
    >
      <input
        type="checkbox"
        className={`check shrink-0 transition-opacity ${checked ? "opacity-100" : "opacity-40 group-hover:opacity-100"}`}
        checked={checked}
        onChange={(e) => onCheck(e.target.checked)}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
        aria-label={`Select ${account.name}`}
      />
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
                seeing Android come up.

                On a GPU boot the window is ALREADY on screen by the time this
                row appears — the engine presents it at spawn so the loading
                animation is visible while Android comes up — so this button
                becomes "put it away" rather than "bring it out". Hiding never
                stops the instance; the game keeps running and rendering, and
                the same button brings it back. `window_visible` comes from the
                engine's own run.json (account_status), never guessed here: a
                window this app hid and a window the user never had are the
                same thing to look at and very different things to click. */}
            {account.window_visible ? (
              <IconButton label="Hide the window (the instance keeps running)" onClick={onHide}>
                <MonitorOffIcon className="h-4 w-4" />
              </IconButton>
            ) : (
              <IconButton label="Open viewer" tone="accent" onClick={onOpen}>
                <MonitorIcon className="h-4 w-4" />
              </IconButton>
            )}
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
          No account matches “<span className="font-mono">{query}</span>”
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
        <p className="text-[13px] font-medium text-ink">No accounts yet</p>
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
