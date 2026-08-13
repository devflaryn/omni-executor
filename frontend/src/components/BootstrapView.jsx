/* First-boot screen: bakes/fetches the runtime before the shell can mount.
   Polls bootstrap_status once, auto-starts the bake if QEMU is present, and
   otherwise surfaces the qemu_hint so the user can fix their machine. */

import { useCallback, useEffect, useState } from "react";
import { bootstrapStatus, bootstrapStart, onEngineEvent } from "../api.js";
import { Button } from "./ui.jsx";
import { CpuIcon } from "./icons.jsx";

export default function BootstrapView({ onReady }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    const s = await bootstrapStatus();
    setStatus(s);
    if (s?.ready) onReady?.();
    return s;
  }, [onReady]);

  useEffect(() => {
    let started = false;
    let cancelled = false;

    refresh().then((s) => {
      if (cancelled) return;
      if (s && !s.ready && s.qemu_ok && !started) {
        started = true;
        bootstrapStart();
      }
    });

    const off = onEngineEvent((event, payload) => {
      if (cancelled) return;
      if (event === "bootstrap-progress") setProgress(payload);
      else if (event === "bootstrap-done") {
        setProgress(null);
        refresh();
      } else if (event === "bootstrap-error") {
        setError(payload?.error || "Setup failed");
      }
    });

    return () => {
      cancelled = true;
      off?.();
    };
  }, [refresh]);

  const pct = progress?.percent ? Math.round(progress.percent) : 0;

  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-6 bg-canvas p-10 text-ink">
      <div className="flex items-center gap-2">
        <CpuIcon className="h-4 w-4 text-ink-3" />
        <h1 className="silk text-ink-2">Setting up Omni Executor</h1>
      </div>

      {status && !status.qemu_ok && (
        <div className="max-w-md rounded-lg border border-amber-600/40 bg-amber-950/20 p-4 text-[13px]">
          <p className="mb-2 font-medium text-amber-400">QEMU is required.</p>
          <code className="block rounded bg-raised px-2 py-1 font-mono text-[11.5px] text-ink-2">
            {status.qemu_hint}
          </code>
          <Button size="sm" className="mt-3" onClick={refresh}>
            Re-check
          </Button>
        </div>
      )}

      {status?.qemu_ok && !error && (
        <div className="w-full max-w-md">
          <div className="mb-2 flex justify-between text-[11px] text-ink-3">
            <span>{progress?.artifact || "Preparing…"}</span>
            <span>{pct}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded bg-raised">
            <div
              className="h-full bg-live transition-all duration-200"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      )}

      {error && (
        <div className="max-w-md text-center">
          <p className="mb-3 text-[13px] text-danger">{error}</p>
          <Button
            variant="solid"
            size="sm"
            onClick={() => {
              setError(null);
              bootstrapStart();
            }}
          >
            Retry
          </Button>
        </div>
      )}
    </div>
  );
}
