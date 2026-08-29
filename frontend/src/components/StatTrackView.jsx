/* Stat Track: what your accounts are actually earning, from inside the game.

   Premium. The gate here is the same shape as Farming's — a locked panel
   rather than a hidden tab — but unlike Farming it is NOT presentation only:
   the server refuses both halves for a free account (402 on /api/v1/stats, 402
   on every report the in-game collector sends), so switching this view off
   would not be what stops the feature. See stats.controller.js.

   THE TWO LIGHTS ARE DIFFERENT FACTS and the whole panel is built around not
   conflating them:

     Running   the VM is up — the account's presence lease, renewed by whichever
               machine launched it.
     Tracking  the SCRIPT inside it is still talking, which is the fresher and
               strictly stronger claim.

   An instance whose Roblox client died keeps "Running" for up to 90 seconds and
   loses "Tracking" immediately. Showing one lamp for both would hide exactly
   the failure a farming dashboard exists to catch. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Button, Lamp, Notice, PanelHead, Toggle } from "./ui.jsx";
import {
  ChartIcon,
  GridIcon,
  HeartPulseIcon,
  InfoIcon,
  LockIcon,
  RefreshIcon,
  UsersIcon,
} from "./icons.jsx";

// How often the tab re-asks while it is open. The collector reports every 20 s,
// so a faster poll here only spends requests to show the same numbers.
const POLL_MS = 15000;

export default function StatTrackView({ active, auth, onGo, showToast }) {
  const premium = auth?.subscription?.tier === "premium";

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1080px] flex-col gap-6">
        {premium ? (
          <StatConsole active={active} showToast={showToast} />
        ) : (
          <StatTrackLocked subscription={auth?.subscription} onGo={onGo} />
        )}
      </div>
    </div>
  );
}

/* The free tier's view. Says what the section does rather than nagging — the
   tab has to earn its place in the rail even for someone who cannot open it.
   An expired plan gets "renew", a plan that never existed gets "redeem": those
   are not the same prompt. */
function StatTrackLocked({ subscription, onGo }) {
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
          Stat Track
          <span className="chip border-premium/45 bg-premium/8 text-premium">Premium</span>
        </p>
        <p className="mx-auto mt-2 max-w-[46ch] text-[12.5px] leading-relaxed text-ink-3">
          {lapsed
            ? "Your plan has expired, so Stat Track stopped reporting. Redeem a key to pick it back up — nothing you collected is lost."
            : "See what every account is actually earning, live, without opening a single screen."}
        </p>
      </div>

      <ul className="mx-auto flex w-full max-w-[420px] flex-col gap-2.5 text-left">
        {[
          ["Read the game's own numbers", "Gems, coins, level — whatever the game puts on screen."],
          ["Know which ones are still earning", "A client that died stops reporting the moment it does."],
          ["From anywhere", "The same numbers show up on the website when you sign in there."],
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
function StatConsole({ active, showToast }) {
  const [status, setStatus] = useState(null);      // the local autoexec file
  const [data, setData] = useState(null);          // the server's rows
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null);          // expanded account name

  const refreshStatus = useCallback(async () => {
    setStatus(await api("stattrack_status"));
  }, []);

  const refresh = useCallback(async () => {
    const res = await api("stattrack_stats");
    if (res?.ok) {
      setData(res);
      setError(null);
    } else {
      // A free/expired plan is not an error state here — the console only
      // renders for premium, so a 402 means the plan lapsed while the tab was
      // open. Say that plainly instead of showing a stale table.
      setError(res?.message || "Could not reach the Omni server.");
    }
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  // Only polls while the tab is on screen. Every view in this app stays
  // mounted so the editor keeps its buffer, which means a naive interval here
  // would keep asking the server forever from a tab nobody is looking at.
  useEffect(() => {
    if (!active) return undefined;
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);

  const toggle = async (next) => {
    setBusy(true);
    const res = await api("stattrack_set", next);
    setBusy(false);
    if (res?.ok) {
      setStatus(res);
      showToast?.(res.message, "info");
    } else {
      showToast?.(res?.message || "Could not change Stat Track.", "error");
    }
  };

  const rows = data?.accounts ?? [];
  const summary = data?.summary ?? {};

  return (
    <>
      <div className="flex items-end justify-between gap-4 px-1">
        <div>
          <h2 className="flex flex-wrap items-center gap-2.5 text-[22px] font-semibold tracking-[-0.01em] text-ink">
            Stat Track
            <span className="chip border-premium/45 bg-premium/8 text-premium">Premium</span>
          </h2>
          <p className="mt-1 text-[12.5px] text-ink-3">
            {status?.enabled
              ? `Reporting from every instance this machine launches · ${summary.tracking ?? 0} live`
              : "Switch it on to have every launch report what it earns."}
          </p>
        </div>
        <div className="flex shrink-0 gap-2 pb-1">
          <Button size="md" onClick={refresh} disabled={busy}>
            <RefreshIcon className="h-3.5 w-3.5" />
            Refresh
          </Button>
        </div>
      </div>

      {/* The switch. One file on disk, and the panel says so — this is the
          folder the user already owns and can open. */}
      <section className="rounded-xl border border-line px-4 py-3.5">
        <Toggle
          id="stattrack-enabled"
          checked={Boolean(status?.enabled)}
          onChange={toggle}
          label="Report stats from every launch"
          hint={
            status?.stale
              ? "The autoexec file points at a different server — switch it on again to repair it."
              : "Writes one file into your autoexec folder. Turning it off deletes it."
          }
        />
        {status?.file && (
          <p className="mt-2.5 truncate font-mono text-[10.5px] text-ink-3" title={status.file}>
            {status.file}
          </p>
        )}
      </section>

      {status?.enabled && (
        <Notice tone="info" icon={InfoIcon}>
          Stat Track arms at launch. An instance that is <em>already</em> running
          picked up the autoexec folder as it was when it started, so it will
          begin reporting after its next start.
        </Notice>
      )}

      {error && (
        <Notice tone="error" icon={InfoIcon} action={<Button size="sm" onClick={refresh}>Retry</Button>}>
          {error}
        </Notice>
      )}

      <div className="grid grid-cols-3 gap-3">
        <Stat label="Accounts" value={summary.accounts ?? 0} icon={UsersIcon} />
        <Stat label="Running" value={summary.online ?? 0} icon={GridIcon} />
        <Stat label="Reporting" value={summary.tracking ?? 0} tone="live" icon={HeartPulseIcon} />
      </div>

      <section>
        <PanelHead icon={ChartIcon} title="Accounts" count={rows.length} />
        {rows.length ? (
          <ul className="py-1">
            {rows.map((row) => (
              <AccountRow
                key={row.username}
                row={row}
                open={open === row.username}
                onToggle={() => setOpen(open === row.username ? null : row.username)}
              />
            ))}
          </ul>
        ) : (
          <div className="px-3.5 py-8 text-center">
            <p className="text-[12.5px] text-ink-2">
              {data ? "No accounts yet." : "Loading…"}
            </p>
            {data && (
              <p className="mt-1 text-[11.5px] text-ink-3">
                Add an account and launch it — its numbers land here.
              </p>
            )}
          </div>
        )}
      </section>
    </>
  );
}

function AccountRow({ row, open, onToggle }) {
  // Two lamps, two facts. `tracking` is the fresher and stronger of the pair:
  // it can only be true if a script inside a live client answered recently.
  const running = row.presence?.state === "running";
  const tone = row.tracking ? "live" : running ? "busy" : "off";
  const state = row.tracking
    ? "Reporting"
    : running
      ? `${row.presence.label} · silent`
      : "Stopped";

  const metrics = row.metrics ?? [];
  const headline = metrics.slice(0, open ? metrics.length : 3);

  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        className="rule-b flex w-full items-center gap-3 rounded-lg px-3.5 py-2.5 text-left
                   transition-colors duration-150 last:after:hidden hover:bg-raised/70"
      >
        <Lamp tone={tone} pulse={row.tracking} size={6} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] font-medium text-ink">
            {row.customName || row.username}
          </span>
          <span className="block truncate text-[11px] text-ink-3">
            {row.placeName || (row.placeId ? `Place ${row.placeId}` : "—")}
          </span>
        </span>

        {/* The numbers themselves, right-aligned so a column of accounts reads
            down rather than across. */}
        <span className="flex shrink-0 items-center gap-5">
          {headline.map((m) => (
            <span key={m.key} className="text-right">
              <span className="silk block text-ink-3">{m.label}</span>
              <span className="block font-mono text-[12.5px] text-ink">{m.display || "—"}</span>
            </span>
          ))}
          {!metrics.length && (
            <span className="font-mono text-[10.5px] text-ink-3">no readings yet</span>
          )}
        </span>

        <span className="w-[104px] shrink-0 text-right font-mono text-[10.5px] text-ink-3">
          {state}
        </span>
      </button>

      {open && (
        <div className="rule-b px-3.5 pt-1 pb-3.5 last:after:hidden">
          <dl className="grid grid-cols-3 gap-x-6 gap-y-1 text-[11.5px]">
            <Detail label="Roblox id" value={row.userId ?? "—"} />
            <Detail label="In game for" value={formatUptime(row.uptimeSec)} />
            <Detail label="Last report" value={formatAgo(row.reportedAt)} />
            <Detail label="Executor" value={row.executor || "—"} />
            <Detail label="Server" value={row.jobId ? `${row.jobId.slice(0, 8)}…` : "—"} />
            <Detail label="Reports" value={row.reportCount ?? 0} />
          </dl>
        </div>
      )}
    </li>
  );
}

function Detail({ label, value }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="silk text-ink-3">{label}</dt>
      <dd className="truncate font-mono text-ink-2">{value}</dd>
    </div>
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

function formatUptime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${seconds}s`;
}

function formatAgo(iso) {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return "—";
  if (ms < 60_000) return `${Math.max(0, Math.round(ms / 1000))}s ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`;
  return `${Math.round(ms / 3_600_000)}h ago`;
}
