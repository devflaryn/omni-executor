/* DEV ONLY — a pretend `window.pywebview.api` so the browser preview has
   something to show: a few accounts, two of them running, an engine that
   reports ready. Enabled by `?mock` on the Vite dev URL, never in a build
   (main.jsx guards on import.meta.env.DEV), and never when the real bridge
   exists. Launch/stop flip state after a short delay so bulk actions can be
   watched. */

export function installDevMock() {
  if (window.pywebview?.api) return;
  const settings = JSON.parse(localStorage.getItem("omni-settings") || "{}");
  let editorState = JSON.parse(localStorage.getItem("omni-editor") || "null");
  const accounts = [
    { name: "admn1b12farm2", base: "bliss-15", arch: "x86", running: true, mode: "farming", vnc_port: 5901, adb_port: 5555, has_vnc: true },
    { name: "admn1b12farm4", base: "bliss-15", arch: "x86", running: true, mode: "gaming", has_vnc: false, window_client: [1280, 720], adb_port: 5557, window_visible: true },
    { name: "farm_alpha", base: "bliss-15", arch: "arm", running: false },
    { name: "farm_beta", base: "bliss-15", arch: "x86", running: false },
    { name: "scout-01", base: "bliss-15", arch: "x86", running: false },
    // Enough rows to overflow the Accounts panel, so the preview exercises the
    // scrolling canvas rather than a list that happens to fit.
    ...Array.from({ length: 14 }, (_, i) => ({
      name: `farmhand-${String(i + 1).padStart(2, "0")}`,
      base: "bliss-15",
      arch: i % 3 === 0 ? "arm" : "x86",
      running: false,
    })),
  ];
  const autoexec = [{ name: "00-antiafk.lua" }, { name: "10-collect.lua" }, { name: "20-rejoin.lua" }];
  const find = (n) => accounts.find((a) => a.name === n);
  const later = (ms, fn) => new Promise((r) => setTimeout(() => r(fn()), ms));
  const push = (e, p) => window.omniEvent?.(e, p);

  window.pywebview = {
    api: {
      get_platform: async () => "win32",
      get_window_state: async () => ({ maximized: false }),
      begin_window_drag: async () => true,
      begin_window_resize: async () => true,
      titlebar_double_click: async () => true,
      toggle_maximize: async () => false,
      minimize: async () => {},
      close: async () => {},
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
      auth_status: async () => ({ ok: true, signedIn: true, apiBase: "mock", device: { name: "dev" } }),
      bootstrap_status: async () => ({ ready: true }),
      engine_version: async () => ({ ok: true, contract: "1.0", arch_aware: true, pool_supported: false, missing_commands: [] }),
      engine_modes: async () => ["gaming", "farming"],
      engine_doctor: async () => ({ ready: true, qemu_present: true, adb_present: true }),
      engine_list: async () => ({ ok: true, accounts: accounts.map((a) => ({ ...a })) }),
      cloud_presence: async () => ({ ok: true, presence: {} }),
      engine_bases: async () => ({ bases: [] }),
      engine_start: (name, mode) =>
        later(1800, () => {
          const a = find(name);
          if (a) Object.assign(a, { running: true, mode: mode || "gaming", vnc_port: 5910, adb_port: 5560, has_vnc: true });
          push("accounts-changed", {});
          return { ok: true };
        }),
      engine_stop: (name) =>
        later(900, () => {
          const a = find(name);
          if (a) Object.assign(a, { running: false, mode: undefined });
          push("accounts-changed", {});
          return { ok: true };
        }),
      engine_view: async () => ({ ok: true }),
      engine_hide: async () => ({ ok: true }),
      engine_remove: async () => ({ ok: true }),
      list_autoexec: async () => ({ ok: true, scripts: autoexec.map((s) => ({ ...s })) }),
      open_autoexec_folder: async () => ({ ok: true }),
      execute_script: (name) => later(600, () => ({ ok: true, output: `ran on ${name}` })),
      // ---- account creation (pretend batch: pushes the same events main.py does)
      creation_get_config: async () => ({
        ok: true,
        creation: { amount: 1, usernameStyle: "name_no" },
        captcha: { provider: "surfsky", apiKeys: { surfsky: "" } },
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
      vault_list: async () => ({ ok: true, accounts: [] }),
      vault_reveal: async (username) => ({
        ok: true,
        account: { username, password: "mock-Passw0rd!", birthday: [1995, 4, 12] },
      }),
    },
  };
  window.dispatchEvent(new Event("pywebviewready"));
}
