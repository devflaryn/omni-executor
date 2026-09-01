/* Updates: what is stale, and one button to fix it.

   Two kinds, shown together because the user does not care about the
   distinction until they have to act on it:

     runtime  base image + Roblox offsets. A stale one is not cosmetic — it is
              how a machine keeps a bug that was fixed weeks ago.
     app      a build of this program. Needs a restart, so it never happens
              without being asked. */

import { useCallback, useEffect, useState } from "react";
import { api, onEngineEvent, saveSettings } from "../api.js";
import { close as closeWindow } from "../window.js";
import { Button, Lamp, Panel, PanelHead } from "./ui.jsx";
import { CpuIcon } from "./icons.jsx";

const fmtBytes = (n) => {
  if (!n) return null;
  const mb = n / 1048576;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
};

/** Shared state so the banner and the Settings panel never disagree. */
export function useUpdates() {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [busy, setBusy] = useState(null); // "runtime" | "app" | null
  const [error, setError] = useState(null);
  // The app is about to replace itself and relaunch — the launch-time
  // auto-update. Terminal: nothing follows it in this process.
  const [applying, setApplying] = useState(false);

  const refresh = useCallback(async () => {
    const res = await api("update_check");
    setStatus(res);
    return res;
  }, []);

  useEffect(
    () =>
      onEngineEvent((event, payload) => {
        if (event === "update-status") {
          setStatus(payload);
        } else if (event === "update-progress") {
          setProgress(payload);
          // A background download the user never asked for still has to look
          // like something is happening rather than like a stall.
          if (payload?.auto) setBusy("app");
        } else if (event === "update-applying") {
          setApplying(true);
        } else if (event === "update-done") {
          setProgress(null);
          setBusy(null);
          setError(null);
          refresh();
        } else if (event === "update-error") {
          setProgress(null);
          setBusy(null);
          // A failed BACKGROUND download is not the user's problem: nobody
          // asked for it, the next check retries, and an error bar for it
          // would be the app complaining about its own housekeeping.
          if (!payload?.auto) setError(payload?.message || "Update failed");
        }
      }),
    [refresh]
  );

  const startRuntime = useCallback(async () => {
    setError(null);
    setBusy("runtime");
    const res = await api("update_runtime");
    if (!res?.ok) {
      setBusy(null);
      setError(res?.message || "Couldn't start the update");
    }
  }, []);

  const startApp = useCallback(async () => {
    setError(null);
    setBusy("app");
    const res = await api("update_app");
    if (!res?.ok) {
      setBusy(null);
      setError(res?.message || "Couldn't download the update");
    }
  }, []);

  /* The backend hands the swap to a helper that waits for THIS APP to exit,
     then closes the window itself — because under Tauri the window belongs to
     the shell, not to Python. The backend used to try to close it and raised
     `AttributeError: 'Api' object has no attribute 'close'` (a pywebview
     leftover), so the helper waited out its 90 s timeout and no update ever
     applied. Closing the shell is what ends both processes, and the shell is
     the one holding omni-exec.exe open. */
  const restartIntoUpdate = useCallback(async () => {
    const res = await api("update_app_restart");
    if (!res?.ok) {
      setError(res?.message || "Couldn't restart");
      return;
    }
    // Give the helper a moment to start waiting on our pid before we go.
    setTimeout(() => {
      closeWindow().catch(() => {
        setError("Update is ready — close Omni Executor to finish installing it.");
      });
    }, 600);
  }, []);

  const setAutoUpdate = useCallback(
    async (on) => {
      await saveSettings({ autoUpdate: on });
      refresh();
    },
    [refresh]
  );

  return {
    status,
    progress,
    busy,
    error,
    applying,
    refresh,
    startRuntime,
    startApp,
    restartIntoUpdate,
    setAutoUpdate,
  };
}

/** One line above the content when something is out of date. Deliberately not
    a modal: an update is never so urgent that it should take the app away.

    The state that matters most is STAGED — a new build is already downloaded
    and one restart away. With auto-update on, that is the only update state a
    user normally sees at all: the check, the download and (at launch) the swap
    all happen without them. While the app is open the restart stays theirs to
    schedule, because this process holds the presence lease and the editor
    buffer, so the banner says exactly that and offers exactly one button. */
export function UpdateBanner({ updates, onOpenSettings }) {
  const { status, progress, busy, error, applying, startApp, restartIntoUpdate } = updates;
  const runtime = status?.runtime?.update;
  const app = status?.app?.update;
  const staged = status?.staged;

  if (!runtime && !app && !staged && !busy && !applying) return null;

  const pct = progress?.percent ? Math.round(progress.percent) : null;
  const auto = status?.autoUpdate !== false;

  // Applying is terminal — the window is about to close and reopen on the new
  // version — so it outranks everything and offers no button to press.
  if (applying) {
    return (
      <Strip tone="busy">
        <span className="truncate text-[13px] text-ink">
          Updating to {status?.app?.available}… the app will restart on its own.
        </span>
      </Strip>
    );
  }

  if (staged) {
    return (
      <Strip tone="live">
        <span className="truncate text-[13px] text-ink">
          <span className="font-medium">Version {staged.version} is ready.</span>{" "}
          <span className="text-ink-2">Restart to update.</span>
        </span>
        <Button variant="solid" size="sm" className="ml-auto shrink-0" onClick={restartIntoUpdate}>
          Restart now
        </Button>
      </Strip>
    );
  }

  const what = busy === "runtime"
    ? "Updating base images…"
    : busy === "app"
      ? `Downloading version ${status?.app?.available ?? ""}…`
      : app && runtime
        ? `App ${status.app.available} and new base images are available`
        : app
          ? `Version ${status.app.available} is available`
          : `New base images are available (${fmtBytes(status.runtime.bytes)})`;

  // With auto-update on, an app-only update needs no button: it is already
  // downloading, and a "Download" button that starts a download already in
  // progress is a button that lies.
  const selfServing = app && !runtime && auto;

  return (
    <Strip tone={busy ? "busy" : "live"} pulse={Boolean(busy)}>
      <span className="truncate text-[13px] text-ink">
        {error || what}
        {pct != null && <span className="ml-2 font-mono text-ink-3">{pct}%</span>}
      </span>
      {/* The button DOES the thing it is labelled with. It used to call
          onOpenSettings for every state, so a button reading "Install" only
          switched tabs — and did nothing visible at all when Settings was
          already open. Only "Details" navigates, because that is what it
          means. */}
      {!selfServing && (
        <Button
          size="sm"
          className="ml-auto shrink-0"
          disabled={Boolean(busy)}
          onClick={app && !runtime ? startApp : onOpenSettings}
        >
          {busy ? "Details" : app && !runtime ? "Download" : "Update"}
        </Button>
      )}
    </Strip>
  );
}

function Strip({ tone, pulse, children }) {
  return (
    <div className="rule-b flex items-center gap-3 bg-accent/8 px-4 py-2">
      <Lamp tone={tone} pulse={pulse} size={6} />
      {children}
    </div>
  );
}

/** Startup popup: a new build is downloaded and one restart away.

    Shown as a modal (not the passive strip) because the user asked to be TOLD
    at launch, with the choice in their hands — "Restart now" applies the staged
    build; "Later" dismisses it for this session and leaves the strip behind so
    it is still one click away. It only appears once a build is actually STAGED,
    so its button applies instantly instead of kicking off a download. */
export function UpdateModal({ updates, onDismiss }) {
  const { status, applying, restartIntoUpdate, error } = updates;
  const staged = status?.staged;
  if (applying) {
    return (
      <Backdrop>
        <Card>
          <h2 className="text-[16px] font-semibold text-ink">Updating…</h2>
          <p className="mt-2 text-[13.5px] leading-snug text-ink-2">
            Installing version {status?.app?.available}. The app will restart on its own.
          </p>
        </Card>
      </Backdrop>
    );
  }
  if (!staged) return null;
  return (
    <Backdrop>
      <Card>
        <div className="flex items-center gap-2.5">
          <Lamp tone="live" size={7} />
          <h2 className="text-[16px] font-semibold text-ink">Update found</h2>
        </div>
        <p className="mt-2.5 text-[13.5px] leading-snug text-ink-2">
          <span className="font-medium text-ink">Version {staged.version}</span> is downloaded
          and ready. Restarting closes this window and reopens it on the new version — running
          instances are separate processes and keep going.
        </p>
        {error && (
          <p className="mt-3 rounded-lg border border-danger/35 bg-danger/8 px-3 py-2 text-[13px] text-danger">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <Button size="sm" onClick={onDismiss}>Later</Button>
          <Button variant="solid" size="sm" onClick={restartIntoUpdate}>
            Restart &amp; apply
          </Button>
        </div>
      </Card>
    </Backdrop>
  );
}

function Backdrop({ children }) {
  return (
    <div className="fixed inset-0 z-[9000] flex items-center justify-center bg-black/45 p-6 backdrop-blur-sm">
      {children}
    </div>
  );
}

function Card({ children }) {
  return (
    <div className="animate-rise w-full max-w-[380px] rounded-3xl border border-line bg-surface p-5 shadow-2xl">
      {children}
    </div>
  );
}

/** The full control, in Settings. */
export default function UpdatePanel({ updates, showToast }) {
  const {
    status,
    progress,
    busy,
    error,
    refresh,
    startRuntime,
    startApp,
    restartIntoUpdate,
    setAutoUpdate,
  } = updates;
  const [checking, setChecking] = useState(false);

  const checkNow = async () => {
    setChecking(true);
    const res = await refresh();
    setChecking(false);
    if (!res?.ok) {
      showToast(res?.error || "Couldn't reach the update server", "error");
      return;
    }
    const any = res.runtime?.update || res.app?.update;
    showToast(any ? "Updates available" : "Everything is up to date", any ? "info" : "success");
  };

  const app = status?.app;
  const runtime = status?.runtime;
  const staged = status?.staged;
  const pct = progress?.percent ? Math.round(progress.percent) : 0;

  return (
    <Panel className="overflow-hidden">
      <PanelHead
        icon={CpuIcon}
        title="Updates"
        right={
          <span className="flex items-center gap-2 text-[12.5px] text-ink-2">
            <Lamp
              tone={status?.ok === false ? "fault" : runtime?.update || app?.update ? "busy" : "live"}
              size={6}
            />
            {status?.ok === false
              ? "Offline"
              : runtime?.update || app?.update
                ? "Update available"
                : "Up to date"}
          </span>
        }
      />
      <div className="flex flex-col gap-4 p-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 font-mono text-[12.5px]">
          <Row label="App version" value={app?.current ?? "—"} />
          <Row label="Latest" value={app?.available ?? "—"} />
        </dl>

        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            className="check mt-0.5"
            checked={status?.autoUpdate !== false}
            onChange={(e) => setAutoUpdate(e.target.checked)}
          />
          <span className="text-[13px] leading-snug text-ink">
            Update automatically
            <span className="mt-0.5 block text-[12px] text-ink-3">
              Installs a new version on launch, when nothing is running yet. While the app is
              open it only downloads — you choose when to restart.
            </span>
          </span>
        </label>

        {status?.ok === false && (
          <p className="text-[12px] text-ink-3">
            Couldn&apos;t reach the update server — {status.error}. Nothing was changed;
            this is a check, not a failure.
          </p>
        )}

        {runtime?.managed === false && (
          <p className="text-[12px] leading-snug text-ink-3">
            Base images are not managed here — {runtime.reason}
          </p>
        )}

        {runtime?.update && (
          <div className="rule-t pt-4">
            <p className="mb-2 text-[13.5px] text-ink">
              New base images — {fmtBytes(runtime.bytes)}
            </p>
            <p className="mb-3 text-[12px] leading-snug text-ink-3">
              {runtime.artifacts.map((a) => a.name).join(", ")}. Stop every instance first;
              a running VM has these files open.
            </p>
            <Button size="sm" onClick={startRuntime} disabled={Boolean(busy)}>
              {busy === "runtime" ? `Downloading… ${pct}%` : "Download and install"}
            </Button>
          </div>
        )}

        {app?.update && !staged && (
          <div className="rule-t pt-4">
            <p className="mb-2 text-[13.5px] text-ink">
              Version {app.available}
              {app.bytes ? ` — ${fmtBytes(app.bytes)}` : ""}
            </p>
            {app.canApply ? (
              <Button size="sm" onClick={startApp} disabled={Boolean(busy)}>
                {busy === "app" ? `Downloading… ${pct}%` : "Download"}
              </Button>
            ) : (
              <p className="text-[12px] leading-snug text-ink-3">{app.reason}</p>
            )}
          </div>
        )}

        {staged && (
          <div className="rule-t pt-4">
            <p className="mb-2 text-[13.5px] text-ink">
              Version {staged.version} is downloaded and ready.
            </p>
            <p className="mb-3 text-[12px] leading-snug text-ink-3">
              Restarting closes this window and reopens it on the new version. Running
              instances are unaffected — they are separate processes and keep running.
            </p>
            <Button variant="solid" size="sm" onClick={restartIntoUpdate}>
              Restart now
            </Button>
          </div>
        )}

        {error && (
          <p className="rounded-lg border border-danger/35 bg-danger/8 px-3 py-2 text-[13px] text-danger">
            {error}
          </p>
        )}

        <div className="rule-t flex gap-2 pt-4">
          <Button size="sm" onClick={checkNow} disabled={checking || Boolean(busy)}>
            {checking ? "Checking…" : "Check for updates"}
          </Button>
        </div>
      </div>
    </Panel>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line-soft pb-2">
      <dt className="silk text-ink-3">{label}</dt>
      <dd className="truncate text-ink-2">{value}</dd>
    </div>
  );
}
