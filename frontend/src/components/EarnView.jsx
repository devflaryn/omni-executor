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
   balance Settings' key-redeem flow feeds into. (100 credits = $1 — that
   ratio still drives the small USD equivalents shown next to credit
   amounts, via `formatUsd`; it's just not spelled out as copy anymore.)

   Mode (cpu/gpu/both) is coin-gated, not beta-gated: the backend reports
   which coins are actually configured server-side (`status.coins`), and
   only those modes are offered. An older backend that doesn't send `coins`
   yet degrades to GPU-only, matching the original beta behavior. */

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
import { Button, Lamp, Notice, PanelHead, Toggle } from "./ui.jsx";
import { AlertIcon, CpuIcon } from "./icons.jsx";

// The engine reports hashrate/earnings on its own cadence; this just catches
// whatever changed between pushes (enroll/start/stop from another window,
// a payout that landed) without hammering the backend.
const POLL_MS = 5000;
const DAY_PRICE_CREDITS = 80;

// xmrig reports H/s; show it human-readable.
function formatHashrate(hps) {
  const n = Number(hps) || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)} MH/s`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(2)} KH/s`;
  return `${Math.round(n)} H/s`;
}

// Credits, WITHOUT rounding tiny amounts down to 0 (a few mined shares are
// worth ~0.01 credits). Up to 4 fraction digits, trailing zeros trimmed.
function formatCredits(credits) {
  const n = Number(credits) || 0;
  return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

// Grayish USD equivalent next to a credit amount (100 credits = $1). Up to
// 4 fraction digits so a fraction-of-a-cent mined amount doesn't just read
// "$0.00".
function formatUsd(credits) {
  const usd = (Number(credits) || 0) * 0.01;
  return `$${usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

const MODE_LABEL = { gpu: "GPU", cpu: "CPU", both: "Both" };
// mode -> the coin it mines, for pulling the right rate/hashrate out of status.
const MODE_COIN = { gpu: "rvn", cpu: "xmr" };

export default function EarnView({ active, auth, showToast, onAuthChange }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [lines, setLines] = useState([]);
  const [enrolling, setEnrolling] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("gpu");
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
        // leave a crashed miner showing "Mining" forever, or clear the
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
      showToast?.("Added 1 day of subscription", "success");
      // The server already spent the credits; `auth` — and the <80 disabled
      // gate on the button below — is stale until this refetches it.
      await onAuthChange?.();
    } else {
      showToast?.(res?.message || "Not enough credits", "error");
    }
  };

  const installed = Boolean(status?.installed);
  const enrolled = Boolean(status?.enrolled);
  const running = Boolean(status?.running);

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1080px] flex-col gap-6">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-[30px] font-semibold tracking-[-0.01em] text-ink">Earn</h2>
            <span className="rounded-full border border-line bg-raised px-2 py-[3px] text-[10.5px] font-bold uppercase tracking-wider text-warn">
              Beta
            </span>
          </div>
          <p className="mt-1 text-[13.5px] text-ink-3">
            Mine with your GPU — spend credits on subscription time.
          </p>
        </div>

        <section className="rounded-xl border border-line">
          <PanelHead icon={CpuIcon} title="Miner" />
          <div className="flex flex-col gap-4 p-4">
            {!enrolled ? (
              <>
                <Notice tone="warn" icon={AlertIcon}>
                  The miner is extra content, downloaded only when you enroll — it is not
                  part of the installer. Windows Defender flags mining software as a
                  threat; that's expected for every miner, not a sign something is wrong.
                  You can add a Defender exclusion for it below once it's installed.
                </Notice>
                <Button variant="solid" onClick={enroll} disabled={enrolling}>
                  {enrolling
                    ? (installed ? "Enrolling…" : "Downloading…")
                    : (installed ? "Enroll to start" : "Download miner & enroll")}
                </Button>
                {progress && (
                  <div className="text-[12.5px] text-ink-3">
                    {Math.round(progress.percent || 0)}% — {progress.artifact || "downloading"}
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="flex items-center gap-3">
                  <Lamp tone={running ? "live" : "off"} pulse={running} />
                  <span className="text-[13.5px] font-medium text-ink">
                    {running ? "Mining" : "Idle"}
                  </span>
                  <span className="ml-auto flex items-baseline gap-4 font-mono text-[12.5px] text-ink-3">
                    <span>{formatHashrate(status?.hashrate)}</span>
                    {/* creditedMicros is the lifetime ledger total this miner
                        session has earned (10,000 micros = 1 credit) — not a
                        per-session delta, which the backend doesn't expose. */}
                    <span>
                      Mined so far: {formatCredits((status?.creditedMicros ?? 0) / 10000)} credits{" "}
                      {formatUsd((status?.creditedMicros ?? 0) / 10000)}
                    </span>
                  </span>
                </div>

                <div className="flex flex-col gap-2">
                  <div
                    role="radiogroup"
                    aria-label="Mining mode"
                    className="flex w-fit shrink-0 gap-1 rounded-lg border border-line bg-raised p-1"
                  >
                    {availableModes.map((m) => (
                      <button
                        key={m}
                        type="button"
                        role="radio"
                        aria-checked={mode === m}
                        disabled={running}
                        onClick={() => setMode(m)}
                        className={`ring-focus flex h-7 items-center gap-1.5 rounded-md px-3 text-[12.5px]
                                    font-semibold transition-colors duration-150 disabled:cursor-not-allowed
                                    disabled:opacity-60 ${
                                      mode === m
                                        ? "bg-accent text-accent-ink"
                                        : "text-ink-2 hover:text-ink"
                                    }`}
                      >
                        {MODE_LABEL[m]}
                      </button>
                    ))}
                  </div>
                  <div className="flex flex-col gap-0.5 text-[12px] text-ink-3">
                    {availableModes.map((m) => {
                      const est = estimateFor(m);
                      return (
                        <div key={m}>
                          {MODE_LABEL[m]} ≈{" "}
                          {est != null
                            ? `${formatCredits(est)} credits/hr`
                            : "— (run to measure)"}
                        </div>
                      );
                    })}
                  </div>
                </div>

                <div className="rounded-lg border border-line bg-raised px-3 py-2">
                  <Toggle
                    id="earn-eco"
                    checked={eco}
                    onChange={setEcoPersisted}
                    disabled={running}
                    label="Eco mode — mine while you use your PC"
                    hint="Keeps games and apps smooth by mining on spare power (CPU runs low-priority; GPU pauses while you're active). Slower, but the machine stays responsive. Set before starting."
                  />
                </div>

                <div className="flex flex-wrap gap-2">
                  {!running ? (
                    <Button variant="solid" onClick={start} disabled={busy}>
                      Start mining
                    </Button>
                  ) : (
                    <Button variant="danger" onClick={stop} disabled={busy}>
                      Stop mining
                    </Button>
                  )}
                  <Button variant="quiet" onClick={addExclusion}>
                    Add Defender exclusion
                  </Button>
                </div>

                <div className="max-h-40 overflow-auto rounded-lg bg-raised p-2.5 font-mono text-[11.5px] text-ink-3">
                  {lines.length ? lines.map((l, i) => <div key={i}>{l}</div>) : <div>No output yet.</div>}
                </div>
              </>
            )}
          </div>
        </section>

        <section className="rounded-xl border border-line">
          <PanelHead title="Your credits" />
          <div className="flex items-center gap-4 p-4">
            <div>
              <div className="flex items-baseline gap-2">
                <span className="text-[32px] leading-none font-bold tracking-[-0.03em] text-ink">
                  {formatCredits(credits)}
                </span>
                <span className="text-[13px] font-medium text-ink-3">{formatUsd(credits)}</span>
              </div>
              <div className="mt-1 text-[12.5px] text-ink-3">credits</div>
            </div>
            <Button
              variant="solid"
              className="ml-auto"
              onClick={buyDay}
              disabled={busy || credits < DAY_PRICE_CREDITS}
            >
              Buy 1 day — {DAY_PRICE_CREDITS} credits
            </Button>
          </div>
        </section>
      </div>
    </div>
  );
}
