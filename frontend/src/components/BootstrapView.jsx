/* First-boot screen: installs everything this machine needs, in order —
   the host tools (QEMU, adb), then the base images — and only then hands
   over to the app shell.

   It used to REFUSE to start until QEMU was already installed, and nothing in
   the app ever installed QEMU. A fresh machine therefore sat here forever,
   reading "QEMU is required" beside a hint promising an automatic install that
   could not happen, with a Re-check button that could only ever return the
   same answer. Setup now begins unconditionally and reports what it is doing;
   the only thing a user is ever asked for is the administrator prompt, and
   only on a machine that actually needs one. */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  bootstrapStatus,
  bootstrapStart,
  enableVirtualization,
  restartWindows,
  onEngineEvent,
} from "../api.js";
import { Button } from "./ui.jsx";
import { CpuIcon } from "./icons.jsx";

/* What the user sees for each phase the backend reports. `tools` and
   `elevate` are the new ones: they cover the window between "the app opened"
   and "there is anything to show a percentage for", which is exactly where a
   fresh install used to look frozen. */
const PHASE_LABEL = {
  tools: "Checking what this PC needs…",
  elevate: "Waiting for administrator permission…",
  install: "Installing QEMU…",
  download: null, // falls through to the artifact name
  start: null,
  done: "Finishing up…",
};

const ARTIFACT_LABEL = {
  qemu: "Downloading QEMU",
  adb: "Downloading Android platform tools",
};

export default function BootstrapView({ onReady }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);
  const [rebooting, setRebooting] = useState(false);
  const [enabling, setEnabling] = useState(false);
  // Tracks whether we've already fired bootstrap_start this session, so a
  // re-render or a status refresh only ever kicks off one install.
  const startedRef = useRef(false);

  const maybeStart = useCallback((s) => {
    // Deliberately NOT gated on s.qemu_ok — installing QEMU is the job, not
    // the entry requirement. Only an unfinished setup and a pending reboot
    // hold it back.
    if (s && !s.ready && !s.reboot_required && !startedRef.current) {
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
      else if (event === "bootstrap-reboot") {
        setStatus((s) => ({ ...(s || {}), reboot_required: true }));
      } else if (event === "bootstrap-done") {
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

  const onEnable = async () => {
    setEnabling(true);
    const res = await enableVirtualization();
    setEnabling(false);
    if (!res?.ok) setError(res?.error || "Could not enable virtualization.");
    else if (res.reboot_required) {
      setStatus((s) => ({ ...(s || {}), reboot_required: true }));
    } else refresh();
  };

  const pct = progress?.percent ? Math.round(progress.percent) : 0;
  const showBar = Boolean(progress?.total) && !error;
  const label =
    PHASE_LABEL[progress?.phase] ??
    ARTIFACT_LABEL[progress?.artifact] ??
    progress?.artifact ??
    "Preparing…";

  // A pending restart outranks everything: the hypervisor feature is enabled
  // on disk but not live, so nothing further can succeed until Windows
  // reboots. Showing progress underneath it would be a lie.
  if (status?.reboot_required) {
    return (
      <Shell>
        <div className="max-w-md rounded-lg border border-amber-600/40 bg-amber-950/20 p-4 text-[13px]">
          <p className="mb-2 font-medium text-amber-400">Restart required</p>
          <p className="mb-3 text-ink-2">
            Windows Hypervisor Platform has been turned on. Windows needs to
            restart before Omni Executor can run the Android VM. Setup will
            carry on by itself afterwards.
          </p>
          <Button
            variant="solid"
            size="sm"
            disabled={rebooting}
            onClick={async () => {
              setRebooting(true);
              await restartWindows();
            }}
          >
            {rebooting ? "Restarting…" : "Restart now"}
          </Button>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      {/* Windows only, and only on an explicit False -- whpx_ok is tri-state
          and `null` means "not determined yet" (e.g. QEMU still installing).
          Showing a virtualization-is-off warning on a machine that is
          actually fine would be worse than saying nothing. */}
      {status?.whpx_ok === false && (
        <div className="max-w-md rounded-lg border border-amber-600/40 bg-amber-950/20 p-4 text-[13px]">
          <p className="mb-2 font-medium text-amber-400">
            Virtualization needs turning on
          </p>
          <p className="mb-3 whitespace-pre-line text-ink-2">
            {status.whpx_hint}
          </p>
          <Button size="sm" variant="solid" disabled={enabling} onClick={onEnable}>
            {enabling ? "Waiting for permission…" : "Turn it on"}
          </Button>
        </div>
      )}

      {!error && (
        <div className="w-full max-w-md">
          <div className="mb-2 flex justify-between text-[11px] text-ink-3">
            <span>{label}</span>
            {showBar && <span>{pct}%</span>}
          </div>
          <div className="h-2 w-full overflow-hidden rounded bg-raised">
            <div
              className={
                showBar
                  ? "h-full bg-live transition-all duration-200"
                  : "h-full w-1/3 animate-pulse bg-live/60"
              }
              style={showBar ? { width: `${pct}%` } : undefined}
            />
          </div>
          <p className="mt-3 text-center text-[11px] text-ink-3">
            This only happens once. It downloads several GB, so it can take a
            while.
          </p>
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
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-6 bg-canvas p-10 text-ink">
      <div className="flex items-center gap-2">
        <CpuIcon className="h-4 w-4 text-ink-3" />
        <h1 className="silk text-ink-2">Setting up Omni Executor</h1>
      </div>
      {children}
    </div>
  );
}
