/* First-boot screen: downloads what this machine needs -- the omnidroid
   bundle and the executor APK -- and only then hands over to the app shell.

   There is nothing to install on the HOST any more. The QEMU engine needed a
   portable QEMU, adb, Windows Hypervisor Platform (an administrator prompt
   and a restart) and several GB of base images; native omnidroid needs none
   of that, so this screen is a download and nothing else. */

import { useCallback, useEffect, useRef, useState } from "react";
import { bootstrapStatus, bootstrapStart, onEngineEvent } from "../api.js";
import { Button } from "./ui.jsx";
import { CpuIcon } from "./icons.jsx";

/* What the user sees for each phase the backend reports. */
const PHASE_LABEL = {
  download: null, // falls through to the artifact name
  start: null,
  done: "Finishing up…",
};

const ARTIFACT_LABEL = {
  "omnidroid-win": "Downloading omnidroid",
  "omnidroid-mac": "Downloading omnidroid",
  "omniexec-apk-win": "Downloading the executor",
  "omniexec-apk-mac": "Downloading the executor",
};

export default function BootstrapView({ onReady }) {
  const [, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);
  // Tracks whether we've already fired bootstrap_start this session, so a
  // re-render or a status refresh only ever kicks off one install.
  const startedRef = useRef(false);

  const maybeStart = useCallback((s) => {
    if (s && !s.ready && !startedRef.current) {
      startedRef.current = true;
      bootstrapStart();
    }
  }, []);

  const refresh = useCallback(async () => {
    const s = await bootstrapStatus();
    setStatus(s);
    if (s?.ready) onReady?.();
    else maybeStart(s);
    return s;
  }, [onReady, maybeStart]);

  useEffect(() => {
    let cancelled = false;

    refresh();

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
  const showBar = Boolean(progress?.total) && !error;
  const label =
    PHASE_LABEL[progress?.phase] ??
    ARTIFACT_LABEL[progress?.artifact] ??
    progress?.artifact ??
    "Preparing…";

  return (
    <Shell>
      {!error && (
        <div className="w-full max-w-md">
          <div className="mb-2 flex justify-between text-[12px] text-ink-3">
            <span>{label}</span>
            {showBar && <span>{pct}%</span>}
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-raised">
            <div
              className={
                showBar
                  ? "h-full bg-live transition-all duration-200"
                  : "h-full w-1/3 animate-pulse bg-live/60"
              }
              style={showBar ? { width: `${pct}%` } : undefined}
            />
          </div>
          <p className="mt-3 text-center text-[12px] text-ink-3">
            This only happens once. It downloads about a gigabyte, so it can
            take a while.
          </p>
        </div>
      )}

      {error && (
        <div className="max-w-md text-center">
          <p className="mb-3 text-[14px] text-danger">{error}</p>
          <Button
            variant="solid"
            size="sm"
            onClick={() => {
              setError(null);
              startedRef.current = true;
              bootstrapStart();
            }}
          >
            Retry
          </Button>
        </div>
      )}
    </Shell>
  );
}

function Shell({ children }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-6 overflow-y-auto bg-canvas p-10 text-ink">
      <div className="flex items-center gap-2">
        <CpuIcon className="h-4 w-4 text-ink-3" />
        <h1 className="silk text-ink-2">Setting up Omni Executor</h1>
      </div>
      {children}
    </div>
  );
}
