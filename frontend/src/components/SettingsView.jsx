/* Settings: identity, appearance, and the engine's own facts. */

import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useEngine } from "../engine.jsx";
import { Button, Field, Lamp, Panel, PanelHead, Toggle } from "./ui.jsx";
import { CpuIcon, GearIcon, MoonIcon, SunIcon, UserIcon } from "./icons.jsx";
import UpdatePanel from "./UpdateBanner.jsx";

function initials(name) {
  const parts = String(name).trim().split(/\s+/).filter(Boolean);
  return (parts.slice(0, 2).map((p) => p[0]).join("") || "?").toUpperCase();
}

export default function SettingsView({
  active,
  theme,
  onTheme,
  compact,
  onCompact,
  profile,
  onProfile,
  showToast,
  auth,
  onAuthChange,
  updates,
}) {
  const engine = useEngine();
  const [name, setName] = useState(profile.name);
  const [tag, setTag] = useState(profile.tag);
  const [saved, setSaved] = useState(false);
  const saveTimer = useRef(null);
  const savedTimer = useRef(null);

  const [bases, setBases] = useState(null);
  const [baseBusy, setBaseBusy] = useState(false);

  useEffect(() => {
    setName(profile.name);
    setTag(profile.tag);
  }, [profile.name, profile.tag]);

  // Base images only exist once the engine is present; ask once.
  useEffect(() => {
    if (engine.backend !== true) return;
    api("engine_bases").then((res) => {
      setBases(Array.isArray(res?.bases) ? res.bases : Array.isArray(res) ? res : []);
    });
  }, [engine.backend]);

  const scheduleSave = (nextName, nextTag) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      onProfile({ name: nextName.trim(), tag: nextTag.trim() });
      setSaved(true);
      clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    }, 500);
  };

  const chooseBase = async (tagValue) => {
    setBaseBusy(true);
    const res = await api("engine_use_base", tagValue);
    setBaseBusy(false);
    showToast(res.message || (res.ok ? "Default base updated" : "Couldn't change the base"), res.ok ? "success" : "error");
  };

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[640px] flex-col gap-4">
        <OmniAccountPanel auth={auth} onAuthChange={onAuthChange} showToast={showToast} />

        {updates && <UpdatePanel updates={updates} showToast={showToast} />}

        {/* Profile */}
        <Panel className="overflow-hidden">
          <PanelHead
            icon={UserIcon}
            title="Profile"
            right={
              <span
                className={`silk text-live transition-opacity duration-300 ${saved ? "opacity-100" : "opacity-0"}`}
              >
                Saved
              </span>
            }
          />
          <div className="flex items-start gap-4 p-4">
            <span
              className="flex h-14 w-14 shrink-0 items-center justify-center rounded-xl border border-accent/40
                         bg-accent/12 font-mono text-lg font-bold text-accent"
              aria-hidden="true"
            >
              {initials(name || "?")}
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-3">
              <Field label="Display name" htmlFor="profile-name">
                <input
                  id="profile-name"
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    scheduleSave(e.target.value, tag);
                  }}
                  className="input"
                  maxLength={40}
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
              <Field label="Status" htmlFor="profile-tag">
                <input
                  id="profile-tag"
                  value={tag}
                  onChange={(e) => {
                    setTag(e.target.value);
                    scheduleSave(name, e.target.value);
                  }}
                  className="input"
                  maxLength={60}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="Scripting away…"
                />
              </Field>
            </div>
          </div>
        </Panel>

        {/* Appearance */}
        <Panel className="overflow-hidden">
          <PanelHead icon={GearIcon} title="Appearance" />
          <div className="flex flex-col gap-4 p-4">
            <div className="flex items-center justify-between gap-4">
              <span>
                <span className="block text-[12.5px] font-medium text-ink">Theme</span>
                <span className="block text-[11px] text-ink-3">
                  Dark is the default sheet; light is the daylight version.
                </span>
              </span>
              <div
                role="radiogroup"
                aria-label="Theme"
                className="flex shrink-0 gap-1 rounded-lg border border-line bg-raised p-1"
              >
                {[
                  { id: "dark", label: "Dark", Icon: MoonIcon },
                  { id: "light", label: "Light", Icon: SunIcon },
                ].map(({ id, label, Icon }) => (
                  <button
                    key={id}
                    type="button"
                    role="radio"
                    aria-checked={theme === id}
                    onClick={() => onTheme(id)}
                    className={`ring-focus flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[11.5px]
                                font-semibold transition-colors duration-150 ${
                                  theme === id
                                    ? "bg-accent text-accent-ink"
                                    : "text-ink-2 hover:text-ink"
                                }`}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div className="rule-t pt-4">
              <Toggle
                id="compact-sidebar"
                checked={compact}
                onChange={onCompact}
                label="Compact sidebar"
                hint="Collapse the rail to icons and give the panels more room."
              />
            </div>
          </div>
        </Panel>

        {/* Engine */}
        <Panel className="overflow-hidden">
          <PanelHead
            icon={CpuIcon}
            title="Engine"
            right={
              <span className="flex items-center gap-2 text-[11.5px] text-ink-2">
                <Lamp tone={engine.health.tone} pulse={engine.health.tone === "busy"} size={6} />
                {engine.health.label}
              </span>
            }
          />
          <div className="flex flex-col gap-4 p-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 font-mono text-[11.5px]">
              <Row label="Contract" value={engine.version?.contract ?? "—"} />
              <Row
                label="Arch aware"
                value={engine.version?.arch_aware == null ? "—" : engine.version.arch_aware ? "yes" : "no"}
              />
              <Row label="QEMU" value={fmtFlag(engine.doctor?.qemu_present)} />
              <Row label="adb" value={fmtFlag(engine.doctor?.adb_present)} />
            </dl>

            {bases?.length > 0 && (
              <div className="rule-t pt-4">
                <Field
                  label="Base image for new accounts"
                  htmlFor="base-select"
                  hint="Existing instances keep the base they were created with."
                >
                  <select
                    id="base-select"
                    defaultValue=""
                    disabled={baseBusy}
                    onChange={(e) => e.target.value && chooseBase(e.target.value)}
                    className="input cursor-pointer"
                  >
                    <option value="">Choose a base…</option>
                    {bases.map((b) => {
                      const tagValue = typeof b === "string" ? b : b.tag;
                      const arch = typeof b === "string" ? null : b.arch;
                      return (
                        <option key={tagValue} value={tagValue}>
                          {arch ? `${tagValue} (${arch})` : tagValue}
                        </option>
                      );
                    })}
                  </select>
                </Field>
              </div>
            )}

            <div className="rule-t flex gap-2 pt-4">
              <Button size="sm" onClick={engine.refreshDoctor} disabled={engine.backend !== true}>
                Re-check engine
              </Button>
              <Button
                variant="solid"
                size="sm"
                onClick={engine.runSetup}
                disabled={engine.backend !== true || engine.settingUp}
              >
                {engine.settingUp ? "Installing…" : "Run setup"}
              </Button>
            </div>
          </div>
        </Panel>

        <p className="silk pb-2 text-center text-ink-3">Omni Executor · v2.0</p>
      </div>
    </div>
  );
}

/* The Omni account: which login this machine is using, what the plan is, what
   this machine calls itself (that name is what other devices SEE — it is the
   "Mac mini" in "Running on Mac mini"), and the way out. */
function OmniAccountPanel({ auth, onAuthChange, showToast }) {
  const [device, setDevice] = useState(auth?.device?.name || "");
  const [syncing, setSyncing] = useState(false);
  const [key, setKey] = useState("");
  const [redeeming, setRedeeming] = useState(false);
  const nameTimer = useRef(null);

  useEffect(() => {
    setDevice(auth?.device?.name || "");
  }, [auth?.device?.name]);

  const sub = auth?.subscription || {};
  const premium = sub.tier === "premium";
  // Free is the resting state now, not a failure — an account with no plan and
  // an account whose plan lapsed are both simply "Free", and the expired case
  // still says so because "your 90 days ran out" is the useful half of it.
  const planLine = premium
    ? sub.plan === "lifetime"
      ? "Premium · lifetime"
      : `Premium · ${sub.daysRemaining} day${sub.daysRemaining === 1 ? "" : "s"} left`
    : sub.plan
      ? `Free · ${sub.planLabel || sub.plan} expired`
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
        ? "Redeemed — this account is on the lifetime plan"
        : `Redeemed — premium for ${next.daysRemaining} more day${next.daysRemaining === 1 ? "" : "s"}`,
      "success"
    );
    onAuthChange?.();
  };

  const renameDevice = (next) => {
    setDevice(next);
    clearTimeout(nameTimer.current);
    nameTimer.current = setTimeout(() => api("set_device_name", next), 600);
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
      // Worth saying out loud rather than hiding in a count: these are
      // accounts another Omni user left on this machine, and the app is
      // deliberately not uploading them into the current user's account.
      showToast(
        `Synced — ${up} up, ${down} down · left ${foreign} account${
          foreign === 1 ? "" : "s"
        } belonging to another Omni user alone`,
        "info"
      );
      return;
    }
    showToast(
      up || down ? `Synced — ${up} uploaded, ${down} downloaded` : "Already up to date",
      "success"
    );
  };

  const signOut = async () => {
    await api("auth_logout");
    onAuthChange?.();
  };

  return (
    <Panel className="overflow-hidden">
      <PanelHead
        icon={UserIcon}
        title="Omni account"
        right={
          <span className="flex items-center gap-2 text-[11.5px] text-ink-2">
            {/* "off", not "fault": free is a normal state of a working
                account, and a red lamp would read as something being broken. */}
            <Lamp tone={premium ? "live" : "off"} size={6} />
            {planLine}
          </span>
        }
      />
      <div className="flex flex-col gap-4 p-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 font-mono text-[11.5px]">
          <Row label="Username" value={auth?.username || "—"} />
          <Row label="Signed in" value={auth?.email || "—"} />
          <Row label="Server" value={(auth?.apiBase || "").replace(/^https?:\/\//, "") || "—"} />
        </dl>

        {/* The only place a key is entered. It used to be on the sign-up form,
            where it decided whether you got an account at all; now it buys time
            on an account you already have, which is a Settings action. */}
        <Field
          label="License key"
          htmlFor="redeem-key"
          hint={
            premium
              ? "Redeeming again stacks the days onto what is left, or converts to lifetime."
              : "Have a key? Redeem it to go premium."
          }
        >
          <div className="flex gap-2">
            <input
              id="redeem-key"
              className="input font-mono tracking-wide"
              placeholder="OMNI-XXXX-XXXX-XXXX"
              value={key}
              spellCheck={false}
              disabled={redeeming}
              // Keys are printed and dictated in upper case; accepting lower
              // case silently and then rejecting it would be needless friction.
              onChange={(e) => setKey(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === "Enter" && redeem()}
            />
            <Button variant="solid" onClick={redeem} disabled={redeeming || !key.trim()}>
              {redeeming ? "Redeeming…" : "Redeem"}
            </Button>
          </div>
        </Field>

        <Field
          label="This device"
          htmlFor="device-name"
          hint="Your other machines show this name when an account is running here."
        >
          <input
            id="device-name"
            className="input"
            value={device}
            maxLength={40}
            spellCheck={false}
            onChange={(e) => renameDevice(e.target.value)}
          />
        </Field>

        {auth?.stale && (
          <p className="text-[11px] text-ink-3">
            Working offline — {auth.message || "the server could not be reached"}. Accounts and
            plan shown from the last successful check.
          </p>
        )}

        <div className="rule-t flex gap-2 pt-4">
          <Button size="sm" onClick={syncNow} disabled={syncing}>
            {syncing ? "Syncing…" : "Sync accounts"}
          </Button>
          <Button size="sm" className="ml-auto" onClick={signOut}>
            Sign out
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

const fmtFlag = (v) => (v == null ? "—" : v ? "present" : "missing");
