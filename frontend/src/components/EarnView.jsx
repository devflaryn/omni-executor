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
const MODES = ["cpu", "gpu", "both"];

export default function EarnView({ active, auth, showToast }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [lines, setLines] = useState([]);
  const [mode, setMode] = useState("both");
  const [enrolling, setEnrolling] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);

  const credits = Math.round(auth?.subscription?.credits?.credits ?? 0);

  const refresh = useCallback(async () => {
    const s = await miningStatus();
    if (s && s.ok !== false) setStatus(s);
  }, []);

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
        setProgress(null);
        setEnrolling(false);
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
    const res = await miningStart(mode, 50);
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
    if (res?.ok) showToast?.("Added 1 day of subscription", "success");
    else showToast?.(res?.message || "Not enough credits", "error");
  };

  const installed = Boolean(status?.installed);
  const running = Boolean(status?.running);

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[1080px] flex-col gap-6">
        <div>
          <h2 className="text-[30px] font-semibold tracking-[-0.01em] text-ink">Earn</h2>
          <p className="mt-1 text-[13.5px] text-ink-3">
            Mine with spare CPU/GPU cycles — 100 credits = $1, spend them on subscription time.
          </p>
        </div>

        <section className="rounded-xl border border-line">
          <PanelHead icon={CpuIcon} title="Miner" />
          <div className="flex flex-col gap-4 p-4">
            {!installed ? (
              <>
                <Notice tone="warn" icon={AlertIcon}>
                  The miner is extra content, downloaded only when you enroll — it is not
                  part of the installer. Windows Defender flags mining software as a
                  threat; that's expected for every miner, not a sign something is wrong.
                  You can add a Defender exclusion for it below once it's installed.
                </Notice>
                <Button variant="solid" onClick={enroll} disabled={enrolling}>
                  {enrolling ? "Downloading…" : "Download miner & enroll"}
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
                  <span className="ml-auto font-mono text-[12.5px] text-ink-3">
                    {status?.hashrate ?? 0} H/s
                  </span>
                </div>

                <div className="flex gap-2">
                  {MODES.map((m) => (
                    <Button
                      key={m}
                      variant={mode === m ? "solid" : "quiet"}
                      size="sm"
                      onClick={() => setMode(m)}
                      disabled={running}
                    >
                      {m.toUpperCase()}
                    </Button>
                  ))}
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
