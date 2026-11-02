/* DEV ONLY — a pretend backend so the browser preview has
   something to show: a few accounts, two of them running, an engine that
   reports ready. Enabled by `?mock` on the Vite dev URL, never in a build
   (main.jsx guards on import.meta.env.DEV), and never when the real bridge
   exists. Launch/stop flip state after a short delay so bulk actions can be
   watched. */

export function installDevMock() {
  if (window.__omniMock) return;
  const settings = JSON.parse(localStorage.getItem("omni-settings") || "{}");
  let editorState = JSON.parse(localStorage.getItem("omni-editor") || "null");
  const accounts = [
    { name: "admn1b12farm2", running: true, state: "running", instance_id: 1 },
    { name: "admn1b12farm4", running: true, state: "installing", instance_id: 2 },
    { name: "farm_alpha", running: false, state: "stopped" },
    { name: "farm_beta", running: false, state: "stopped" },
    { name: "scout-01", running: false, state: "stopped" },
    // Enough rows to overflow the Accounts panel, so the preview exercises the
    // scrolling canvas rather than a list that happens to fit.
    ...Array.from({ length: 14 }, (_, i) => ({
      name: `farmhand-${String(i + 1).padStart(2, "0")}`,
      running: false,
      state: "stopped",
    })),
  ];
  const autoexec = [{ name: "00-antiafk.lua" }, { name: "10-collect.lua" }, { name: "20-rejoin.lua" }];
  // Drives the Stat Track preview's drifting numbers (see stattrack_stats).
  let statTick = 0;
  const find = (n) => accounts.find((a) => a.name === n);
  const later = (ms, fn) => new Promise((r) => setTimeout(() => r(fn()), ms));
  const push = (e, p) => window.omniEvent?.(e, p);
  // ?mock&dev previews a dev-tagged account; ?mock&newer a build ahead of its channel.
  const params = new URLSearchParams(location.search);
  const mockUpdate = () => {
    const allowed = params.has("dev") ? ["prod", "beta", "dev"] : ["prod", "beta"];
    const picked = settings.updateChannel || "prod";
    const active = allowed.includes(picked) ? picked : "prod";
    const newer = params.has("newer");
    return {
      ok: true,
      autoUpdate: settings.autoUpdate !== false,
      staged: null,
      channel: { picked, allowed, active },
      app: {
        current: newer ? "1.3.0" : "1.0.0",
        available: { prod: "1.0.0", beta: "1.0.0", dev: "1.0.0" }[active],
        update: false,
        downgrade: newer,
        canApply: true,
        reason: null,
      },
      runtime: { update: false, artifacts: [], bytes: 0, managed: true },
    };
  };

  window.__omniMock = {
    // The window itself is Tauri's now (frontend/src/window.js), and every
    // call there no-ops outside the shell — so the only window question left
    // for the backend is which chrome layout to draw.
    get_platform: async () => "win32",
    update_check: async () => mockUpdate(),
    update_set_channel: async (name) => {
      if (!mockUpdate().channel.allowed.includes(name)) {
        return { ok: false, error: "channel_forbidden", message: "This account can't use that channel." };
      }
      settings.updateChannel = name;
      localStorage.setItem("omni-settings", JSON.stringify(settings));
      return mockUpdate();
    },
    get_settings: async () => settings,
    save_settings: async (patch) => {
      Object.assign(settings, patch);
      localStorage.setItem("omni-settings", JSON.stringify(settings));
      return settings;
    },
    get_editor_state: async () => editorState,
    save_editor_state: async (s) => {
      editorState = s;
      localStorage.setItem("omni-editor", JSON.stringify(s));
      return { ok: true };
    },
    // PREMIUM in the preview. The mock exists so the browser has something
    // to show, and two of the sections (Farming, Stat Track) render only
    // their locked state without a plan — which is the one view that needs
    // no mock data at all. `?mock=free` still exercises the locked side.
    auth_status: async () => ({
      ok: true,
      signedIn: true,
      apiBase: "mock",
      device: { name: "dev" },
      subscription: new URLSearchParams(location.search).get("mock") === "free"
        ? { plan: null, tier: "free", active: false, credits: { credits: 0 } }
        : {
            plan: "lifetime",
            planLabel: "Lifetime",
            tier: "premium",
            active: true,
            daysRemaining: null,
            credits: { credits: 240 },
          },
    }),
    bootstrap_status: async () => ({ ready: true }),
    engine_version: async () => ({ ok: true, engine: "omnidroid", contract: "1.0", arch_aware: true, missing_commands: [], bundle: "mock" }),
    engine_doctor: async () => ({ ok: true, ready: true, apk: "OmniExecutor.apk", pool_up: true, pool_port: 7457 }),
    engine_list: async () => ({ ok: true, accounts: accounts.map((a) => ({ ...a })) }),
    cloud_presence: async () => ({ ok: true, presence: {} }),
    engine_start: (name) =>
      later(1800, () => {
        const a = find(name);
        if (a) Object.assign(a, { running: true, state: "running", instance_id: 9 });
        push("accounts-changed", {});
        return { ok: true };
      }),
    engine_stop: (name) =>
      later(900, () => {
        const a = find(name);
        if (a) Object.assign(a, { running: false, state: "stopped", instance_id: undefined });
        push("accounts-changed", {});
        return { ok: true };
      }),
    engine_view: async () => ({ ok: true }),
    engine_remove: async () => ({ ok: true }),
    list_autoexec: async () => ({ ok: true, scripts: autoexec.map((s) => ({ ...s })) }),
    open_autoexec_folder: async () => ({ ok: true }),
    read_autoexec: async (name) => {
      const s = autoexec.find((x) => x.name === name);
      if (!s) return { ok: false, error: "not_found", name };
      if (s.content === undefined) s.content = `-- ${name}\nprint("autoexec: ${name}")\n`;
      return { ok: true, name, content: s.content };
    },
    save_autoexec: async (name, content = "") => {
      const s = autoexec.find((x) => x.name === name);
      if (s) s.content = content;
      else autoexec.push({ name, content });
      autoexec.sort((a, b) => a.name.localeCompare(b.name));
      return { ok: true, name };
    },
    rename_autoexec: async (old, name) => {
      const s = autoexec.find((x) => x.name === old);
      if (!s) return { ok: false, error: "not_found", name: old };
      if (autoexec.some((x) => x.name === name)) return { ok: false, error: "exists", name };
      s.name = name;
      autoexec.sort((a, b) => a.name.localeCompare(b.name));
      return { ok: true, name };
    },
    // ---- stat track. The toggle writes a real file in the app; here it
    // flips a flag and adds/removes the mock autoexec entry, so the preview
    // shows the same cause and effect. Numbers drift upward on every poll so
    // the "still earning" reading is visibly live rather than frozen.
    stattrack_status: async () => ({
      ok: true,
      enabled: Boolean(settings.stattrack),
      installed: Boolean(settings.stattrack),
      stale: false,
      file: "C:\\Users\\dev\\AppData\\Roaming\\omni\\autoexec\\10_omni_stattrack.lua",
      dir: "C:\\Users\\dev\\AppData\\Roaming\\omni\\autoexec",
      base: "http://mock",
    }),
    stattrack_set: async (enabled) => {
      settings.stattrack = Boolean(enabled);
      localStorage.setItem("omni-settings", JSON.stringify(settings));
      const name = "10_omni_stattrack.lua";
      const at = autoexec.findIndex((s) => s.name === name);
      if (enabled && at < 0) autoexec.unshift({ name });
      if (!enabled && at >= 0) autoexec.splice(at, 1);
      return {
        ok: true,
        enabled: Boolean(enabled),
        installed: Boolean(enabled),
        stale: false,
        file: `C:\\Users\\dev\\AppData\\Roaming\\omni\\autoexec\\${name}`,
        message: enabled ? "Stat Track is on — it starts reporting on the next launch." : "Stat Track is off.",
      };
    },
    stattrack_stats: async () => {
      // A small counter, NOT the epoch: a tick derived from Date.now() is in
      // the hundred-millions, and the preview then shows "298001.46M gems",
      // which looks like a bug rather than a number a game would print.
      const tick = statTick++;
      const rows = accounts.map((a, i) => {
        const tracking = Boolean(settings.stattrack) && a.running;
        const gems = 1_200_000 + tick * 2500 + i * 90_000;
        return {
          username: a.name,
          userId: 1000000 + i,
          displayName: a.name,
          customName: null,
          presence: a.running
            ? { state: "running", label: "Running", isLocal: true }
            : { state: "stopped", label: "Stopped", isLocal: false },
          tracking,
          placeId: "8737899170",
          placeName: tracking ? "Pet Simulator 99" : null,
          jobId: tracking ? "3f1a55c0-2b19-4d0f-9c11-0d0a5a4b7e21" : null,
          metrics: tracking
            ? [
                { key: "gems", label: "Gems", value: gems, display: `${(gems / 1e6).toFixed(2)}M` },
                { key: "coins", label: "Coins", value: 34_000 + tick * 40, display: `${34_000 + tick * 40}` },
                { key: "level", label: "Level", value: 12 + i, display: `${12 + i}` },
              ]
            : [],
          uptimeSec: tracking ? 60 * (7 + i) : null,
          executor: tracking ? "Arceus X NEO" : null,
          reportedAt: tracking ? new Date().toISOString() : null,
          reportCount: tracking ? 40 + tick : 0,
        };
      });
      return {
        ok: true,
        accounts: rows,
        summary: {
          accounts: rows.length,
          online: rows.filter((r) => r.presence.state === "running").length,
          tracking: rows.filter((r) => r.tracking).length,
        },
      };
    },
    execute_script: (name) => later(600, () => ({ ok: true, output: `ran on ${name}` })),
    // ---- account creation (pretend batch: pushes the same events main.py does)
    creation_get_config: async () => ({
      ok: true,
      creation: { amount: 1, usernameStyle: "name_no", ...(settings.creation || {}) },
      // Read back what creation_save_config wrote, so the Network tab's
      // proxy row appears in the preview the moment one is typed.
      captcha: { proxy: "", ...(settings.captcha || {}) },
    }),
    creation_save_config: async (patch) => {
      Object.assign(settings, patch);
      localStorage.setItem("omni-settings", JSON.stringify(settings));
      return { ...settings, ok: true };
    },
    creation_status: async () => ({ ok: true, running: false, index: 0, total: 0 }),
    creation_stop: async () => ({ ok: true, stopping: true }),
    creation_start: (cfg) => {
      const total = Number(cfg?.amount) || 1;
      later(400, () =>
        Array.from({ length: total }, (_, i) => {
          const style = cfg?.usernameStyle || "name_no";
          const name =
            style === "stealth"
              ? Math.random().toString(36).slice(2, 14)
              : style === "adj_noun"
                ? `FrozenWolf${1000 + Math.floor(Math.random() * 9000)}`
                : style === "gamertag"
                  ? `EpicWizard${100 + Math.floor(Math.random() * 900)}`
                  : `Eric${100000 + Math.floor(Math.random() * 900000)}`;
          push("creation-progress", { index: i + 1, total, phase: "start", message: `Starting account ${i + 1} of ${total}` });
          setTimeout(() => {
            push("creation-account", { index: i + 1, total, ok: true, username: name, password: "mock-Passw0rd!" });
            push("accounts-changed", {});
            accounts.push({ name, base: "bliss-15", arch: "x86", running: false });
            if (i === total - 1) push("creation-done", { ok: true, created: total, failed: 0, results: [], message: "Done" });
          }, 1500 + i * 800);
        })
      );
      return Promise.resolve({ ok: true, started: true, total });
    },
    // ---- network (the Network tab). Jittered around plausible numbers, and
    // the proxy row only appears once one is configured, exactly as
    // netcheck.probe_all does.
    net_probe: () =>
      later(500, () => {
        const jitter = (mid) => Math.round(mid * (0.7 + Math.random() * 0.8) * 10) / 10;
        const grade = (ms) => (ms <= 700 ? "ok" : "slow");
        const targets = [
          {
            id: "roblox",
            label: "Roblox",
            url: "https://users.roblox.com/v1/users/1",
            note: "Logins and joins go through this host.",
            ms: jitter(420),
            httpStatus: 200,
            detail: "",
          },
          {
            id: "omni",
            label: "Omni server",
            url: "http://mock/omni/dist/health",
            note: "Sign-in, your account list and presence.",
            ms: jitter(140),
            httpStatus: 200,
            detail: "",
          },
        ].map((t) => ({ ...t, status: grade(t.ms) }));
        const proxy = String(settings.captcha?.proxy || "").trim();
        if (proxy) {
          const ms = jitter(1500);
          targets.push({
            id: "proxy",
            label: "Proxy",
            url: `http://${proxy.split(":").slice(0, 2).join(":")}`,
            note: "Reaching Roblox through the proxy.",
            ms,
            httpStatus: 200,
            detail: "",
            status: grade(ms),
          });
        }
        return { ok: true, checkedAt: Date.now(), okMs: 700, timeoutMs: 6000, targets };
      }),
    vault_list: async () => ({ ok: true, accounts: [] }),
    vault_reveal: async (username) => ({
      ok: true,
      account: { username, password: "mock-Passw0rd!", birthday: [1995, 4, 12] },
    }),
    // ---- earn / mining. Mirrors mining_enroll's real shape (installed flips
    // after "downloading", start/stop flip `running`) so the Earn tab has
    // something to react to in the browser preview.
    mining_status: async () => ({
      ok: true,
      installed: Boolean(settings.miningInstalled),
      enrolled: Boolean(settings.miningInstalled),
      running: Boolean(settings.miningRunning),
      hashrate: settings.miningRunning ? 1200 + Math.round(Math.random() * 300) : 0,
      estHashrate: { gpu: 1350000, cpu: 2400 },
      rates: { rvn: 0.000009, xmr: 0.0021 },
      coins: { rvn: true, xmr: true },
      creditedMicros: 124000,
    }),
    mining_enroll: () =>
      later(1200, () => {
        settings.miningInstalled = true;
        localStorage.setItem("omni-settings", JSON.stringify(settings));
        return { ok: true, minerToken: "mock", stratumHost: "localhost", stratumPort: 3333 };
      }),
    mining_start: (mode) => {
      settings.miningRunning = true;
      localStorage.setItem("omni-settings", JSON.stringify(settings));
      push("mining-stat", { line: `[mock] starting ${mode || "both"} miner…` });
      return Promise.resolve({ ok: true, started: true });
    },
    mining_stop: async () => {
      settings.miningRunning = false;
      localStorage.setItem("omni-settings", JSON.stringify(settings));
      return { ok: true, stopped: true };
    },
    mining_add_defender_exclusion: async () => ({ ok: true }),
    buy_day_with_credits: async () => ({ ok: true, subscription: { active: true }, credits: { credits: 0 } }),
  };
}
