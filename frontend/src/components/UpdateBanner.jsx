/* Updates: what is stale, and one button to fix it.

   Two kinds, shown together because the user does not care about the
   distinction until they have to act on it:

     runtime  base image + Roblox offsets. A stale one is not cosmetic — it is
              how a machine keeps a bug that was fixed weeks ago.
     app      a build of this program. Needs a restart, so it never happens
              without being asked. */

import { useCallback, useEffect, useState } from "react";
import { api, onEngineEvent } from "../api.js";
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
        } else if (event === "update-done") {
          setProgress(null);
          setBusy(null);
          setError(null);
          refresh();
        } else if (event === "update-error") {
          setProgress(null);
          setBusy(null);
          setError(payload?.message || "Update failed");
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

  const restartIntoUpdate = useCallback(async () => {
    const res = await api("update_app_restart");
    if (!res?.ok) setError(res?.message || "Couldn't restart");
  }, []);

  return { status, progress, busy, error, refresh, startRuntime, startApp, restartIntoUpdate };
}

/** One line above the content when something is out of date. Deliberately not
    a modal: an update is never so urgent that it should take the app away. */
export function UpdateBanner({ updates, onOpenSettings }) {
  const { status, progress, busy } = updates;
  const runtime = status?.runtime?.update;
  const app = status?.app?.update;
  const staged = status?.staged;

  if (!runtime && !app && !staged && !busy) return null;

  const what = staged
    ? `Version ${staged.version} is ready to install`
    : busy === "runtime"
      ? "Updating base images…"
      : busy === "app"
        ? "Downloading the new version…"
        : app && runtime
          ? `App ${status.app.available} and new base images are available`
          : app
            ? `Version ${status.app.available} is available`
            : `New base images are available (${fmtBytes(status.runtime.bytes)})`;

  const pct = progress?.percent ? Math.round(progress.percent) : null;

  return (
    <div className="rule-b flex items-center gap-3 bg-accent/8 px-4 py-2">
      <Lamp tone={busy ? "busy" : "live"} pulse={Boolean(busy)} size={6} />
      <span className="truncate text-[12px] text-ink">
        {what}
        {pct != null && <span className="ml-2 font-mono text-ink-3">{pct}%</span>}
      </span>
      <Button size="sm" className="ml-auto shrink-0" onClick={onOpenSettings}>
        {staged ? "Install" : busy ? "Details" : "Update"}
      </Button>
    </div>
  );
}

/** The full control, in Settings. */
export default function UpdatePanel({ updates, showToast }) {
  const { status, progress, busy, error, refresh, startRuntime, startApp, restartIntoUpdate } =
    updates;
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
          <span className="flex items-center gap-2 text-[11.5px] text-ink-2">
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
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 font-mono text-[11.5px]">
          <Row label="App version" value={app?.current ?? "—"} />
          <Row label="Latest" value={app?.available ?? "—"} />
        </dl>

        {status?.ok === false && (
          <p className="text-[11px] text-ink-3">
            Couldn&apos;t reach the update server — {status.error}. Nothing was changed;
            this is a check, not a failure.
          </p>
        )}

        {runtime?.managed === false && (
          <p className="text-[11px] leading-snug text-ink-3">
            Base images are not managed here — {runtime.reason}
          </p>
        )}

        {runtime?.update && (
          <div className="rule-t pt-4">
            <p className="mb-2 text-[12.5px] text-ink">
              New base images — {fmtBytes(runtime.bytes)}
            </p>
            <p className="mb-3 text-[11px] leading-snug text-ink-3">
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
            <p className="mb-2 text-[12.5px] text-ink">
              Version {app.available}
              {app.bytes ? ` — ${fmtBytes(app.bytes)}` : ""}
            </p>
            {app.canApply ? (
              <Button size="sm" onClick={startApp} disabled={Boolean(busy)}>
                {busy === "app" ? `Downloading… ${pct}%` : "Download"}
              </Button>
            ) : (
              <p className="text-[11px] leading-snug text-ink-3">{app.reason}</p>
            )}
          </div>
        )}

        {staged && (
          <div className="rule-t pt-4">
            <p className="mb-2 text-[12.5px] text-ink">
              Version {staged.version} is downloaded and ready.
            </p>
            <p className="mb-3 text-[11px] leading-snug text-ink-3">
              Installing restarts the app. Stop every instance first — this app holds
              their running state.
            </p>
            <Button variant="solid" size="sm" onClick={restartIntoUpdate}>
              Restart and install
            </Button>
          </div>
        )}

        {error && (
          <p className="rounded-lg border border-danger/35 bg-danger/8 px-3 py-2 text-[12px] text-danger">
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
