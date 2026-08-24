/* Home: the one screen that says how things stand and offers the next move.

   The signature element is the instrument strip — one lamp per account, lit
   when that account is running here, dim when stopped, pulsing while it
   boots. It is the app's own Lamp device scaled into a readout, so "how many
   are on right now" is a glance, not a number to read. Everything else on the
   page is quiet: three tiles, one row of actions, two lists. */

import { useMemo } from "react";
import { useEngine } from "../engine.jsx";
import { useEditorStore } from "../editorStore.jsx";
import { Button, Lamp, PanelHead } from "./ui.jsx";
import {
  CodeIcon,
  FileIcon,
  GearIcon,
  PlayIcon,
  PlusIcon,
  StopIcon,
  UsersIcon,
} from "./icons.jsx";

function greeting(name) {
  const h = new Date().getHours();
  const part = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  return name && name !== "Guest" ? `${part}, ${name}` : part;
}

function ago(ts) {
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export default function HomeView({ active, profile, launch, onGo, onAddAccount, showToast }) {
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

  const recent = editor.recent.slice(0, 6);
  const instances = accounts.slice(0, 8);

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1080px] flex-col gap-6">
        {/* Greeting + engine */}
        <div className="flex items-end justify-between gap-4 px-1">
          <div>
            <h2 className="text-[22px] font-semibold tracking-[-0.01em] text-ink">{greeting(profile?.name)}</h2>
            <p className="mt-1 text-[12.5px] text-ink-3">
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
        <div className="grid grid-cols-3 gap-3">
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
        <div className="flex flex-wrap items-center gap-2 px-1">
          <Button variant="solid" onClick={newScript}>
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

        {/* Lists */}
        <div className="grid items-start gap-6 lg:grid-cols-2">
          <section>
            <PanelHead icon={FileIcon} title="Recent scripts" count={editor.tabs.length} />
            <ul className="py-1">
              {recent.map((tab) => (
                <li key={tab.id}>
                  <button
                    type="button"
                    onClick={() => openScript(tab.id)}
                    className="ring-focus rule-b flex w-full items-center gap-3 rounded-lg px-3.5 py-2.5 text-left
                               transition-colors duration-150 last:after:hidden hover:bg-raised/70"
                  >
                    <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-3" />
                    <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-ink">{tab.name}</span>
                    <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
                      {tab.content.split("\n").length} ln · {ago(tab.updatedAt)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <section>
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
              <ul className="py-1">
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
                        className="ring-focus rule-b flex w-full items-center gap-3 rounded-lg px-3.5 py-2.5 text-left
                                   transition-colors duration-150 last:after:hidden hover:bg-raised/70"
                      >
                        <Lamp tone={tone} pulse={tone === "busy"} size={6} />
                        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-ink">{a.name}</span>
                        {a.running && a.mode && <span className="chip border-live/40 text-live">{a.mode}</span>}
                        <span className="shrink-0 font-mono text-[10.5px] text-ink-3">{status}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <div className="px-3.5 py-8 text-center">
                <p className="text-[12.5px] text-ink-2">No accounts yet.</p>
                <p className="mt-1 text-[11.5px] text-ink-3">Each one gets its own Android instance.</p>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
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
    <div className="rounded-xl border border-line bg-surface px-4 py-3.5" aria-label="Instances at a glance">
      <div className="flex flex-wrap gap-1.5">
        {cells.map((c) => (
          <span
            key={c.key}
            title={c.title}
            className={`h-[18px] w-[18px] rounded-[5px] transition-colors duration-300 ${
              c.state === "live"
                ? "bg-live shadow-[0_0_8px_rgba(111,181,131,0.55)]"
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

function Tile({ label, value, note, tone, icon: Icon, onClick }) {
  return (
    <button type="button" className="tile" onClick={onClick}>
      <span className="flex items-center gap-1.5">
        {Icon && <Icon className="h-3.5 w-3.5 text-ink-3" />}
        <span className="silk text-ink-3">{label}</span>
      </span>
      <span className="flex items-baseline gap-2">
        <span className={`font-mono text-[26px] leading-none font-semibold ${tone === "live" ? "text-live" : "text-ink"}`}>
          {value}
        </span>
        {note && <span className="text-[11px] text-ink-3">{note}</span>}
      </span>
    </button>
  );
}
