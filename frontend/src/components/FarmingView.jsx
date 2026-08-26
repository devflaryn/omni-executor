/* Farming: the console for running many instances unattended.

   Premium only. The gate here is PRESENTATION, not enforcement — it hides a
   view, which is worth nothing against anyone who does not want to be stopped.
   That is acceptable today only because the supervisor does not exist yet, so
   there is no privileged work to reach. The moment it lands, the server has to
   refuse it for a free account the way every other paid path does; see the
   comments left on accounts.routes.js and execBridge.js guiGate, which are the
   two places that lesson was already learned.

   What is real here and what is not, deliberately:

     Fleet     REAL. Live instance state straight from the engine, and a
               membership set that genuinely persists to settings.json.
     Health    REAL, read-only. Counted from the same engine state.
     Schedule  INERT, and says so. The controls are disabled rather than
               wired to a store, because a schedule that accepts 09:00 and
               then never fires is a worse lie than a control that is
               visibly not ready. Same reason there is no "crashed
               accounts" list: the engine can detect a dead client
               (verify_client_survived, the logcat crash matcher) but none
               of it is on the GUI bridge, so every row would be invented. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { loadSettings, saveSettings } from "../api.js";
import { useEngine } from "../engine.jsx";
import { Button, Lamp, Notice, Panel, PanelHead } from "./ui.jsx";
import {
  ClockIcon,
  GridIcon,
  HeartPulseIcon,
  InfoIcon,
  LockIcon,
  UsersIcon,
} from "./icons.jsx";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function FarmingView({ active, auth, launch, onGo, showToast }) {
  const premium = auth?.subscription?.tier === "premium";

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1080px] flex-col gap-6">
        {premium ? (
          <FarmConsole launch={launch} showToast={showToast} />
        ) : (
          <FarmingLocked subscription={auth?.subscription} onGo={onGo} />
        )}
      </div>
    </div>
  );
}

/* The free tier's view of Farming. Not a nag and not a blank wall: it says
   what the section does, so the tab earns its place in the rail even for
   someone who cannot open it. An expired plan gets a different sentence from
   one that never had a plan — "renew" and "buy" are not the same prompt. */
function FarmingLocked({ subscription, onGo }) {
  const lapsed = Boolean(subscription?.plan);

  return (
    <div className="flex flex-col items-center gap-5 px-4 py-20 text-center">
      <span
        className="flex h-12 w-12 items-center justify-center rounded-2xl border border-premium/40 bg-premium/8"
        aria-hidden="true"
      >
        <LockIcon className="h-5 w-5 text-premium" strokeWidth={1.5} />
      </span>

      <div>
        <p className="flex items-center justify-center gap-2.5 text-[15px] font-semibold text-ink">
          Farming
          <span className="chip border-premium/45 bg-premium/8 text-premium">Premium</span>
        </p>
        <p className="mx-auto mt-2 max-w-[46ch] text-[12.5px] leading-relaxed text-ink-3">
          {lapsed
            ? "Your plan has expired, so Farming is closed. Redeem a key to pick it back up — your accounts and scripts are untouched."
            : "Run your accounts unattended. Included with a premium plan."}
        </p>
      </div>

      <ul className="mx-auto flex w-full max-w-[420px] flex-col gap-2.5 text-left">
        {[
          ["Restore what crashes", "Notices an instance whose client died and brings it back."],
          ["Open and close on a schedule", "Start in the morning, stop at night, without being at the machine."],
          ["Drive the whole fleet", "Launch, stop and watch every account from one place."],
        ].map(([title, detail]) => (
          <li key={title} className="rule-b flex items-start gap-3 px-1 py-2.5 last:after:hidden">
            <span className="mt-[5px] h-[6px] w-[6px] shrink-0 rounded-full bg-premium" />
            <span className="min-w-0">
              <span className="block text-[12.5px] font-medium text-ink">{title}</span>
              <span className="block text-[11.5px] leading-relaxed text-ink-3">{detail}</span>
            </span>
          </li>
        ))}
      </ul>

      <Button variant="solid" onClick={() => onGo("settings")}>
        {lapsed ? "Renew in Settings" : "Redeem a key"}
      </Button>
    </div>
  );
}

/* The premium console. */
function FarmConsole({ launch, showToast }) {
  const engine = useEngine();
  const { accounts, busy } = engine;
  // Which accounts belong to the farm. Persisted so the choice survives a
  // restart — and so the supervisor has something to read when it exists.
  const [members, setMembers] = useState(null); // null until settings load

  useEffect(() => {
    loadSettings().then((s) => {
      const saved = Array.isArray(s?.farming?.members) ? s.farming.members : [];
      setMembers(new Set(saved));
    });
  }, []);

  const persist = useCallback((next) => {
    setMembers(next);
    saveSettings({ farming: { members: [...next] } });
  }, []);

  const toggle = (name) => {
    const next = new Set(members);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    persist(next);
  };

  const inFarm = useMemo(
    () => (members ? accounts.filter((a) => members.has(a.name)) : []),
    [accounts, members]
  );

  const stats = useMemo(() => {
    const running = inFarm.filter((a) => a.running).length;
    const elsewhere = inFarm.filter((a) => !a.running && a.where?.state === "running").length;
    return {
      members: inFarm.length,
      running,
      elsewhere,
      stopped: inFarm.length - running - elsewhere,
    };
  }, [inFarm]);

  const allSelected = members && accounts.length > 0 && accounts.every((a) => members.has(a.name));

  const selectAll = () => persist(new Set(allSelected ? [] : accounts.map((a) => a.name)));

  const startFarm = () => {
    const stopped = inFarm.filter((a) => !a.running && a.where?.state !== "running" && !busy[a.name]);
    if (!stopped.length) {
      showToast?.(inFarm.length ? "Everything in the farm is already running." : "Add accounts to the farm first.", "info");
      return;
    }
    engine.startMany(stopped.map((a) => a.name), { ...launch, mode: "farming" });
  };

  const stopFarm = () => {
    const running = inFarm.filter((a) => a.running);
    if (!running.length) {
      showToast?.("Nothing in the farm is running.", "info");
      return;
    }
    engine.stopMany(running.map((a) => a.name));
  };

  if (members === null) return null; // one frame, avoids every row flashing unchecked

  return (
    <>
      <div className="flex items-end justify-between gap-4 px-1">
        <div>
          <h2 className="flex flex-wrap items-center gap-2.5 text-[22px] font-semibold tracking-[-0.01em] text-ink">
            Farming
            <span className="chip border-premium/45 bg-premium/8 text-premium">Premium</span>
          </h2>
          <p className="mt-1 text-[12.5px] text-ink-3">
            {stats.members === 0
              ? "Pick the accounts this machine should farm."
              : `${stats.members} ${stats.members === 1 ? "account" : "accounts"} in the farm · ${stats.running} running here.`}
          </p>
        </div>
        <div className="flex shrink-0 gap-2 pb-1">
          <Button onClick={startFarm} disabled={engine.backend !== true || !stats.stopped}>
            Start farm
          </Button>
          <Button onClick={stopFarm} disabled={!stats.running}>
            Stop farm
          </Button>
        </div>
      </div>

      <Notice tone="info" icon={InfoIcon}>
        The supervisor that watches and schedules the farm is not built yet, so
        nothing here runs on its own. What works today: choosing the fleet, and
        starting or stopping it by hand.
      </Notice>

      {/* Health — counted from live engine state, never invented. */}
      <div className="grid grid-cols-4 gap-3">
        <Stat label="In farm" value={stats.members} icon={GridIcon} />
        <Stat label="Running here" value={stats.running} tone="live" icon={HeartPulseIcon} />
        <Stat label="Stopped" value={stats.stopped} icon={UsersIcon} />
        <Stat label="Elsewhere" value={stats.elsewhere} icon={UsersIcon} />
      </div>

      {/* Fleet */}
      <section>
        <PanelHead
          icon={GridIcon}
          title="Fleet"
          count={accounts.length}
          right={
            accounts.length > 0 && (
              <Button size="sm" onClick={selectAll}>
                {allSelected ? "Clear all" : "Select all"}
              </Button>
            )
          }
        />
        {accounts.length ? (
          <ul className="py-1">
            {accounts.map((a) => {
              const busyLabel = busy[a.name];
              const remote = a.where?.state === "running" && !a.where.isLocal;
              const tone = busyLabel ? "busy" : a.running ? "live" : remote ? "busy" : "off";
              const status = busyLabel ? `${busyLabel}…` : a.running ? "Running" : remote ? a.where.label : "Stopped";
              const picked = members.has(a.name);
              return (
                <li key={a.name}>
                  <label
                    className="rule-b flex w-full cursor-pointer items-center gap-3 rounded-lg px-3.5 py-2.5
                               transition-colors duration-150 last:after:hidden hover:bg-raised/70"
                  >
                    <input
                      type="checkbox"
                      className="check shrink-0"
                      checked={picked}
                      onChange={() => toggle(a.name)}
                    />
                    <Lamp tone={tone} pulse={tone === "busy"} size={6} />
                    <span
                      className={`min-w-0 flex-1 truncate text-[12.5px] font-medium ${
                        picked ? "text-ink" : "text-ink-3"
                      }`}
                    >
                      {a.name}
                    </span>
                    {a.running && a.mode && <span className="chip border-live/40 text-live">{a.mode}</span>}
                    <span className="shrink-0 font-mono text-[10.5px] text-ink-3">{status}</span>
                  </label>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="px-3.5 py-8 text-center">
            <p className="text-[12.5px] text-ink-2">No accounts yet.</p>
            <p className="mt-1 text-[11.5px] text-ink-3">A farm needs accounts to run.</p>
          </div>
        )}
      </section>

      <Schedule />
    </>
  );
}

/* Schedule — the shape of the thing, switched off.

   Every control is disabled and nothing is stored. It is here so the layout is
   settled before the supervisor lands, and so what farming will do is legible
   now; it is not here to look functional. */
function Schedule() {
  return (
    <Panel className="overflow-hidden opacity-60">
      <PanelHead
        icon={ClockIcon}
        title="Schedule"
        right={<span className="chip">Not active yet</span>}
      />
      <div className="flex flex-col gap-4 p-4">
        <fieldset disabled className="flex flex-col gap-4">
          <legend className="sr-only">Farming schedule</legend>

          <div className="flex flex-wrap items-center gap-4">
            <span className="w-[74px] shrink-0 text-[12px] font-medium text-ink-2">Days</span>
            <div className="flex flex-wrap gap-1">
              {DAYS.map((d) => (
                <span
                  key={d}
                  className="silk rounded-md border border-line px-2 py-[5px] text-ink-3"
                >
                  {d}
                </span>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <span className="w-[74px] shrink-0 text-[12px] font-medium text-ink-2">Open</span>
            <input type="time" defaultValue="09:00" className="input w-[130px] cursor-not-allowed font-mono" />
            <span className="w-[46px] shrink-0 text-[12px] font-medium text-ink-2">Close</span>
            <input type="time" defaultValue="23:00" className="input w-[130px] cursor-not-allowed font-mono" />
          </div>
        </fieldset>

        <p className="rule-t pt-4 text-[11.5px] leading-relaxed text-ink-3">
          These controls do nothing yet, on purpose — setting a time that never
          fires would be worse than one you can see is switched off. When the
          supervisor ships it will reconcile the fleet toward this window and
          restart whatever dies inside it.
        </p>
      </div>
    </Panel>
  );
}

function Stat({ label, value, tone, icon: Icon }) {
  return (
    <div className="tile cursor-default hover:border-line hover:bg-surface">
      <span className="flex items-center gap-1.5">
        {Icon && <Icon className="h-3.5 w-3.5 text-ink-3" />}
        <span className="silk text-ink-3">{label}</span>
      </span>
      <span
        className={`font-mono text-[26px] leading-none font-semibold ${
          tone === "live" && value ? "text-live" : "text-ink"
        }`}
      >
        {value}
      </span>
    </div>
  );
}
