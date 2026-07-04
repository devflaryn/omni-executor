/* Omni Executor — editor logic, Lua highlighting, theme persistence. */
(() => {
  "use strict";

  // ---------------------------------------------------------------- elements
  const input = document.getElementById("input");
  const highlightCode = document.getElementById("highlight-code");
  const highlightPre = document.getElementById("highlight");
  const gutter = document.getElementById("gutter");
  const themeToggle = document.getElementById("theme-toggle");
  const themeKnob = document.getElementById("theme-knob");
  const iconSun = document.getElementById("icon-sun");
  const iconMoon = document.getElementById("icon-moon");
  const btnCopy = document.getElementById("btn-copy");
  const btnClear = document.getElementById("btn-clear");
  const dirtyDot = document.getElementById("dirty-dot");
  const statPos = document.getElementById("stat-pos");
  const statChars = document.getElementById("stat-chars");

  // ------------------------------------------------------- settings storage
  // Prefers the Python API (persists to a JSON file in the user's config
  // dir); falls back to localStorage when opened directly in a browser.
  const pywebviewReady = new Promise((resolve) => {
    if (window.pywebview) return resolve();
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
    setTimeout(resolve, 1500); // browser fallback: don't wait forever
  });

  async function loadSettings() {
    await pywebviewReady;
    try {
      if (window.pywebview?.api) return await window.pywebview.api.get_settings();
      return JSON.parse(localStorage.getItem("omni-settings")) || {};
    } catch {
      return {};
    }
  }

  function saveSettings(patch) {
    pywebviewReady.then(async () => {
      try {
        if (window.pywebview?.api) {
          await window.pywebview.api.save_settings(patch);
        } else {
          const cur = JSON.parse(localStorage.getItem("omni-settings")) || {};
          localStorage.setItem("omni-settings", JSON.stringify({ ...cur, ...patch }));
        }
      } catch (err) {
        console.error("Failed to save settings:", err);
      }
    });
  }

  // ------------------------------------------------------------------ theme
  function applyTheme(theme) {
    document.documentElement.classList.toggle("dark", theme === "dark");
    // The sun shows in light mode, the moon in dark mode.
    iconSun.classList.toggle("hidden", theme === "dark");
    iconMoon.classList.toggle("hidden", theme !== "dark");
    themeKnob.classList.remove("animate-pop");
    void themeKnob.offsetWidth; // restart the pop animation
    themeKnob.classList.add("animate-pop");
  }

  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme(next);
    saveSettings({ theme: next });
  });

  // ?theme=light|dark overrides saved settings (handy for testing in a browser).
  const forcedTheme = new URLSearchParams(location.search).get("theme");
  if (forcedTheme) {
    applyTheme(forcedTheme === "light" ? "light" : "dark");
  } else {
    loadSettings().then((s) => applyTheme(s.theme === "light" ? "light" : "dark"));
  }

  // ----------------------------------------------------- Lua tokenizer
  const LUA_KEYWORDS = new Set([
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function",
    "goto", "if", "in", "local", "nil", "not", "or", "repeat", "return",
    "then", "true", "until", "while",
  ]);

  const LUA_BUILTINS = new Set([
    "print", "pairs", "ipairs", "next", "type", "tostring", "tonumber",
    "require", "pcall", "xpcall", "error", "assert", "select", "unpack",
    "rawget", "rawset", "rawequal", "rawlen", "setmetatable", "getmetatable",
    "collectgarbage", "load", "loadstring", "dofile", "coroutine", "table",
    "string", "math", "os", "io", "utf8", "debug", "self", "_G", "_ENV",
  ]);

  const TOKEN_RE = new RegExp(
    [
      "(?<comment>--\\[(?<ceq>=*)\\[[\\s\\S]*?(?:\\]\\k<ceq>\\]|$)|--[^\\n]*)",
      "(?<string>\\[(?<seq>=*)\\[[\\s\\S]*?(?:\\]\\k<seq>\\]|$)|\"(?:\\\\.|[^\"\\\\\\n])*\"?|'(?:\\\\.|[^'\\\\\\n])*'?)",
      "(?<number>0[xX][0-9a-fA-F]+(?:\\.[0-9a-fA-F]*)?(?:[pP][+-]?\\d+)?|\\d+\\.?\\d*(?:[eE][+-]?\\d+)?|\\.\\d+(?:[eE][+-]?\\d+)?)",
      "(?<word>[A-Za-z_]\\w*)",
    ].join("|"),
    "g"
  );

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlightLua(source) {
    let html = "";
    let last = 0;
    TOKEN_RE.lastIndex = 0;
    for (const match of source.matchAll(TOKEN_RE)) {
      html += escapeHtml(source.slice(last, match.index));
      const g = match.groups;
      const text = escapeHtml(match[0]);
      if (g.comment !== undefined) html += `<span class="tok-com">${text}</span>`;
      else if (g.string !== undefined) html += `<span class="tok-str">${text}</span>`;
      else if (g.number !== undefined) html += `<span class="tok-num">${text}</span>`;
      else if (LUA_KEYWORDS.has(match[0])) html += `<span class="tok-kw">${text}</span>`;
      else if (LUA_BUILTINS.has(match[0])) html += `<span class="tok-fn">${text}</span>`;
      else html += text;
      last = match.index + match[0].length;
    }
    html += escapeHtml(source.slice(last));
    return html + "\n"; // trailing newline keeps pre/textarea heights in sync
  }

  // ------------------------------------------------------------- rendering
  let lineCount = 0;

  function render() {
    const value = input.value;
    highlightCode.innerHTML = highlightLua(value);

    const lines = value.split("\n").length;
    if (lines !== lineCount) {
      lineCount = lines;
      let nums = "";
      for (let i = 1; i <= lines; i++) nums += i + "\n";
      gutter.textContent = nums;
    }

    statChars.textContent = `${value.length} chars`;
    updateCaret();
  }

  function updateCaret() {
    const upToCaret = input.value.slice(0, input.selectionStart);
    const line = (upToCaret.match(/\n/g) || []).length + 1;
    const col = input.selectionStart - upToCaret.lastIndexOf("\n");
    statPos.textContent = `Ln ${line}, Col ${col}`;
  }

  function syncScroll() {
    highlightPre.scrollTop = input.scrollTop;
    highlightPre.scrollLeft = input.scrollLeft;
    gutter.scrollTop = input.scrollTop;
  }

  // --------------------------------------------------------------- editing
  function insertText(text) {
    // execCommand keeps the native undo stack; fall back if unavailable.
    if (!document.execCommand("insertText", false, text)) {
      input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
      input.dispatchEvent(new Event("input"));
    }
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      insertText("    ");
    } else if (e.key === "Enter") {
      e.preventDefault();
      const before = input.value.slice(0, input.selectionStart);
      const currentLine = before.slice(before.lastIndexOf("\n") + 1);
      const indent = (currentLine.match(/^[ \t]*/) || [""])[0];
      // Indent one level further after block openers.
      const opensBlock = /\b(function|then|do|repeat|else)\s*$|{\s*$/.test(currentLine);
      insertText("\n" + indent + (opensBlock ? "    " : ""));
    }
  });

  let dirtyTimer;
  input.addEventListener("input", () => {
    render();
    syncScroll();
    dirtyDot.classList.remove("opacity-0");
    clearTimeout(dirtyTimer);
    dirtyTimer = setTimeout(() => dirtyDot.classList.add("opacity-0"), 1200);
  });
  input.addEventListener("scroll", syncScroll);
  document.addEventListener("selectionchange", () => {
    if (document.activeElement === input) updateCaret();
  });

  // --------------------------------------------------------------- toolbar
  btnCopy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
    } catch {
      input.select();
      document.execCommand("copy");
    }
    const old = btnCopy.textContent;
    btnCopy.textContent = "Copied!";
    setTimeout(() => (btnCopy.textContent = old), 1000);
  });

  btnClear.addEventListener("click", () => {
    input.focus();
    input.select();
    insertText("\n"); // replace everything, undo-friendly
    input.value = input.value === "\n" ? "" : input.value;
    render();
    syncScroll();
  });

  // ------------------------------------------------------------------ init
  input.value = [
    '-- Omni Executor · sample script',
    'local Greeter = {}',
    'Greeter.__index = Greeter',
    '',
    'function Greeter.new(name)',
    '    local self = setmetatable({}, Greeter)',
    '    self.name = name or "world"',
    '    return self',
    'end',
    '',
    'function Greeter:say()',
    '    print(("Hello, %s!"):format(self.name))',
    'end',
    '',
    '--[[ Memoized Fibonacci ]]',
    'local memo = {}',
    'local function fib(n)',
    '    if n < 2 then return n end',
    '    if not memo[n] then',
    '        memo[n] = fib(n - 1) + fib(n - 2)',
    '    end',
    '    return memo[n]',
    'end',
    '',
    'for i = 1, 10 do',
    '    io.write(fib(i), " ")',
    'end',
    '',
    'Greeter.new("Omni"):say()',
    '',
  ].join("\n");

  render();
  input.focus();
  input.setSelectionRange(0, 0); // start at the top of the file
  input.scrollTop = 0;
  syncScroll();
  updateCaret();
})();
