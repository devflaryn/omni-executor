/* Settings: who you are, how the app updates, and how it looks.

   Each section is one sheet of rows, the same surface the Earn tab uses. A row
   says what a setting is on the left and holds its control on the right. */

import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Button } from "./ui.jsx";
import { MoonIcon, SunIcon } from "./icons.jsx";
import UpdatePanel, { SettingRow, SettingsSection } from "./UpdateBanner.jsx";

function initials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean);
  return (parts.slice(0, 2).map((p) => p[0]).join("") || "?").toUpperCase();
}

const THEMES = [
  { id: "dark", label: "Dark", Icon: MoonIcon },
  { id: "light", label: "Light", Icon: SunIcon },
];

export default function SettingsView({
  active,
  theme,
  onTheme,
  profile,
  onProfile,
  showToast,
  auth,
  onAuthChange,
  updates,
}) {
  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 pt-3 pb-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[720px] flex-col gap-7">
        <ProfileSection
          profile={profile}
          onProfile={onProfile}
          auth={auth}
          onAuthChange={onAuthChange}
          showToast={showToast}
        />

        {updates && <UpdatePanel updates={updates} showToast={showToast} />}

        <SettingsSection title="Appearance">
          <SettingRow label="Theme" hint="Choose how the app looks.">
            <div role="radiogroup" aria-label="Theme" className="segmented">
              {THEMES.map(({ id, label, Icon }) => (
                <button
                  key={id}
                  type="button"
                  role="radio"
                  aria-checked={theme === id}
                  onClick={() => onTheme(id)}
                  className="segmented-item"
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </button>
              ))}
            </div>
          </SettingRow>
        </SettingsSection>

        <p className="silk pb-2 text-center text-ink-3">
          Omni Executor{updates?.status?.app?.current ? ` ${updates.status.app.current}` : ""}
        </p>
      </div>
    </div>
  );
}

/* Your Omni account and how you appear in the app. The device name is what your
   other machines SEE: it is the "Mac mini" in "Running on Mac mini". */
function ProfileSection({ profile, onProfile, auth, onAuthChange, showToast }) {
  const [name, setName] = useState(profile.name);
  const [tag, setTag] = useState(profile.tag);
  const [device, setDevice] = useState(auth?.device?.name || "");
  const [key, setKey] = useState("");
  const [redeeming, setRedeeming] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [saved, setSaved] = useState(false);
  const saveTimer = useRef(null);
  const savedTimer = useRef(null);
  const nameTimer = useRef(null);

  useEffect(() => {
    setName(profile.name);
    setTag(profile.tag);
  }, [profile.name, profile.tag]);

  useEffect(() => {
    setDevice(auth?.device?.name || "");
  }, [auth?.device?.name]);

  const flashSaved = () => {
    setSaved(true);
    clearTimeout(savedTimer.current);
    savedTimer.current = setTimeout(() => setSaved(false), 1500);
  };

  const scheduleSave = (nextName, nextTag) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      onProfile({ name: nextName.trim(), tag: nextTag.trim() });
      flashSaved();
    }, 500);
  };

  const renameDevice = (next) => {
    setDevice(next);
    clearTimeout(nameTimer.current);
    nameTimer.current = setTimeout(async () => {
      await api("set_device_name", next);
      flashSaved();
    }, 600);
  };

  const sub = auth?.subscription || {};
  const premium = sub.tier === "premium";
  // Free is the resting state, not a failure. An expired plan still says so,
  // because "your 90 days ran out" is the useful half of it.
  const plan = premium
    ? sub.plan === "lifetime"
      ? "Premium, lifetime"
      : `Premium, ${sub.daysRemaining} day${sub.daysRemaining === 1 ? "" : "s"} left`
    : sub.plan
      ? `Free, ${sub.planLabel || sub.plan} expired`
      : "Free";

  const redeem = async () => {
    if (redeeming || !key.trim()) return;
    setRedeeming(true);
    const res = await api("keys_redeem", key.trim());
    setRedeeming(false);
    if (!res?.ok) {
      showToast(res?.message || "That key could not be redeemed", "error");
      return;
    }
    setKey("");
    const next = res.subscription || {};
    showToast(
      next.plan === "lifetime"
        ? "Redeemed. This account is on the lifetime plan"
        : `Redeemed. Premium for ${next.daysRemaining} more day${next.daysRemaining === 1 ? "" : "s"}`,
      "success"
    );
    onAuthChange?.();
  };

  const syncNow = async () => {
    setSyncing(true);
    const res = await api("cloud_sync");
    setSyncing(false);
    if (!res?.ok) {
      showToast(res?.message || "Sync failed", "error");
      return;
    }
    const up = res.pushed?.length || 0;
    const down = res.pulled?.length || 0;
    const foreign = res.foreign?.length || 0;
    if (foreign) {
      // Worth saying out loud: these are accounts another Omni user left on this
      // machine, and the app is deliberately not uploading them into this one.
      showToast(
        `Synced ${up} up, ${down} down. Left ${foreign} account${
          foreign === 1 ? "" : "s"
        } belonging to another Omni user alone`,
        "info"
      );
      return;
    }
    showToast(
      up || down ? `Synced ${up} up, ${down} down` : "Already up to date",
      "success"
    );
  };

  const signOut = async () => {
    await api("auth_logout");
    onAuthChange?.();
  };

  return (
    <SettingsSection
      title="Profile"
      right={
        <span
          className={`text-[12.5px] text-live transition-opacity duration-300 ${saved ? "opacity-100" : "opacity-0"}`}
        >
          Saved
        </span>
      }
    >
      <div className="flex items-center gap-4 p-5">
        <span
          className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-raised text-[18px] font-semibold text-ink"
          aria-hidden="true"
        >
          {initials(name || auth?.username || "?")}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[17px] font-semibold tracking-[-0.01em] text-ink">
            {name || auth?.username || "Guest"}
          </p>
          <p className="truncate text-[13px] text-ink-3">
            {[auth?.username, auth?.email].filter(Boolean).join(", ")}
          </p>
        </div>
        <span
          className={`shrink-0 rounded-md border px-2 py-[3px] text-[12px] font-medium ${
            premium ? "border-premium/40 text-premium" : "border-line text-ink-2"
          }`}
        >
          {plan}
        </span>
      </div>

      {auth?.stale && (
        <p className="settings-note">
          Working offline: {auth.message || "the server could not be reached"}. Your plan is
          shown from the last successful check.
        </p>
      )}

      <SettingRow label="Display name" htmlFor="profile-name">
        <input
          id="profile-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            scheduleSave(e.target.value, tag);
          }}
          className="input settings-input"
          maxLength={40}
          autoComplete="off"
          spellCheck={false}
        />
      </SettingRow>

      <SettingRow label="Status" htmlFor="profile-tag">
        <input
          id="profile-tag"
          value={tag}
          onChange={(e) => {
            setTag(e.target.value);
            scheduleSave(name, e.target.value);
          }}
          className="input settings-input"
          maxLength={60}
          autoComplete="off"
          spellCheck={false}
          placeholder="Scripting away…"
        />
      </SettingRow>

      <SettingRow
        label="This device"
        hint="Your other computers show this name when an account is running here."
        htmlFor="device-name"
      >
        <input
          id="device-name"
          className="input settings-input"
          value={device}
          maxLength={40}
          spellCheck={false}
          onChange={(e) => renameDevice(e.target.value)}
        />
      </SettingRow>

      <SettingRow
        label="License key"
        hint={
          premium
            ? "Redeeming again adds the days to what's left, or converts to lifetime."
            : "Have a key? Redeem it to go premium."
        }
        htmlFor="redeem-key"
      >
        <div className="flex w-[300px] max-w-full gap-2">
          <input
            id="redeem-key"
            className="input min-w-0 flex-1"
            placeholder="OMNI-XXXX-XXXX-XXXX"
            value={key}
            spellCheck={false}
            disabled={redeeming}
            // Keys are printed and dictated in upper case; accepting lower case
            // silently and then rejecting it would be needless friction.
            onChange={(e) => setKey(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && redeem()}
          />
          <Button onClick={redeem} disabled={redeeming || !key.trim()}>
            {redeeming ? "Redeeming…" : "Redeem"}
          </Button>
        </div>
      </SettingRow>

      <div className="settings-row">
        <Button size="sm" onClick={syncNow} disabled={syncing}>
          {syncing ? "Syncing…" : "Sync accounts"}
        </Button>
        <Button size="sm" onClick={signOut}>
          Sign out
        </Button>
      </div>
    </SettingsSection>
  );
}
