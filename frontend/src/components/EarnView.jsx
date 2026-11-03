/* Earn: turn idle CPU/GPU cycles into subscription credits.

   Not premium-gated — anyone signed in can enroll. The miner itself is NOT
   part of the installer: Windows Defender (and most AV) flags mining
   software as a threat, correctly, because a miner running without consent
   is indistinguishable from one that's mining FOR someone else. So this view
   never downloads anything until the user explicitly opts in via
   `miningEnroll`, and says why Defender will complain before it happens
   rather than after.

   Mined value is metered server-side (accepted shares → payout, see
   cloud.py) and shows up here as `subscription.credits.credits`, the same
   balance Settings' key-redeem flow feeds into. (100 credits = $1.)

   Mode (cpu/gpu/both) is coin-gated, not beta-gated: the backend reports
   which coins are actually configured server-side (`status.coins`), and
   only those modes are offered. An older backend that doesn't send `coins`
   yet degrades to GPU-only, matching the original beta behavior.

   The page is one sheet with two sides: what you have (balance, and what it
   buys) and what's producing it (the miner). The log stays folded away. */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  miningStatus,
  miningEnroll,
  miningStart,
  miningStop,
  miningAddDefenderExclusion,
  buyDayWithCredits,
  onEngineEvent,
} from "../api.js";
import { AccountLocked, Button, Lamp, Toggle } from "./ui.jsx";
import { EarnDuoIcon } from "./icons.jsx";

// The engine reports hashrate/earnings on its own cadence; this just catches
// whatever changed between pushes (enroll/start/stop from another window,
// a payout that landed) without hammering the backend.
const POLL_MS = 5000;
const DAY_PRICE_CREDITS = 80;
// Five minutes of readings, at one per poll.
const SPEED_SAMPLES = 60;

// xmrig reports H/s; show it human-readable.
function formatHashrate(hps) {
  const n = Number(hps) || 0;
  const fixed = (v) => v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 1e6) return `${fixed(n / 1e6)} MH/s`;
  if (n >= 1e3) return `${fixed(n / 1e3)} KH/s`;
  return `${Math.round(n)} H/s`;
}

// Credits, WITHOUT rounding tiny amounts down to 0 (a few mined shares are
// worth ~0.01 credits). Up to 4 fraction digits, trailing zeros trimmed.
function formatCredits(credits) {
  const n = Number(credits) || 0;
  return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

// USD equivalent of a credit amount (100 credits = $1). Up to 4 fraction digits
// so a fraction-of-a-cent mined amount doesn't just read "$0.00".
function formatUsd(credits) {
  const usd = (Number(credits) || 0) * 0.01;
  return `$${usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

function formatDuration(hours) {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
  if (hours < 48) return `${Math.round(hours)} h`;
  return `${Math.round(hours / 24)} days`;
}

const MODE_LABEL = { gpu: "Graphics card", cpu: "Processor", both: "Both" };
// mode -> the coin it mines, for pulling the right rate/hashrate out of status.
const MODE_COIN = { gpu: "rvn", cpu: "xmr" };

export default function EarnView({ onSignIn, ...props }) {
  if (props.auth?.guest) {
    return (
      <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${props.active ? "" : "hidden"}`}>
        <AccountLocked title="Earn" onSignIn={onSignIn}>
          Turn idle CPU and GPU time into credits you can spend on Premium. Your balance lives on
          your Omni account, so Earn needs one.
        </AccountLocked>
      </div>
    );
  }
  return <EarnConsole {...props} />;
}

function EarnConsole({ active, auth, chrome, showToast, onAuthChange }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [lines, setLines] = useState([]);
  const [enrolling, setEnrolling] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("gpu");
  const [speeds, setSpeeds] = useState([]);
  // Eco mode: mine on spare cycles so the machine stays usable (see start()).
  // Remembered across launches; localStorage can throw in odd webview states,
  // so every access is guarded and falls back to "off".
  const [eco, setEco] = useState(() => {
    try {
      return localStorage.getItem("earn.eco") === "1";
    } catch {
      return false;
    }
  });
  const timer = useRef(null);
  // Last-seen creditedMicros, so a poll can tell "a payout just landed" (the
  // value went UP) apart from "nothing changed" — only the former is worth an
  // auth refresh. null until the first status answers, so the very first
  // poll never fires one for an existing balance.
  const lastCredited = useRef(null);

  const credits = auth?.subscription?.credits?.credits ?? 0;

  // Which coins the backend actually has configured. An older backend that
  // doesn't send `coins` at all degrades to the original GPU-only beta
  // rather than hiding mining entirely.
  const coins = status?.coins;
  const hasRvn = coins ? Boolean(coins.rvn) : true;
  const hasXmr = coins ? Boolean(coins.xmr) : false;
  const availableModes = [
    ...(hasRvn ? ["gpu"] : []),
    ...(hasXmr ? ["cpu"] : []),
    ...(hasRvn && hasXmr ? ["both"] : []),
  ];

  // estHashrate/rates come from the backend's mining_status; both are
  // optional (older backend) so every lookup here must tolerate `undefined`.
  const estHashrate = status?.estHashrate;
  const rates = status?.rates;
  // Credits/hour a mode would earn AT its last-measured hashrate. null means
  // "never measured yet" (nothing to estimate from), not "zero".
  const creditsPerHourFor = (kind) => {
    const hr = Number(estHashrate?.[kind]) || 0;
    const rate = Number(rates?.[MODE_COIN[kind]]);
    if (!hr || !Number.isFinite(rate)) return null;
    return hr * rate;
  };
  const estimateFor = (m) => {
    if (m === "both") {
      const g = creditsPerHourFor("gpu");
      const c = creditsPerHourFor("cpu");
      if (g == null && c == null) return null;
      return (g ?? 0) + (c ?? 0);
    }
    return creditsPerHourFor(m);
  };

  // Keep `mode` valid as `coins` arrives/changes (first poll, or a coin
  // getting disabled server-side) without stomping a still-valid choice.
  useEffect(() => {
    if (availableModes.length && !availableModes.includes(mode)) {
      setMode(availableModes[0]);
    }
  }, [availableModes.join(","), mode]);

  const refresh = useCallback(async () => {
    const s = await miningStatus();
    if (!s || s.ok === false) return;
    setStatus(s);
    setSpeeds((prev) => (s.running ? [...prev, Number(s.hashrate) || 0].slice(-SPEED_SAMPLES) : []));
    const now = s.creditedMicros;
    if (typeof now === "number") {
      if (lastCredited.current != null && now > lastCredited.current) {
        // The mining payout flush (~60s, server-side) just credited the
        // permanent bucket. `auth` is what the whole app — and this view's
        // own `credits` gate — reads, so it must refresh too.
        onAuthChange?.();
      }
      lastCredited.current = now;
    }
  }, [onAuthChange]);

  useEffect(() => {
    if (!active) return undefined;
    refresh();
    timer.current = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer.current);
  }, [active, refresh]);

  useEffect(() => {
    return onEngineEvent((event, payload) => {
      if (event === "mining-progress") setProgress(payload);
      if (event === "mining-stat") {
        const line = payload?.line;
        if (line) setLines((l) => [...l.slice(-200), line]);
      }
      if (event === "mining-done") {
        // main.py emits two distinct phases on this event: "install" after
        // the enroll download finishes, and "run" when the miner PROCESS
        // exits (stopped, crashed, or asked to stop). Conflating them would
        // leave a crashed miner showing "Running" forever, or clear the
        // download progress bar for an unrelated run-exit.
        if (payload?.phase === "install") {
          setProgress(null);
          setEnrolling(false);
        } else if (payload?.phase === "run") {
          setBusy(false);
        }
        refresh();
      }
      if (event === "mining-error") {
        setProgress(null);
        setEnrolling(false);
        showToast?.(payload?.error || payload?.message || "Mining error", "error");
      }
    });
  }, [refresh, showToast]);

  const enroll = async () => {
    setEnrolling(true);
    const res = await miningEnroll();
    if (!res?.ok) {
      setEnrolling(false);
      showToast?.(res?.message || "Could not download the miner", "error");
      return;
    }
    showToast?.("Miner downloaded", "success");
    setEnrolling(false);
    refresh();
  };

  const setEcoPersisted = (on) => {
    setEco(on);
    try {
      localStorage.setItem("earn.eco", on ? "1" : "0");
    } catch {
      /* a webview with storage disabled just won't remember the choice */
    }
  };

  const start = async () => {
    setBusy(true);
    const res = await miningStart(mode, 50, eco);
    setBusy(false);
    if (res?.ok) {
      showToast?.("Mining started", "success");
      refresh();
    } else {
      showToast?.(res?.message || "Could not start mining", "error");
    }
  };

  const stop = async () => {
    setBusy(true);
    await miningStop();
    setBusy(false);
    showToast?.("Mining stopped", "info");
    refresh();
  };

  const addExclusion = async () => {
    const res = await miningAddDefenderExclusion();
    showToast?.(
      res?.ok ? "Defender exclusion added" : res?.message || "Could not add the exclusion",
      res?.ok ? "success" : "error"
    );
  };

  const buyDay = async () => {
    setBusy(true);
    const res = await buyDayWithCredits();
    setBusy(false);
    if (res?.ok) {
      showToast?.("Added 1 day of premium", "success");
      // The server already spent the credits; `auth` — and the <80 disabled
      // gate on the button below — is stale until this refetches it.
      await onAuthChange?.();
    } else {
      showToast?.(res?.message || "Not enough credits", "error");
    }
  };

  const enrolled = Boolean(status?.enrolled);
  const installed = Boolean(status?.installed);
  const running = Boolean(status?.running);
  // creditedMicros is the lifetime ledger total (10,000 micros = 1 credit).
  const mined = (status?.creditedMicros ?? 0) / 10000;
  const rate = estimateFor(mode);
  // Defender exists on Windows only. A browser preview shows the Windows layout.
  const windows = !chrome?.desktop || chrome?.platform === "windows";

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 pt-3 pb-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[920px] flex-col gap-4">
        <section className="earn-sheet">
          <Balance credits={credits} mined={mined} busy={busy} onBuy={buyDay} />
          {enrolled ? (
            <Miner
              running={running}
              busy={busy}
              speed={status?.hashrate}
              speeds={speeds}
              rate={rate}
              creditsNeeded={Math.max(0, DAY_PRICE_CREDITS - credits)}
              offline={Boolean(status?.remote_error)}
              modes={availableModes}
              mode={mode}
              onMode={setMode}
              eco={eco}
              onEco={setEcoPersisted}
              onStart={start}
              onStop={stop}
            />
          ) : (
            <Setup
              enrolling={enrolling}
              installed={installed}
              progress={progress}
              onSetUp={enroll}
            />
          )}
        </section>

        {enrolled && (
          <div className="flex flex-col gap-2">
            <details className="earn-log">
              <summary className="ring-focus rounded-md text-[12.5px] text-ink-3 hover:text-ink-2">
                Miner log
              </summary>
              <div className="mt-2 max-h-44 overflow-auto rounded-lg border border-line bg-surface p-3 font-mono text-[11.5px] leading-relaxed text-ink-3 select-text">
                {lines.length ? lines.map((l, i) => <div key={i}>{l}</div>) : <div>Nothing yet. The log fills once mining starts.</div>}
              </div>
            </details>
            {windows && (
              <p className="text-[12.5px] text-ink-3">
                Windows Defender stopping the miner?{" "}
                <button
                  type="button"
                  onClick={addExclusion}
                  className="ring-focus rounded-sm text-ink-2 underline decoration-ink-3 underline-offset-2 hover:text-ink"
                >
                  Add an exclusion
                </button>
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** What you have, and how far it goes. */
function Balance({ credits, mined, busy, onBuy }) {
  const days = Math.floor(credits / DAY_PRICE_CREDITS);
  const fill = days >= 1 ? 100 : (credits / DAY_PRICE_CREDITS) * 100;
  const needed = Math.ceil((DAY_PRICE_CREDITS - credits) * 100) / 100;

  return (
    <div className="earn-side">
      <p className="text-[13px] text-ink-3">Balance</p>
      <p className="mt-2 flex items-baseline gap-2">
        <span className="text-[44px] leading-none font-semibold tracking-[-0.03em] text-ink tabular-nums">
          {formatCredits(credits)}
        </span>
        <span className="text-[14px] text-ink-2">credits</span>
      </p>
      <p className="mt-1.5 text-[13px] text-ink-3 tabular-nums">{formatUsd(credits)}</p>
      {mined > 0 && (
        <p className="mt-1 text-[13px] text-ink-3 tabular-nums">
          {formatCredits(mined)} credits mined so far
        </p>
      )}

      <div className="mt-auto pt-8">
        <div
          className="h-1 overflow-hidden rounded-full bg-raised"
          role="progressbar"
          aria-label="Progress to one day of premium"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(fill)}
        >
          <div className="h-full rounded-full bg-accent" style={{ width: `${fill}%` }} />
        </div>
        <p className="mt-2.5 text-[13px] text-ink-2">
          {days >= 1
            ? `Enough for ${days} ${days === 1 ? "day" : "days"} of premium`
            : `${formatCredits(needed)} more for 1 day of premium`}
        </p>
        <Button
          variant={days >= 1 ? "solid" : "quiet"}
          className="mt-4 w-full"
          onClick={onBuy}
          disabled={busy || days < 1}
        >
          Buy 1 day for {DAY_PRICE_CREDITS} credits
        </Button>
      </div>
    </div>
  );
}

/** Before the miner is on this computer. Says what it is and what it costs you. */
function Setup({ enrolling, installed, progress, onSetUp }) {
  const pct = Math.round(progress?.percent || 0);
  return (
    <div className="earn-side">
      <EarnDuoIcon className="h-7 w-7 text-ink-2" />
      <h3 className="mt-4 text-[17px] font-semibold tracking-[-0.01em] text-ink">
        Mine with your computer&apos;s spare power
      </h3>
      <p className="mt-2 max-w-[42ch] text-[13.5px] leading-relaxed text-ink-2">
        Your computer mines cryptocurrency and you&apos;re paid in credits. It uses more
        electricity and runs warmer while it&apos;s on, and you can stop at any time.
      </p>

      <div className="mt-auto pt-8">
        {enrolling && !installed && (
          <div className="mb-4">
            <div className="h-1 overflow-hidden rounded-full bg-raised">
              <div className="h-full rounded-full bg-accent transition-[width] duration-200" style={{ width: `${pct}%` }} />
            </div>
            <p className="mt-2 text-[12.5px] text-ink-3 tabular-nums">Downloading {pct}%</p>
          </div>
        )}
        <p className="mb-4 text-[12.5px] leading-relaxed text-ink-3">
          This downloads the miner, which isn&apos;t part of the app. Antivirus software often
          flags miners, and Windows Defender may flag this one.
        </p>
        <Button variant="solid" className="w-full" onClick={onSetUp} disabled={enrolling}>
          {enrolling ? "Setting up…" : "Set up mining"}
        </Button>
      </div>
    </div>
  );
}

function Miner({
  running, busy, speed, speeds, rate, creditsNeeded, offline,
  modes, mode, onMode, eco, onEco, onStart, onStop,
}) {
  return (
    <div className="earn-side">
      <div className="flex items-center gap-2.5">
        <Lamp tone={running ? "live" : "off"} pulse={running} />
        <h3 className="text-[15px] font-semibold tracking-[-0.01em] text-ink">
          {running ? "Mining" : "Not mining"}
        </h3>
        {running && (
          <span className="ml-auto text-[13px] text-ink-2 tabular-nums">{formatHashrate(speed)}</span>
        )}
      </div>

      {running ? (
        <div className="mt-4">
          <Sparkline values={speeds} />
          <dl className="mt-4 flex flex-col gap-2 text-[13px]">
            {rate != null && (
              <Fact label="Earning about" value={`${formatCredits(Math.round(rate * 100) / 100)} credits an hour`} />
            )}
            {rate > 0 && creditsNeeded > 0 && (
              <Fact label="Next day of premium" value={`in about ${formatDuration(creditsNeeded / rate)}`} />
            )}
          </dl>
        </div>
      ) : (
        <div className="mt-5 flex flex-col gap-4">
          {modes.length === 0 ? (
            <p className="text-[13px] text-ink-3">Mining isn&apos;t available right now.</p>
          ) : (
            <div>
              <p className="mb-2 text-[13px] text-ink-3">Mine with</p>
              <div role="radiogroup" aria-label="Mine with" className="flex gap-1 rounded-lg border border-line bg-raised p-1">
                {modes.map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="radio"
                    aria-checked={mode === m}
                    onClick={() => onMode(m)}
                    className={`ring-focus h-8 min-w-0 flex-1 truncate rounded-md px-2 text-[12.5px] font-semibold
                                transition-colors duration-150 ${
                                  mode === m ? "bg-accent text-accent-ink" : "text-ink-2 hover:text-ink"
                                }`}
                  >
                    {MODE_LABEL[m]}
                  </button>
                ))}
              </div>
              <p className="mt-2 text-[12.5px] text-ink-3 tabular-nums">
                {rate != null
                  ? `About ${formatCredits(Math.round(rate * 100) / 100)} credits an hour`
                  : "Start it once to see what it earns here."}
              </p>
            </div>
          )}
          <Toggle
            id="earn-eco"
            checked={eco}
            onChange={onEco}
            label="Eco mode"
            hint="Earns less, but games and apps stay smooth."
          />
        </div>
      )}

      <div className="mt-auto pt-8">
        {offline && (
          <p className="mb-3 text-[12.5px] text-ink-3">
            Can&apos;t reach the server. Your balance updates once it&apos;s back.
          </p>
        )}
        {running ? (
          <Button variant="quiet" className="w-full" onClick={onStop} disabled={busy}>
            Stop mining
          </Button>
        ) : (
          <Button variant="solid" className="w-full" onClick={onStart} disabled={busy || modes.length === 0}>
            Start mining
          </Button>
        )}
      </div>
    </div>
  );
}

function Fact({ label, value }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-ink-3">{label}</dt>
      <dd className="text-ink-2 tabular-nums">{value}</dd>
    </div>
  );
}

/** Mining speed over the last few minutes. One series, so no legend; the reading
    above it is the current value, and hovering reads any earlier one. */
function Sparkline({ values }) {
  const [hover, setHover] = useState(null);
  const W = 400;
  const H = 56;
  const PAD = 3;

  if (values.length < 2) {
    return (
      <div className="flex h-14 items-center border-b border-line text-[12.5px] text-ink-3">
        Measuring speed…
      </div>
    );
  }

  const max = Math.max(...values) || 1;
  const x = (i) => (i / (values.length - 1)) * W;
  // Anchored at zero, so a small wobble doesn't look like a crash.
  const y = (v) => H - PAD - (v / max) * (H - PAD * 2);
  const line = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  const onMove = (e) => {
    const box = e.currentTarget.getBoundingClientRect();
    const i = Math.round(((e.clientX - box.left) / box.width) * (values.length - 1));
    setHover(Math.min(values.length - 1, Math.max(0, i)));
  };

  return (
    <div
      className="relative h-14"
      onMouseMove={onMove}
      onMouseLeave={() => setHover(null)}
      role="img"
      aria-label={`Mining speed over the last few minutes, now ${formatHashrate(values[values.length - 1])}`}
    >
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-full w-full">
        <line x1="0" x2={W} y1={H - 0.5} y2={H - 0.5} stroke="var(--color-line)" vectorEffect="non-scaling-stroke" />
        <polyline
          points={line}
          fill="none"
          stroke="var(--color-ink-2)"
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
        {hover != null && (
          <line
            x1={x(hover)}
            x2={x(hover)}
            y1="0"
            y2={H}
            stroke="var(--color-ink-3)"
            vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>
      {hover != null && (
        <span
          className="pointer-events-none absolute -top-1 rounded-md border border-line bg-raised px-1.5 py-0.5 text-[11.5px] whitespace-nowrap text-ink tabular-nums"
          style={
            x(hover) > W / 2
              ? { right: `${100 - (x(hover) / W) * 100 + 1}%` }
              : { left: `${(x(hover) / W) * 100 + 1}%` }
          }
        >
          {formatHashrate(values[hover])}
        </span>
      )}
    </div>
  );
}
