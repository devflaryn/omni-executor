/* Home: the one screen that says how things stand and offers the next move.

   The signature element is the instrument strip — one lamp per account, lit
   when that account is running here, dim when stopped, pulsing while it
   boots. It is the app's own Lamp device scaled into a readout, so "how many
   are on right now" is a glance, not a number to read. Everything else on the
   page is quiet: three tiles, one row of actions, two lists. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { useEngine } from "../engine.jsx";
import { useEditorStore } from "../editorStore.jsx";
import { Button, Lamp, MiniSwitch, PanelHead } from "./ui.jsx";
import {
  CodeIcon,
  FileIcon,
  GearIcon,
  PlayIcon,
  PlusIcon,
  RocketIcon,
  StopIcon,
  UsersIcon,
} from "./icons.jsx";

/* Who to greet.

   The Omni account's username first — that is the identity the whole app hangs
   off, and it is unique. `profile.name` is the local Settings display name and
   only wins if the user actually set one, since it defaults to "Guest" and
   greeting a signed-in account as Guest is worse than saying nothing. An
   account made before usernames existed has none, so the email's local part is
   the last stop before the bare greeting. */
function displayName(auth, profile) {
  const local = profile?.name?.trim();
  if (local && local !== "Guest") return local;
  if (auth?.username) return auth.username;
  const email = auth?.email || "";
  return email.includes("@") ? email.slice(0, email.indexOf("@")) : "";
}

function greeting(name) {
  const h = new Date().getHours();
  const part = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  return name ? `${part}, ${name}` : part;
}

function ago(ts) {
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export default function HomeView({ active, auth, profile, launch, onGo, onAddAccount, showToast }) {
  const engine = useEngine();
  const editor = useEditorStore();
  const { accounts, running, runningElsewhere, busy, health } = engine;

  const stopped = useMemo(
    () => accounts.filter((a) => !a.running && a.where?.state !== "running" && !busy[a.name]),
    [accounts, busy]
  );
  const usable = engine.backend === true && health.tone !== "fault";

  const launchAll = () => {
    if (!stopped.length) {
      showToast(accounts.length ? "Everything is already running." : "Add an account first.", "info");
      return;
    }
    engine.startMany(stopped.map((a) => a.name), launch);
  };

  const newScript = () => {
    editor.newTab();
    onGo("editor");
  };

  const openScript = (id) => {
    editor.selectTab(id);
    onGo("editor");
  };

  // An autoexec row opens (or refocuses) its disk-backed editor tab.
  const editAutoexec = async (name) => {
    await editor.openAutoexec(name);
    onGo("editor");
  };

  const recent = editor.recent.slice(0, 6);
  const instances = accounts.slice(0, 8);

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-7 pt-2 pb-7 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1120px] flex-col gap-7">
        {/* Greeting + engine */}
        <div className="flex items-end justify-between gap-4 px-0.5">
          <div>
            <h2 className="flex flex-wrap items-center gap-3 text-[32px] leading-[1.1] font-bold tracking-[-0.025em] text-ink">
              {greeting(displayName(auth, profile))}
              <TierBadge subscription={auth?.subscription} onGo={onGo} />
            </h2>
            <p className="mt-2 text-[13.5px] text-ink-3">
              {accounts.length === 0
                ? "Add an account to get an instance of your own."
                : running.length
                  ? `${running.length} of ${accounts.length} running on this machine.`
                  : `${accounts.length} ${accounts.length === 1 ? "account" : "accounts"}, none running right now.`}
            </p>
          </div>
          <div className="flex items-center gap-2 pb-1" title={`Engine: ${health.label}`}>
            <Lamp tone={health.tone} pulse={health.tone === "busy"} />
            <span className="silk text-ink-3">{health.label}</span>
          </div>
        </div>

        {/* Instrument strip */}
        <InstrumentStrip accounts={accounts} busy={busy} />

        {/* Stat tiles */}
        <div className="grid grid-cols-3 gap-4">
          <Tile label="Accounts" value={accounts.length} icon={UsersIcon} onClick={() => onGo("accounts")} />
          <Tile
            label="Running now"
            value={running.length}
            note={runningElsewhere.length ? `+${runningElsewhere.length} elsewhere` : null}
            tone={running.length ? "live" : "off"}
            icon={PlayIcon}
            onClick={() => onGo("accounts")}
          />
          <Tile label="Scripts open" value={editor.tabs.length} icon={CodeIcon} onClick={() => onGo("editor")} />
        </div>

        {/* Quick actions */}
        <div className="flex flex-wrap items-center gap-2.5 px-0.5">
          <Button onClick={newScript}>
            <PlusIcon className="h-3.5 w-3.5" />
            New script
          </Button>
          <Button
            variant="solid"
            onClick={launchAll}
            disabled={!usable || !stopped.length}
            title={stopped.length ? `Start ${stopped.length} stopped ${stopped.length === 1 ? "instance" : "instances"} in ${launch.mode} mode` : "Nothing to launch"}
          >
            <PlayIcon className="h-3.5 w-3.5" />
            Launch all{stopped.length ? ` (${stopped.length})` : ""}
          </Button>
          {running.length > 0 && (
            <Button onClick={() => engine.stopMany(running.map((a) => a.name))}>
              <StopIcon className="h-3 w-3" />
              Stop all
            </Button>
          )}
          <Button onClick={onAddAccount} disabled={!usable}>
            <UsersIcon className="h-3.5 w-3.5" />
            Add account
          </Button>
          <Button onClick={() => onGo("settings")} className="ml-auto">
            <GearIcon className="h-3.5 w-3.5" />
            Settings
          </Button>
        </div>

        {/* Lists. The left column is the two script lists, stacked: Autoexec
            on top because it is standing configuration — what WILL run,
            everywhere, every launch — and Recent scripts under it because that
            is only a history of what was edited. */}
        <div className="grid items-start gap-6 lg:grid-cols-2">
          <div className="flex flex-col gap-7">
            <AutoexecList active={active} showToast={showToast} onEdit={editAutoexec} />

            <section className="card">
              <PanelHead icon={FileIcon} title="Recent scripts" count={editor.tabs.length} />
              <ul className="p-1.5">
                {recent.map((tab) => (
                  <li key={tab.id}>
                    <button
                      type="button"
                      onClick={() => openScript(tab.id)}
                      className="ring-focus rule-b flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left
                                 transition-colors duration-150 last:after:hidden hover:bg-raised/60"
                    >
                      {tab.kind === "autoexec" ? (
                        <RocketIcon className="h-3.5 w-3.5 shrink-0 text-ink-3" />
                      ) : (
                        <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-3" />
                      )}
                      <span className="min-w-0 flex-1 truncate font-mono text-[13.5px] text-ink">{tab.name}</span>
                      <span className="shrink-0 font-mono text-[11.5px] text-ink-3">
                        {tab.content.split("\n").length} ln · {ago(tab.updatedAt)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          </div>

          <section className="card">
            <PanelHead
              icon={UsersIcon}
              title="Instances"
              count={accounts.length}
              right={
                accounts.length > instances.length && (
                  <Button size="sm" onClick={() => onGo("accounts")}>
                    All {accounts.length}
                  </Button>
                )
              }
            />
            {instances.length ? (
              <ul className="p-1.5">
                {instances.map((a) => {
                  const busyLabel = busy[a.name];
                  const remote = a.where?.state === "running" && !a.where.isLocal;
                  const tone = busyLabel ? "busy" : a.running ? "live" : remote ? "busy" : "off";
                  const status = busyLabel ? `${busyLabel}…` : a.running ? "Running" : remote ? a.where.label : "Stopped";
                  return (
                    <li key={a.name}>
                      <button
                        type="button"
                        onClick={() => onGo("accounts")}
                        className="ring-focus rule-b flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left
                                   transition-colors duration-150 last:after:hidden hover:bg-raised/60"
                      >
                        <Lamp tone={tone} pulse={tone === "busy"} size={6} />
                        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-ink">{a.name}</span>
                        {a.running && a.mode && <span className="chip chip-live capitalize">{a.mode}</span>}
                        <span className="shrink-0 font-mono text-[11.5px] text-ink-3">{status}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <div className="px-4 py-9 text-center">
                <p className="text-[13.5px] text-ink-2">No accounts yet.</p>
                <p className="mt-1 text-[12.5px] text-ink-3">Each one gets its own Android instance.</p>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

/* The tier, next to the name it applies to.

   A chip rather than another word in the sentence: "Good night, berat Premium"
   parses as a two-word name for a beat, and the whole point of the badge is to
   be readable at a glance without being read. Premium takes the accent colour
   and its remaining days as a tooltip; Free stays in the quiet chip default and
   is a button through to Settings, where the key that changes it is redeemed. */
function TierBadge({ subscription, onGo }) {
  const premium = subscription?.tier === "premium";
  const days = subscription?.daysRemaining;
  const title = premium
    ? subscription.plan === "lifetime"
      ? "Lifetime plan"
      : `${subscription.planLabel || subscription.plan} · ${days} day${days === 1 ? "" : "s"} left`
    : "Free plan — redeem a key in Settings to go premium";

  if (premium) {
    return (
      <span className="chip chip-premium" title={title}>
        Premium
      </span>
    );
  }
  return (
    <button type="button" className="chip ring-focus hover:text-ink" title={title} onClick={() => onGo("settings")}>
      Free
    </button>
  );
}

/* One cell per account. Lit = running here, pulsing = booting/stopping,
   dim = stopped. Empty state draws a few ghost cells so the readout still
   reads as an instrument and not a gap. */
function InstrumentStrip({ accounts, busy }) {
  const cells = accounts.length
    ? accounts.map((a) => ({
        key: a.name,
        title: `${a.name} — ${busy[a.name] ? busy[a.name] : a.running ? "running" : "stopped"}`,
        state: busy[a.name] ? "busy" : a.running ? "live" : "off",
      }))
    : Array.from({ length: 6 }, (_, i) => ({ key: i, title: "", state: "ghost" }));

  return (
    <div className="card px-4.5 py-4" aria-label="Instances at a glance">
      <div className="flex flex-wrap gap-1.5">
        {cells.map((c) => (
          <span
            key={c.key}
            title={c.title}
            className={`h-[20px] w-[20px] rounded-[7px] transition-colors duration-300 ${
              c.state === "live"
                ? "bg-live shadow-[0_0_9px_color-mix(in_srgb,var(--color-live)_55%,transparent)]"
                : c.state === "busy"
                  ? "animate-pulse bg-accent/80"
                  : c.state === "ghost"
                    ? "border border-dashed border-line"
                    : "bg-raised"
            }`}
          />
        ))}
      </div>
    </div>
  );
}

/* Autoexec: the scripts every instance runs at session start.

   Built exactly like Recent scripts below it — a PanelHead over a list of
   rows on the bare sheet, no card — because the two are the same kind of
   thing seen from two angles: scripts that will run, and scripts that were
   edited. The only difference the row carries is the run order, which is what
   an autoexec folder is actually FOR: the number on the left is the position
   the file takes in the sequence, not decoration.

   TWO SWITCHES, and they are different questions. The one in the header arms
   the whole folder; the one on each row arms that script. Neither DELETES
   anything — a script you have switched off keeps its contents, keeps its
   place in the run order, and can still be opened and edited — because the
   thing people actually want here is "stop this one running for a bit", and
   making them delete a file to get it is how work gets lost.

   ⚠ BOTH LAND ON THE NEXT LAUNCH, not on instances already running, and the
   row says so. The scripts do not live on this machine at run time: they live
   in the exec server's channel for the account, and only a launch rewrites
   it. */
function AutoexecList({ active, showToast, onEdit }) {
  const [scripts, setScripts] = useState([]);
  const [master, setMaster] = useState(true);
  const [busy, setBusy] = useState(null);

  const refresh = useCallback(async () => {
    const res = await api("list_autoexec");
    if (res?.ok) {
      setScripts(res.scripts || []);
      setMaster(res.master !== false);
    }
  }, []);

  // Create an empty file (uniquely named, mid-sequence) and edit it at once;
  // a double-click on its tab renames file and run position in one move.
  const newScript = async () => {
    const taken = new Set(scripts.map((s) => s.name));
    let name = "50-script.lua";
    for (let i = 2; taken.has(name); i += 1) name = `50-script-${i}.lua`;
    const res = await api("save_autoexec", name, "");
    if (!res?.ok) {
      showToast?.(`Could not create ${name}: ${res?.message || res?.error || "unknown"}`, "error");
      return;
    }
    refresh();
    onEdit?.(name);
  };

  /* Optimistic, then reconciled. The rename is fast, but the switch has to
     move under the finger or it reads as broken; refresh() afterwards is what
     makes a failed rename snap back rather than lie. */
  const toggleMaster = async (next) => {
    setMaster(next);
    const res = await api("set_autoexec_master", next);
    if (!res?.ok) {
      showToast?.(res?.message || "Could not save that setting", "error");
    } else if (res.message) {
      showToast?.(res.message, next ? "info" : "warn");
    }
    refresh();
  };

  const toggleScript = async (name, next) => {
    setBusy(name);
    setScripts((prev) => prev.map((s) => (s.name === name ? { ...s, enabled: next } : s)));
    const res = await api("set_autoexec_enabled", name, next);
    if (!res?.ok) {
      showToast?.(
        `Could not switch ${name} ${next ? "on" : "off"}: ${res?.message || res?.error || "unknown"}`,
        "error"
      );
    }
    setBusy(null);
    refresh();
  };

  useEffect(() => {
    if (active) refresh();
  }, [active, refresh]);

  const openFolder = async () => {
    const res = await api("open_autoexec_folder");
    showToast?.(
      res?.ok
        ? "Opened autoexec folder — drop .lua files here to auto-run them at start"
        : `Could not open folder: ${res?.message || res?.error || "unknown"}`,
      res?.ok ? "info" : "error"
    );
    setTimeout(refresh, 1200);
  };

  /* The count in the header is what will RUN, not what is in the folder.
     "Autoexec 3" over a list where one is switched off would be a lie in the
     one place people look to answer "what is about to happen". */
  const armed = scripts.filter((s) => s.enabled !== false).length;

  return (
    <section className="card">
      <PanelHead
        icon={RocketIcon}
        title="Autoexec"
        count={master ? armed : 0}
        right={
          <>
            <MiniSwitch
              checked={master}
              onChange={toggleMaster}
              label="Run autoexec scripts"
              title={
                master
                  ? "Autoexec is on — switch off to stop every script running"
                  : "Autoexec is off — nothing auto-runs at start"
              }
            />
            <Button size="sm" onClick={newScript}>
              New
            </Button>
            <Button size="sm" onClick={refresh}>
              Refresh
            </Button>
            <Button size="sm" onClick={openFolder}>
              Open folder
            </Button>
          </>
        }
      />
      {!master && scripts.length > 0 && (
        <p className="px-4 pt-3 text-[12.5px] leading-relaxed text-ink-3">
          Autoexec is <span className="text-ink-2">off</span>. Nothing in this folder runs — the
          scripts are all still here. Takes effect on the next launch.
        </p>
      )}
      {scripts.length ? (
        <ul className="p-1.5">
          {scripts.map((s, i) => {
            const on = s.enabled !== false;
            const live = on && master;
            return (
              <li key={s.name}>
                <div
                  className="rule-b flex w-full items-center gap-3 rounded-xl px-3 py-2.5
                             transition-colors duration-150 last:after:hidden hover:bg-raised/60"
                >
                  <span className="w-3.5 shrink-0 text-right font-mono text-[11.5px] text-ink-3">
                    {live ? i + 1 : "—"}
                  </span>
                  <button
                    type="button"
                    onClick={() => onEdit?.(s.name)}
                    title={
                      live
                        ? `${s.name} — runs in every instance at start · click to edit`
                        : `${s.name} — switched off, will not run · click to edit`
                    }
                    className={`ring-focus min-w-0 flex-1 truncate rounded-lg text-left font-mono text-[13.5px]
                                ${live ? "text-ink" : "text-ink-3 line-through decoration-ink-3/50"}`}
                  >
                    {s.name}
                  </button>
                  <span className="shrink-0 font-mono text-[11.5px] text-ink-3">
                    {live ? "every instance" : "off"}
                  </span>
                  <MiniSwitch
                    checked={on}
                    disabled={busy === s.name}
                    onChange={(next) => toggleScript(s.name, next)}
                    label={`Run ${s.name}`}
                    title={
                      on
                        ? `Switch ${s.name} off — it stays in the folder`
                        : `Switch ${s.name} back on`
                    }
                  />
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="px-4 py-9 text-center">
          <p className="text-[13.5px] text-ink-2">No autoexec scripts yet.</p>
          <p className="mx-auto mt-1 max-w-[40ch] text-[12.5px] leading-relaxed text-ink-3">
            Open the folder and drop <span className="font-mono text-ink-2">.lua</span> files in — they
            run in filename order, in every instance, right after it starts.
          </p>
        </div>
      )}
    </section>
  );
}

function Tile({ label, value, note, tone, icon: Icon, onClick }) {
  return (
    <button type="button" className="tile" onClick={onClick}>
      <span className="flex items-center gap-1.5">
        {Icon && <Icon className="h-3.5 w-3.5 text-ink-3" />}
        <span className="silk text-ink-3">{label}</span>
      </span>
      <span className="flex items-baseline gap-2">
        <span className={`text-[32px] leading-none font-bold tracking-[-0.03em] ${tone === "live" ? "text-live" : "text-ink"}`}>
          {value}
        </span>
        {note && <span className="text-[12px] text-ink-3">{note}</span>}
      </span>
    </button>
  );
}
