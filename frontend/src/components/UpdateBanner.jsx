/* Updates: what is stale, and one button to fix it.

   Two kinds, shown together because the user does not care about the
   distinction until they have to act on it:

     runtime  base image + Roblox offsets. A stale one is not cosmetic — it is
              how a machine keeps a bug that was fixed weeks ago.
     app      a build of this program. Needs a restart, so it never happens
              without being asked. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, onEngineEvent, saveSettings } from "../api.js";
import { close as closeWindow } from "../window.js";
import { Button, Lamp, MiniSwitch } from "./ui.jsx";

// What each update channel is, in the words Settings uses for it.
const CHANNELS = {
  prod: { label: "Prod", hint: "Tested releases. Recommended for most people." },
  beta: { label: "Beta", hint: "New versions earlier. They may have bugs." },
  dev: { label: "Dev", hint: "Internal builds, updated often. Things can break." },
};

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
  // The version a launch is installing by itself, or null. An "Updating" screen
  // covers the app until the window closes.
  const [applying, setApplying] = useState(null);
  const closing = useRef(false);

  /* The last step of an update at launch. The backend has handed the swap to a
     helper that waits for this app to exit, and the window is the shell's to close. */
  const closeForUpdate = useCallback(() => {
    if (closing.current) return;
    closing.current = true;
    setTimeout(() => {
      closeWindow().catch(() => {
        setError("Update is ready. Close Omni Executor to finish installing it.");
      });
    }, 600);
  }, []);

  const followLaunchUpdate = useCallback(
    (update) => {
      if (!update) return;
      setApplying(update.version);
      if (update.closing) closeForUpdate();
    },
    [closeForUpdate]
  );

  const refresh = useCallback(async () => {
    const res = await api("update_check");
    setStatus(res);
    followLaunchUpdate(res?.launchUpdate);
    return res;
  }, [followLaunchUpdate]);

  // Ask once as well. The watcher's first report can land before this listens,
  // and Settings needs a status to show the channel at all.
  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(
    () =>
      onEngineEvent((event, payload) => {
        if (event === "update-status") {
          setStatus(payload);
          followLaunchUpdate(payload?.launchUpdate);
        } else if (event === "update-progress") {
          setProgress(payload);
          // A background download the user never asked for still has to look
          // like something is happening rather than like a stall.
          if (payload?.auto) setBusy("app");
        } else if (event === "update-applying") {
          followLaunchUpdate(payload);
        } else if (event === "update-done") {
          setProgress(null);
          setBusy(null);
          setError(null);
          refresh();
        } else if (event === "update-error") {
          setProgress(null);
          setBusy(null);
          setApplying(null);
          // A failed BACKGROUND download is not the user's problem: nobody
          // asked for it, the next check retries, and an error bar for it
          // would be the app complaining about its own housekeeping.
          if (!payload?.auto) setError(payload?.message || "Update failed");
        }
      }),
    [refresh, followLaunchUpdate]
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

  /* Move to an older build on a steadier channel. Only ever a button press. */
  const startSwitch = useCallback(async () => {
    setError(null);
    setBusy("app");
    const res = await api("update_app", true);
    if (!res?.ok) {
      setBusy(null);
      setError(res?.message || "Couldn't download that version");
    }
  }, []);

  const setChannel = useCallback(async (name) => {
    setError(null);
    const res = await api("update_set_channel", name);
    if (res?.error === "channel_forbidden" || res?.error === "already_running") {
      setError(res.message);
      return false;
    }
    setStatus(res);
    return true;
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
    startSwitch,
    setChannel,
    restartIntoUpdate,
    setAutoUpdate,
  };
}

/** One line above the content when something is out of date. Deliberately not
    a modal: an update is never so urgent that it should take the app away.

    With auto-update on, the only state a user normally sees here is STAGED: a new
    build is already downloaded and one restart away. Until then there is nothing
    for them to do, so nothing shows. The restart stays theirs to schedule, because
    this process holds the presence lease and whatever is in the editor. */
export function UpdateBanner({ updates, onOpenSettings }) {
  const { status, progress, busy, error, startApp, restartIntoUpdate } = updates;
  const runtime = status?.runtime?.update;
  const app = status?.app?.update;
  const staged = status?.staged;

  if (!runtime && !app && !staged && !busy) return null;

  const pct = progress?.percent ? Math.round(progress.percent) : null;
  const auto = status?.autoUpdate !== false;

  if (staged) {
    return (
      <Strip tone="live">
        <span className="truncate text-[13px] text-ink">
          {staged.downgrade
            ? `Version ${staged.version} is ready. Restart to switch.`
            : "Update is available. Restart to update."}
        </span>
        <Button variant="solid" size="sm" className="ml-auto shrink-0" onClick={restartIntoUpdate}>
          Restart now
        </Button>
      </Strip>
    );
  }

  // Downloading by itself, so there is nothing to say yet and no button to press.
  if (auto && app && !runtime && status?.app?.canApply !== false) return null;

  const what = busy === "runtime"
    ? "Updating the engine…"
    : busy === "app"
      ? `Downloading version ${status?.app?.available ?? ""}…`
      : app && runtime
        ? `App ${status.app.available} and an engine update are available`
        : app
          ? `Version ${status.app.available} is available`
          : `An engine update is available (${fmtBytes(status.runtime.bytes)})`;

  return (
    <Strip tone={busy ? "busy" : "live"} pulse={Boolean(busy)}>
      <span className="truncate text-[13px] text-ink">
        {error || what}
        {pct != null && <span className="ml-2 font-mono text-ink-3">{pct}%</span>}
      </span>
      {/* The button DOES the thing it is labelled with. Only "Details" and
          "Update" navigate, because that is what they mean. */}
      <Button
        size="sm"
        className="ml-auto shrink-0"
        disabled={Boolean(busy)}
        onClick={app && !runtime ? startApp : onOpenSettings}
      >
        {busy ? "Details" : app && !runtime ? "Download" : "Update"}
      </Button>
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

/** Covers the app while a launch installs an update by itself, so nobody starts
    work in a window that is about to close. It clears if the download fails. */
export function UpdatingScreen({ updates }) {
  const { applying, progress, error } = updates;
  const pct = progress?.percent ? Math.round(progress.percent) : 0;
  return (
    <Backdrop>
      <Card>
        <h2 className="text-[16px] font-semibold text-ink">Updating Omni Executor</h2>
        <p className="mt-2 text-[13.5px] leading-snug text-ink-2">
          Installing version {applying}. The app will close and reopen on its own.
        </p>
        <div
          className="mt-4 h-1 overflow-hidden rounded-full bg-raised"
          role="progressbar"
          aria-label="Download progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
        >
          <div className="h-full rounded-full bg-accent transition-[width] duration-200" style={{ width: `${pct}%` }} />
        </div>
        {error && (
          <p className="mt-3 rounded-lg border border-danger/35 bg-danger/8 px-3 py-2 text-[13px] text-danger">
            {error}
          </p>
        )}
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

/** A titled sheet of rows, in Settings. */
export function SettingsSection({ title, right, children }) {
  return (
    <section>
      <div className="mb-2.5 flex items-baseline justify-between gap-3 px-1">
        <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
        {right}
      </div>
      <div className="settings-sheet">{children}</div>
    </section>
  );
}

export function SettingRow({ label, hint, htmlFor, children }) {
  const Label = htmlFor ? "label" : "span";
  return (
    <div className="settings-row">
      <span className="min-w-0">
        <Label htmlFor={htmlFor} className="block text-[13.5px] font-medium text-ink">
          {label}
        </Label>
        {hint && <span className="mt-0.5 block text-[12.5px] leading-snug text-ink-3">{hint}</span>}
      </span>
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
    startSwitch,
    setChannel,
    restartIntoUpdate,
    setAutoUpdate,
  } = updates;
  const [checking, setChecking] = useState(false);
  const [switching, setSwitching] = useState(false);

  const pickChannel = async (id) => {
    if (id === channel?.active) return;
    setSwitching(true);
    const ok = await setChannel(id);
    setSwitching(false);
    if (ok) showToast(`Now following ${CHANNELS[id].label}`, "success");
  };

  const checkNow = async () => {
    setChecking(true);
    const res = await refresh();
    setChecking(false);
    if (!res?.ok) {
      showToast(res?.error || "Couldn't reach the update server", "error");
      return;
    }
    const any = res.staged || res.runtime?.update || res.app?.update;
    showToast(any ? "Update available" : "You're up to date", any ? "info" : "success");
  };

  const app = status?.app;
  const runtime = status?.runtime;
  const staged = status?.staged;
  const pct = progress?.percent ? Math.round(progress.percent) : 0;
  const channel = status?.channel;
  const activeLabel = CHANNELS[channel?.active]?.label;
  const pending = staged || runtime?.update || app?.update;

  return (
    <SettingsSection title="Updates">
      <SettingRow
        label={app?.current ? `Version ${app.current}` : "Version"}
        hint={
          status?.ok === false
            ? "Couldn't reach the update server. Nothing has changed."
            : staged
              ? "An update is downloaded and ready."
              : pending
                ? "An update is available."
                : "You're up to date."
        }
      >
        {staged ? (
          <Button variant="solid" size="sm" onClick={restartIntoUpdate}>
            Restart now
          </Button>
        ) : (
          <Button size="sm" onClick={checkNow} disabled={checking || Boolean(busy)}>
            {checking ? "Checking…" : "Check for updates"}
          </Button>
        )}
      </SettingRow>

      {channel && (
        <SettingRow
          label="Update channel"
          hint={
            channel.picked !== channel.active
              ? `This account can't use ${CHANNELS[channel.picked]?.label ?? channel.picked}, so this computer follows ${activeLabel}.`
              : CHANNELS[channel.active]?.hint
          }
        >
          <div role="radiogroup" aria-label="Update channel" className="segmented">
            {channel.allowed.map((id) => (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={channel.active === id}
                disabled={switching || Boolean(busy)}
                onClick={() => pickChannel(id)}
                className="segmented-item"
              >
                {CHANNELS[id]?.label ?? id}
              </button>
            ))}
          </div>
        </SettingRow>
      )}

      <SettingRow
        label="Update automatically"
        hint="Installs a new version when the app opens. If one arrives while it's open, it downloads in the background and you choose when to restart."
      >
        <MiniSwitch
          label="Update automatically"
          checked={status?.autoUpdate !== false}
          onChange={setAutoUpdate}
        />
      </SettingRow>

      {runtime?.managed === false && <p className="settings-note">{runtime.reason}</p>}

      {runtime?.update && (
        <SettingRow
          label={`Engine update, ${fmtBytes(runtime.bytes)}`}
          hint="Stop every instance first, because a running one has these files open."
        >
          <Button size="sm" onClick={startRuntime} disabled={Boolean(busy)}>
            {busy === "runtime" ? `Downloading… ${pct}%` : "Install"}
          </Button>
        </SettingRow>
      )}

      {app?.update && !staged && (
        <SettingRow
          label={`Version ${app.available}${app.bytes ? `, ${fmtBytes(app.bytes)}` : ""}`}
          hint={app.canApply ? null : app.reason}
        >
          {app.canApply && (
            <Button size="sm" onClick={startApp} disabled={Boolean(busy)}>
              {busy === "app" ? `Downloading… ${pct}%` : "Download"}
            </Button>
          )}
        </SettingRow>
      )}

      {app?.downgrade && !staged && (
        <SettingRow
          label={`You're on ${app.current}, which is newer than ${activeLabel} (${app.available})`}
          hint={app.canApply ? `You'll stay on it until ${activeLabel} catches up, or you can switch now.` : app.reason}
        >
          {app.canApply && (
            <Button size="sm" onClick={startSwitch} disabled={Boolean(busy)}>
              {busy === "app" ? `Downloading… ${pct}%` : `Install ${app.available}`}
            </Button>
          )}
        </SettingRow>
      )}

      {error && <p className="settings-note text-danger">{error}</p>}
    </SettingsSection>
  );
}
