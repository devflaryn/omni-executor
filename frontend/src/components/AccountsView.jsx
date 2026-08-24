/* Instances panel: one row per account, one launch bay on the right. */

import { useEffect, useMemo, useRef, useState } from "react";
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
  Toggle,
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

export default function AccountsView({ active, launch, onLaunch, showToast, addRequest = 0 }) {
  const engine = useEngine();
  const { accounts, busy, progress, backend, issue, settingUp, modes } = engine;

  const [selected, setSelected] = useState(null);
  // Ticked rows, for bulk launch/stop. Separate from `selected` (the row the
  // launch bay describes): ticking is a plan, selecting is a look.
  const [checked, setChecked] = useState(() => new Set());
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);

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
                  {accounts.length > 0 && (
                    <label
                      className="mr-1 flex cursor-pointer items-center gap-1.5 text-[11.5px] text-ink-3 select-none hover:text-ink-2"
                      title="Tick every instance in the list"
                    >
                      <input
                        type="checkbox"
                        className="check"
                        checked={allVisibleChecked}
                        onChange={toggleAll}
                        aria-label="Select all instances"
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
          </Panel>

          {/* ---- Launch bay ---- */}
          <Panel className="animate-rise overflow-hidden">
            <PanelHead icon={RocketIcon} title="Launch" />
            <div className="flex flex-col gap-4 p-3.5">
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

              <div className="rule-t pt-3.5">
                <Toggle
                  id="launch-multi"
                  checked={launch.multiInstance}
                  onChange={(v) => onLaunch({ ...launch, multiInstance: v })}
                  label="Multi-instance"
                  hint="Run more than one account at the same time."
                />
              </div>

              {/* Only on an engine that has the command (engine.jsx gates on
                  the handshake's advertised list). Its home is here, under the
                  settings it is warmed for, because those settings are exactly
                  what decides whether a warm instance is usable at all. */}
              {engine.poolSupported && (
                <div className="rule-t pt-3.5">
                  <WarmPool active={active} launch={launch} onLaunch={onLaunch} />
                </div>
              )}

              <div className="flex flex-col gap-2">
                {bulk ? (
                  <>
                    {/* Several rows are ticked: the bay's Mode / Graphics /
                        Place apply to all of them. A bulk launch is
                        multi-instance by definition, whatever the toggle says. */}
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
                            ? "Pick an instance, or tick several to launch them together"
                            : "Pick an instance from the list"}
                    </p>
                  </>
                )}
              </div>
            </div>
          </Panel>
        </div>
      </div>

      {adding && <AddAccountModal onClose={() => setAdding(false)} onAdded={setSelected} />}

    </div>
  );
}

/* -------------------------------------------------------------------------- */

/* Is the pool on? FARMING SAYS YES UNLESS THE USER SAYS OTHERWISE.

   `keepWarm` is deliberately tri-state. `undefined` means "nobody has had an
   opinion", and the right answer to that depends entirely on the mode:

     farming  is fifty launches, and a cold boot is 35-60 s of every one of
              them. Leaving the pool off by default made "it always cold
              boots" the default experience of the mode built for volume, on
              the one host where a warm pool is the ONLY fast path (WHPX
              cannot snapshot a VM).
     gaming   is one instance somebody is about to watch. A warm slot for it
              is a second guest holding its memory so that one launch can be
              quick, which is a bad trade for a single player.

   Once the toggle is touched it stores a real boolean and that sticks, in
   both directions -- so this decides the default, never the setting. Reading
   the mode at render time (rather than writing a value in when the mode
   changes) is what lets switching gaming -> farming turn the pool on without
   overwriting a preference the user has already expressed. */
export function keepWarmDefault(launch) {
  if (launch?.keepWarm === undefined || launch?.keepWarm === null) {
    return launch?.mode === "farming";
  }
  return Boolean(launch.keepWarm);
}

/* Keep one instance warm.

   The engine can hold instances pre-booted to the account-free ready point;
   a launch then adopts one instead of booting. Measured on this box: 35-60 s
   of boot against 0.093 s to adopt — and on Windows it is the only fast path
   there is, because WHPX cannot snapshot a VM.

   The control lives directly under Mode / Graphics / Place ID because those
   settings decide whether a warm instance is usable AT ALL: a slot is only
   adopted by a launch that resolves to the same machine, and the place sizes
   it (the engine floors guest RAM per game — PS99 asks for 3072 MB). So this
   warms the pool for the settings shown above it, re-warms when they change,
   and never claims a slot is ready unless it was warmed for what the launch
   bay says right now — the alternative is a panel reporting "instant" while
   every launch quietly cold-boots. */
function WarmPool({ active, launch, onLaunch }) {
  const engine = useEngine();
  const { doctor, pool, poolBusy, poolError, refreshPool, startPool, stopPool } = engine;

  const enabled = keepWarmDefault(launch);
  const mode = launch.mode;
  const place = String(launch.place || "").trim();
  // Normalised exactly as main.py normalises it. If the two sides disagreed
  // about what "no policy" means, the receipt would never match the settings
  // and the pool would re-warm itself forever.
  const gpu = GPU_INFO[launch.gpu] ? launch.gpu : "";
  const specKey = `${mode}|${place}|${gpu}`;

  const warmedFor = pool?.warmed_for || null;
  const warmedKey = warmedFor ? `${warmedFor.mode}|${warmedFor.place}|${warmedFor.gpu}` : null;
  const matched = warmedKey !== null && warmedKey === specKey;
  const managerDown = Boolean(pool?.configured) && pool?.manager_alive === false;
  const needsRestart = Boolean(pool) && (!pool.configured || pool.manager_alive === false);

  // What we last asked the backend to warm, and how many times. Both are refs:
  // nothing here should re-render, and both survive a status poll.
  const asked = useRef({ key: null, tries: 0 });
  const autoStopped = useRef(false);

  // Adopt the live pool's own receipt on arrival, so opening the app onto a
  // pool that is already correct does not re-issue a start.
  useEffect(() => {
    if (asked.current.key === null && warmedKey && pool?.manager_alive) {
      asked.current = { key: warmedKey, tries: 1 };
    }
  }, [pool, warmedKey]);

  useEffect(() => {
    if (!enabled) return undefined;
    // Debounced, because Place ID is a free-text field: without this, typing
    // one id would stop and re-warm the pool once per keystroke, and each of
    // those is a real virtual machine powered off and booted again.
    const timer = setTimeout(() => {
      const first = asked.current.key !== specKey;
      // One automatic retry per settings change, no more. A pool that will not
      // come up must never become a subprocess every couple of seconds; the
      // status line says what is wrong and the toggle is the retry.
      const retry = needsRestart && !poolError && asked.current.tries < 2;
      if (!first && !retry) {
        refreshPool();
        return;
      }
      asked.current = { key: specKey, tries: first ? 1 : asked.current.tries + 1 };
      startPool(1, mode, place, gpu);
    }, 1500);
    return () => clearTimeout(timer);
  }, [enabled, specKey, needsRestart, poolError, mode, place, gpu, refreshPool, startPool]);

  /* The toggle is off, and a pool THIS app warmed is still configured — a stop
     that did not land, or an app that went away before it could. Give the
     memory back. Once per mount, and only when there is a receipt: a pool
     somebody started from the CLI is not this app's to shut down. */
  useEffect(() => {
    if (enabled || autoStopped.current) return;
    if (!pool?.configured || !pool.warmed_for) return;
    autoStopped.current = true;
    stopPool();
  }, [enabled, pool, stopPool]);

  /* The only timer. 30 s because nothing here moves faster than a boot, and
     only while this panel is on screen and the pool is on; it dies with the
     component. */
  useEffect(() => {
    if (!enabled || !active) return undefined;
    const timer = setInterval(refreshPool, 30000);
    return () => clearInterval(timer);
  }, [enabled, active, refreshPool]);

  const toggle = (on) => {
    onLaunch({ ...launch, keepWarm: on });
    if (on) {
      autoStopped.current = false;
      asked.current = { key: null, tries: 0 };
    } else {
      asked.current = { key: null, tries: 0 };
      stopPool();
    }
  };

  // `ready_matching`, never `ready` — and only when the pool was warmed for the
  // settings above. A ready slot for other settings is invisible to a launch.
  const ready = matched ? Number(pool?.ready_matching) || 0 : 0;

  let tone = "off";
  let status = "Off — every launch boots from cold";
  if (enabled) {
    if (poolError) {
      tone = "fault";
      status = `Couldn't keep one warm — ${poolError}`;
    } else if (poolBusy || !pool) {
      tone = "busy";
      status = "Checking…";
    } else if (pool.ok === false) {
      tone = "fault";
      status = errText(pool);
    } else if (managerDown) {
      // No count in this state, however many slots are up: nothing is tending
      // them, so "instant" would go on being printed while the pool drains.
      tone = "fault";
      status = "The pool manager isn't running — switch this off and on again";
    } else if (ready > 0) {
      tone = "live";
      status = `${ready} ready — the next launch is instant`;
    } else if (warmedKey && !matched) {
      tone = "busy";
      status = "Re-warming for these settings…";
    } else {
      tone = "busy";
      status = "Warming — this first boot still takes a few minutes";
    }
  } else if (pool?.configured && pool.warmed_for) {
    // Off, but a pool THIS app warmed is still holding the memory (the same
    // condition the auto-stop above acts on). Saying "off" here would be the
    // one lie this panel can tell that costs gigabytes.
    tone = poolError ? "fault" : "busy";
    status = poolError
      ? `A warm instance is still running — ${poolError}`
      : "Off — shutting the warm instance down…";
  }

  const fits = Number.isFinite(doctor?.scratch_fits_instances)
    ? doctor.scratch_fits_instances
    : null;

  return (
    <div className="flex flex-col gap-2">
      <Toggle
        id="launch-warm"
        checked={enabled}
        onChange={toggle}
        label="Keep an instance warm"
        hint="Pre-boots one now, so the next launch skips the boot entirely."
      />
      <div className="flex items-center gap-2">
        <Lamp tone={tone} pulse={tone === "busy"} size={6} />
        {/* The engine's own failures are prose and can run to paragraphs (a
            missing base image prints the whole install checklist), so the line
            stays one line and the full text lives in the tooltip. */}
        <span title={status} className="truncate font-mono text-[11px] text-ink-3">
          {status}
        </span>
      </div>
      <p className="text-[11px] leading-snug text-ink-3">
        A warm instance stays booted while it waits, holding its memory and its disk scratch
        until you switch this off.
        {fits != null && ` Free disk here fits about ${fits} instance${fits === 1 ? "" : "s"}.`}
      </p>
    </div>
  );
}

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
