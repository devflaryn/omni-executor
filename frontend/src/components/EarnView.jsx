/* Earn: turn idle CPU/GPU cycles into subscription credits.

   Not premium-gated — anyone signed in can enroll. The miner itself is NOT
   part of the installer: Windows Defender (and most AV) flags mining
   software as a threat, correctly, because a miner running without consent
   is indistinguishable from one that's mining FOR someone else. So this view
   never downloads anything until the user explicitly opts in via
   `miningEnroll`, and says why Defender will complain before it happens
   rather than after.

   100 credits = $1. Mined value is metered server-side (accepted shares →
   payout, see cloud.py) and shows up here as `subscription.credits.credits`,
   the same balance Settings' key-redeem flow feeds into. */

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
import { Button, Lamp, Notice, PanelHead } from "./ui.jsx";
import { AlertIcon, CpuIcon } from "./icons.jsx";

// The engine reports hashrate/earnings on its own cadence; this just catches
// whatever changed between pushes (enroll/start/stop from another window,
// a payout that landed) without hammering the backend.
const POLL_MS = 5000;
const DAY_PRICE_CREDITS = 80;
// Beta: GPU/Ravencoin only. The CPU (Monero) path exists in the backend but is
// gated off here and in main.py's mining_start until the beta ends.
const MODE = "gpu";

export default function EarnView({ active, auth, showToast, onAuthChange }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [lines, setLines] = useState([]);
  const [enrolling, setEnrolling] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);
  // Last-seen creditedMicros, so a poll can tell "a payout just landed" (the
  // value went UP) apart from "nothing changed" — only the former is worth an
  // auth refresh. null until the first status answers, so the very first
  // poll never fires one for an existing balance.
  const lastCredited = useRef(null);

  const credits = Math.round(auth?.subscription?.credits?.credits ?? 0);

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

  const start = async () => {
    setBusy(true);
    const res = await miningStart(MODE, 50);
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
            Mine with your GPU — 100 credits = $1, spend them on subscription time.
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
                    <span>{status?.hashrate ?? 0} H/s</span>
                    {/* creditedMicros is the lifetime ledger total this miner
                        session has earned (10,000 micros = 1 credit) — not a
                        per-session delta, which the backend doesn't expose. */}
                    <span>
                      Mined so far: {Math.round((status?.creditedMicros ?? 0) / 10000)} credits
                    </span>
                  </span>
                </div>

                <div className="flex items-center gap-2.5">
                  <span className="rounded-lg border border-line bg-raised px-3 py-1.5 text-[12.5px] font-medium text-ink">
                    GPU · Ravencoin
                  </span>
                  <span className="text-[12px] text-ink-3">CPU mining comes after the beta.</span>
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
              <div className="text-[32px] leading-none font-bold tracking-[-0.03em] text-ink">
                {credits}
              </div>
              <div className="mt-1 text-[12.5px] text-ink-3">credits · 100 = $1</div>
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
