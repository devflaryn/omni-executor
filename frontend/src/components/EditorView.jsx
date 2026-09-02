/* Lua editor with tabs. Each tab is its own <CodeSurface> — a transparent
   textarea sitting exactly on a highlighted <pre> — mounted once and hidden
   when not active, so native undo, scroll and caret survive a tab switch.
   Tabs, text and the active tab persist across relaunches (editorStore). */

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { highlightLua, LUA_KEYWORDS, LUA_BUILTINS, LUA_MEMBERS } from "../lua.js";
import { api } from "../api.js";
import { useEngine } from "../engine.jsx";
import { useEditorStore } from "../editorStore.jsx";
import { Button, IconButton, Lamp } from "./ui.jsx";
import { CheckIcon, CloseIcon, CopyIcon, EraserIcon, FileIcon, PlayIcon, PlusIcon, RocketIcon } from "./icons.jsx";

const ALL = "__all__";

export default function EditorView({ active, showToast }) {
  const store = useEditorStore();
  const { tabs, activeId, activeTab } = store;
  const { running: runningAccounts } = useEngine();

  const surfaces = useRef(new Map()); // tab id -> CodeSurface handle
  const [caret, setCaret] = useState({ line: 1, col: 1 });
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState(false);
  const [results, setResults] = useState(null);

  // Where to run: every running instance by default, or one of them. Stopped
  // accounts are not offered — a script cannot run where no game is.
  const [target, setTarget] = useState(ALL);
  useEffect(() => {
    if (target !== ALL && !runningAccounts.some((a) => a.name === target)) setTarget(ALL);
  }, [runningAccounts, target]);

  useEffect(() => {
    if (active && activeId) surfaces.current.get(activeId)?.focus();
  }, [active, activeId]);

  // Ctrl+N new tab · Ctrl+W close · Ctrl+Tab / Ctrl+Shift+Tab cycle.
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === "n") {
        e.preventDefault();
        store.newTab();
      } else if (key === "w") {
        e.preventDefault();
        if (activeId) store.closeTab(activeId);
      } else if (key === "tab") {
        e.preventDefault();
        const index = tabs.findIndex((t) => t.id === activeId);
        const next = tabs[(index + (e.shiftKey ? -1 : 1) + tabs.length) % tabs.length];
        if (next) store.selectTab(next.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, activeId, tabs, store]);

  const runOn = async (name, script) => {
    const res = await api("execute_script", name, script);
    if (!res.ok) return { name, tone: "error", text: res.message || res.error || "Execute failed" };
    if (res.ran === false) return { name, tone: "error", text: res.output || "Script error" };
    if (res.pending) return { name, tone: "warn", text: res.output || "Running…" };
    return { name, tone: "ok", text: res.output || "ok" };
  };

  const run = async () => {
    if (running || !activeTab) return;
    const names = target === ALL ? runningAccounts.map((a) => a.name) : [target];
    if (!names.length) {
      showToast("Nothing is running — launch an instance first.", "error");
      return;
    }
    setRunning(true);
    setResults(null);
    const out = await Promise.all(names.map((n) => runOn(n, activeTab.content)));
    setRunning(false);
    setResults(out);
    const failed = out.filter((r) => r.tone === "error").length;
    if (failed === 0) {
      showToast(names.length === 1 ? `Executed on ${names[0]} ✓` : `Executed on ${names.length} instances ✓`, "success");
    } else {
      showToast(failed === out.length ? "Execute failed" : `Failed on ${failed} of ${out.length}`, "error");
    }
  };

  const copy = async () => {
    if (!activeTab) return;
    try {
      await navigator.clipboard.writeText(activeTab.content);
    } catch {
      surfaces.current.get(activeId)?.copyFallback();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  const clear = () => surfaces.current.get(activeId)?.clear();

  const lineCount = useMemo(() => (activeTab ? activeTab.content.split("\n").length : 0), [activeTab]);

  return (
    <div className={`min-h-0 flex-1 flex-col p-5 pt-2 ${active ? "flex" : "hidden"}`}>
      <div className="animate-rise flex min-h-0 flex-1 flex-col overflow-hidden">
        {/* Header — ONE row: tabs on the left, the run tools on the right.
            They used to be two stacked bars (tabs above, tools below) that
            never lined up; now both sit on the same rule, tabs bottom-aligned
            so the active underline touches it, tools vertically centered. */}
        <div className="rule-b flex h-11 shrink-0 items-end gap-2 pr-3.5">
          <TabBar
            tabs={tabs}
            activeId={activeId}
            onSelect={store.selectTab}
            onClose={store.closeTab}
            onNew={() => store.newTab()}
            onRename={store.renameTab}
          />
          <div className="flex shrink-0 items-center gap-1 self-center">
            <select
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              title="Where to run — every running instance, or one of them"
              className="mr-1 max-w-[170px] cursor-pointer rounded-md border border-line bg-raised px-2 py-1
                         font-mono text-[12px] text-ink-2 outline-none focus:border-accent/60"
            >
              <option value={ALL}>
                {runningAccounts.length ? `All (${runningAccounts.length} running)` : "None"}
              </option>
              {runningAccounts.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
            <IconButton
              label="Autoexec folder — every script here auto-runs in every instance at start"
              onClick={async () => {
                const res = await api("open_autoexec_folder");
                showToast?.(
                  res?.ok
                    ? "Opened autoexec folder — drop .lua files here to auto-run them at start"
                    : `Could not open autoexec folder: ${res?.message || res?.error || "unknown"}`,
                  res?.ok ? "ok" : "err",
                );
              }}
            >
              <RocketIcon className="h-4 w-4" />
            </IconButton>
            <IconButton label={copied ? "Copied" : "Copy script"} onClick={copy}>
              {copied ? <CheckIcon className="h-4 w-4 text-live" /> : <CopyIcon className="h-4 w-4" />}
            </IconButton>
            <IconButton label="Clear editor" tone="danger" onClick={clear}>
              <EraserIcon className="h-4 w-4" />
            </IconButton>
            <Button
              variant="solid"
              size="sm"
              className="ml-1"
              onClick={run}
              disabled={running || !activeTab}
              title="Run script (Ctrl+Enter)"
            >
              <PlayIcon className="h-3 w-3" />
              {running ? "Running…" : target === ALL && runningAccounts.length > 1 ? `Run on all` : "Run"}
            </Button>
          </div>
        </div>

        {/* Code surfaces — one per tab, only the active one visible */}
        <div className="relative flex min-h-0 flex-1">
          {tabs.map((tab) => (
            <CodeSurface
              key={tab.id}
              ref={(handle) => {
                if (handle) surfaces.current.set(tab.id, handle);
                else surfaces.current.delete(tab.id);
              }}
              value={tab.content}
              visible={tab.id === activeId}
              onChange={(text) => store.updateContent(tab.id, text)}
              onCaret={tab.id === activeId ? setCaret : undefined}
              onRun={run}
            />
          ))}
        </div>

        {/* Exec output — one line per instance it ran on */}
        {results && (
          <div className="rule-t max-h-32 shrink-0 overflow-auto px-3.5 py-2 font-mono text-[12px]">
            {results.map((r) => (
              <div
                key={r.name}
                className={`flex gap-3 whitespace-pre-wrap ${
                  r.tone === "error" ? "text-danger" : r.tone === "warn" ? "text-amber-400" : "text-live"
                }`}
              >
                {results.length > 1 && <span className="shrink-0 text-ink-3">{r.name}</span>}
                <span className="min-w-0 flex-1">{r.text}</span>
              </div>
            ))}
          </div>
        )}

        {/* Status bar */}
        <div className="rule-t flex h-8 shrink-0 items-center gap-4 px-3.5 font-mono text-[11.5px] text-ink-3">
          <span className="flex items-center gap-2">
            <Lamp tone={running ? "busy" : "live"} pulse={running} size={6} />
            {running ? "Running" : "Ready"}
          </span>
          <span>Lua</span>
          <span>
            {tabs.length} {tabs.length === 1 ? "tab" : "tabs"}
          </span>
          <span className="ml-auto">
            Ln {caret.line}, Col {caret.col}
          </span>
          <span>
            {lineCount} lines · {activeTab?.content.length ?? 0} chars
          </span>
          <span className="hidden sm:inline">UTF-8</span>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function TabBar({ tabs, activeId, onSelect, onClose, onNew, onRename }) {
  const [editing, setEditing] = useState(null); // { id, value }
  const barRef = useRef(null);

  // Keep the active tab in view when there are many.
  useEffect(() => {
    barRef.current?.querySelector("[aria-selected='true']")?.scrollIntoView({ inline: "nearest", block: "nearest" });
  }, [activeId]);

  const commitRename = () => {
    if (editing) onRename(editing.id, editing.value);
    setEditing(null);
  };

  return (
    <div className="flex min-w-0 flex-1 items-end gap-0.5 self-stretch overflow-x-auto px-1" role="tablist" ref={barRef}>
      {tabs.map((tab) => {
        const selected = tab.id === activeId;
        const isEditing = editing?.id === tab.id;
        return (
          <div
            key={tab.id}
            role="tab"
            aria-selected={selected}
            tabIndex={0}
            onClick={() => onSelect(tab.id)}
            onDoubleClick={() => setEditing({ id: tab.id, value: tab.name })}
            onAuxClick={(e) => e.button === 1 && onClose(tab.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(tab.id);
              } else if (e.key === "F2") {
                setEditing({ id: tab.id, value: tab.name });
              }
            }}
            className={`ring-focus group relative flex h-8 max-w-[200px] min-w-0 cursor-pointer items-center gap-1.5
                        rounded-t-md px-2.5 pr-1.5 font-mono text-[12.5px] transition-colors duration-150
                        ${selected ? "bg-surface text-ink" : "text-ink-3 hover:bg-raised/60 hover:text-ink-2"}`}
            title={tab.kind === "autoexec" ? `${tab.name} — autoexec, runs in every instance at start` : tab.name}
          >
            {/* The rocket marks a tab that IS an autoexec file — edits write
                through to the folder, not just to the editor's state. */}
            {tab.kind === "autoexec" ? (
              <RocketIcon className={`h-3 w-3 shrink-0 ${selected ? "text-ink-2" : "text-ink-3"}`} />
            ) : (
              <FileIcon className={`h-3 w-3 shrink-0 ${selected ? "text-ink-2" : "text-ink-3"}`} />
            )}
            {isEditing ? (
              <input
                autoFocus
                value={editing.value}
                onChange={(e) => setEditing({ id: tab.id, value: e.target.value })}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename();
                  else if (e.key === "Escape") setEditing(null);
                  e.stopPropagation();
                }}
                onClick={(e) => e.stopPropagation()}
                className="w-[140px] rounded border border-accent/50 bg-raised px-1 py-0.5 font-mono text-[12.5px]
                           text-ink outline-none select-text"
                spellCheck={false}
              />
            ) : (
              <span className="truncate">{tab.name}</span>
            )}
            <button
              type="button"
              aria-label={`Close ${tab.name}`}
              title="Close tab"
              onClick={(e) => {
                e.stopPropagation();
                onClose(tab.id);
              }}
              className={`ring-focus flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded
                          text-ink-3 transition-opacity hover:bg-raised hover:text-ink
                          ${selected ? "opacity-70" : "opacity-0 group-hover:opacity-70"}`}
            >
              <CloseIcon className="h-3 w-3" />
            </button>
            {selected && <span className="absolute inset-x-0 bottom-0 h-[2px] rounded-full bg-accent" />}
          </div>
        );
      })}
      <IconButton label="New script (Ctrl+N)" onClick={onNew} className="mb-0.5 ml-0.5">
        <PlusIcon className="h-3.5 w-3.5" />
      </IconButton>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/* --- Editing smarts ------------------------------------------------------ */

/* Auto-close pairs. Quotes are here too — they double as their own closer,
   which is what makes "step over" work for them. */
const PAIR = { "(": ")", "[": "]", "{": "}", '"': '"', "'": "'" };
const CLOSERS = new Set([")", "]", "}"]);
const WORD_CH = /[A-Za-z0-9_]/;

/* Look of a completion row's kind tag — the little letter VS Code draws as an
   icon. Reuses the syntax palette so a keyword completes in keyword purple. */
const KIND_STYLE = {
  kw: { tag: "k", cls: "text-code-kw" },
  fn: { tag: "f", cls: "text-code-fn" },
  prop: { tag: "m", cls: "text-code-prop" },
  var: { tag: "v", cls: "text-ink-3" },
};

/* Completion candidates for the word `prefix` ending at `caret`. After a `.`
   or `:` the menu is members — the curated LUA_MEMBERS list merged with every
   `base.x` the buffer already contains, so anything used once completes
   everywhere. Otherwise: keywords + builtins + the buffer's own identifiers.
   `force` (Ctrl+Space) opens even on an empty word. */
function completionsFor(source, caret, prefix, force = false) {
  const before = source.slice(0, caret - prefix.length);
  const member = before.match(/([A-Za-z_]\w*)\s*[.:]$/);
  const seen = new Set();
  const items = [];
  const push = (label, kind) => {
    if (label === prefix || seen.has(label)) return;
    if (prefix && !label.toLowerCase().startsWith(prefix.toLowerCase())) return;
    seen.add(label);
    items.push({ label, kind });
  };
  if (member) {
    for (const m of LUA_MEMBERS[member[1]] || []) push(m, "prop");
    const used = new RegExp(`\\b${member[1]}\\s*[.:]([A-Za-z_]\\w*)`, "g");
    for (const m of source.matchAll(used)) push(m[1], "prop");
  } else {
    if (!prefix && !force) return [];
    for (const k of LUA_KEYWORDS) push(k, "kw");
    for (const b of LUA_BUILTINS) push(b, "fn");
    for (const m of source.matchAll(/[A-Za-z_]\w{2,}/g)) push(m[0], "var");
  }
  // Shortest first — the tightest continuation of what was typed sits on top.
  items.sort((a, b) => a.label.length - b.label.length || a.label.localeCompare(b.label));
  return items.slice(0, 8);
}

/* Gutter + highlight layer + textarea, sharing .editor-metrics so the caret
   lands on the glyphs. Owns its own scroll and caret; reports the caret up.
   Carries the VS-Code-ish smarts: auto-closing pairs (wrap the selection,
   step over the closer, Backspace eats both halves) and a completion menu
   (letters open it, Ctrl+Space forces it, arrows + Enter/Tab drive it). */
const CodeSurface = forwardRef(function CodeSurface({ value, visible, onChange, onCaret, onRun }, ref) {
  const inputRef = useRef(null);
  const highlightRef = useRef(null);
  const gutterRef = useRef(null);
  const [line, setLine] = useState(1);
  const [menu, setMenu] = useState(null); // { items, index, prefix, left, top }
  const menuKeyRef = useRef(false); // keyup after a menu-nav keydown must not refilter
  const charWRef = useRef(0);

  // One glyph's width — the surface is strictly monospace, so caret pixel
  // positions are arithmetic, not DOM measurement.
  const charWidth = () => {
    if (!charWRef.current) {
      const s = getComputedStyle(inputRef.current);
      const ctx = document.createElement("canvas").getContext("2d");
      ctx.font = `${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
      charWRef.current = ctx.measureText("M").width || 8;
    }
    return charWRef.current;
  };

  const html = useMemo(() => highlightLua(value), [value]);
  const lineCount = useMemo(() => value.split("\n").length, [value]);

  const syncScroll = () => {
    const input = inputRef.current;
    if (!input) return;
    if (highlightRef.current) {
      highlightRef.current.scrollTop = input.scrollTop;
      highlightRef.current.scrollLeft = input.scrollLeft;
    }
    if (gutterRef.current) gutterRef.current.scrollTop = input.scrollTop;
  };

  const updateCaret = () => {
    const input = inputRef.current;
    if (!input) return;
    const upToCaret = input.value.slice(0, input.selectionStart);
    const ln = (upToCaret.match(/\n/g) || []).length + 1;
    setLine(ln);
    onCaret?.({ line: ln, col: input.selectionStart - upToCaret.lastIndexOf("\n") });
  };

  // Report the caret when this surface becomes the active one.
  useEffect(() => {
    if (visible) updateCaret();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  // execCommand keeps the native undo stack alive; fall back if unavailable.
  const insertText = (text) => {
    const input = inputRef.current;
    if (!document.execCommand("insertText", false, text)) {
      input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
      onChange(input.value);
    }
  };

  useImperativeHandle(ref, () => ({
    focus: () => inputRef.current?.focus(),
    copyFallback: () => {
      inputRef.current?.select();
      document.execCommand("copy");
    },
    clear: () => {
      const input = inputRef.current;
      if (!input) return;
      input.focus();
      input.select();
      insertText("\n"); // replace everything, undo-friendly
      if (input.value === "\n") {
        input.value = "";
        onChange("");
      }
      syncScroll();
    },
  }));

  /* Build (or close) the completion menu for the word at the caret, anchored
     under that word's first character. */
  const openMenu = (force = false) => {
    const input = inputRef.current;
    if (!input || input.selectionStart !== input.selectionEnd) return setMenu(null);
    const caret = input.selectionStart;
    const text = input.value;
    const prefix = (text.slice(0, caret).match(/[A-Za-z_]\w*$/) || [""])[0];
    const items = completionsFor(text, caret, prefix, force);
    if (!items.length) return setMenu(null);
    const upTo = text.slice(0, caret - prefix.length);
    const line0 = (upTo.match(/\n/g) || []).length;
    const lineText = upTo.slice(upTo.lastIndexOf("\n") + 1);
    let col = 0; // visual column — tabs render 4 wide (tab-size)
    for (const ch of lineText) col += ch === "\t" ? 4 - (col % 4) : 1;
    const PAD = 16, LINE_H = 22, WIDTH = 230;
    const box = input.parentElement; // the relative wrapper
    const height = items.length * 25 + 8;
    const left = Math.max(4, Math.min(PAD + col * charWidth() - input.scrollLeft, box.clientWidth - WIDTH - 4));
    let top = PAD + (line0 + 1) * LINE_H - input.scrollTop + 2;
    if (top + height > box.clientHeight - 4) top = PAD + line0 * LINE_H - input.scrollTop - height - 2;
    setMenu({ items, index: 0, prefix, left, top });
  };

  const accept = (item) => {
    insertText(item.label.slice(menu.prefix.length));
    setMenu(null);
    updateCaret();
  };

  const onKeyDown = (e) => {
    const input = inputRef.current;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      setMenu(null);
      onRun();
      return;
    }
    if (e.ctrlKey && e.code === "Space") {
      e.preventDefault();
      openMenu(true);
      return;
    }
    if (menu) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        menuKeyRef.current = true;
        const step = e.key === "ArrowDown" ? 1 : -1;
        setMenu((m) => m && { ...m, index: (m.index + step + m.items.length) % m.items.length });
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        menuKeyRef.current = true;
        accept(menu.items[menu.index]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        menuKeyRef.current = true;
        setMenu(null);
        return;
      }
    }
    const s = input.selectionStart;
    const t = input.selectionEnd;
    const prev = input.value[s - 1] || "";
    const next = input.value[t] || "";
    // Step over the closer (or closing quote) auto-close already placed.
    if (s === t && next === e.key && (CLOSERS.has(e.key) || PAIR[e.key] === e.key)) {
      e.preventDefault();
      input.setSelectionRange(s + 1, s + 1);
      updateCaret();
      return;
    }
    if (PAIR[e.key]) {
      const isQuote = PAIR[e.key] === e.key;
      if (s !== t) {
        // Wrap the selection instead of overtyping it.
        e.preventDefault();
        const inner = input.value.slice(s, t);
        insertText(e.key + inner + PAIR[e.key]);
        input.setSelectionRange(s + 1, s + 1 + inner.length);
        return;
      }
      // A quote against a word (it's, don't) stays a lone quote.
      if (isQuote && (WORD_CH.test(prev) || WORD_CH.test(next))) return;
      e.preventDefault();
      insertText(e.key + PAIR[e.key]);
      input.setSelectionRange(s + 1, s + 1);
      return;
    }
    // Deleting an opener takes its untouched closer with it.
    if (e.key === "Backspace" && s === t && s > 0 && PAIR[prev] === next && next !== "") {
      e.preventDefault();
      input.setSelectionRange(s - 1, s + 1);
      if (!document.execCommand("delete")) {
        input.setRangeText("", s - 1, s + 1, "end");
        onChange(input.value);
      }
      updateCaret();
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      insertText("    ");
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const before = input.value.slice(0, s);
      const currentLine = before.slice(before.lastIndexOf("\n") + 1);
      const indent = (currentLine.match(/^[ \t]*/) || [""])[0];
      // Enter inside a fresh pair puts the closer on its own line, the
      // caret indented on the line between.
      if ((prev === "{" && next === "}") || (prev === "(" && next === ")")) {
        insertText("\n" + indent + "    \n" + indent);
        const mid = s + 1 + indent.length + 4;
        input.setSelectionRange(mid, mid);
        updateCaret();
        return;
      }
      const opensBlock = /\b(function|then|do|repeat|else)\s*$|[{(]\s*$/.test(currentLine);
      insertText("\n" + indent + (opensBlock ? "    " : ""));
    }
  };

  /* The menu lives off keyUP: by then the character is in the buffer, so the
     word under the caret is current. Nav keys were consumed by keydown and
     must not refilter (menuKeyRef); chorded keys are shortcuts, not typing. */
  const onKeyUp = (e) => {
    updateCaret();
    if (menuKeyRef.current) {
      menuKeyRef.current = false;
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key.length === 1 || e.key === "Backspace") openMenu();
    else if (menu) setMenu(null);
  };

  return (
    <div className={`absolute inset-0 ${visible ? "flex" : "hidden"}`}>
      <div
        ref={gutterRef}
        aria-hidden="true"
        className="editor-metrics w-[52px] shrink-0 overflow-hidden py-4
                   pr-3 text-right font-mono text-ink-3 select-none"
      >
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i} className={i + 1 === line ? "text-accent" : "opacity-55"}>
            {i + 1}
          </div>
        ))}
      </div>

      <div className="relative min-w-0 flex-1">
        <pre
          ref={highlightRef}
          aria-hidden="true"
          className="editor-metrics pointer-events-none absolute inset-0 m-0 overflow-hidden py-4 pr-4
                     pl-4 font-mono whitespace-pre text-ink"
        >
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>

        <textarea
          ref={inputRef}
          aria-label="Lua script"
          spellCheck={false}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          onScroll={syncScroll}
          onSelect={updateCaret}
          onKeyUp={onKeyUp}
          onClick={() => {
            updateCaret();
            setMenu(null);
          }}
          onBlur={() => setMenu(null)}
          onWheel={() => menu && setMenu(null)}
          className="editor-metrics absolute inset-0 resize-none overflow-auto bg-transparent py-4 pr-4
                     pl-4 font-mono whitespace-pre text-transparent caret-accent outline-none
                     select-text selection:bg-accent/25"
        />

        {/* Completion menu, anchored under the word it completes. mousedown
            (not click) accepts, so the textarea never loses focus. */}
        {menu && (
          <div
            role="listbox"
            aria-label="Completions"
            className="absolute z-20 w-[230px] overflow-hidden rounded-lg border border-line bg-raised py-1
                       shadow-xl shadow-black/40"
            style={{ left: menu.left, top: menu.top }}
          >
            {menu.items.map((item, i) => (
              <button
                key={item.label}
                type="button"
                role="option"
                aria-selected={i === menu.index}
                onMouseDown={(e) => {
                  e.preventDefault();
                  accept(item);
                }}
                onMouseEnter={() => setMenu((m) => m && { ...m, index: i })}
                className={`flex h-[25px] w-full items-center gap-2 px-2.5 text-left font-mono text-[12.5px]
                            ${i === menu.index ? "bg-accent/20 text-ink" : "text-ink-2"}`}
              >
                <span className={`w-3 shrink-0 text-center text-[10px] font-bold ${KIND_STYLE[item.kind].cls}`}>
                  {KIND_STYLE[item.kind].tag}
                </span>
                <span className="truncate">
                  <span className="font-semibold text-ink">{item.label.slice(0, menu.prefix.length)}</span>
                  {item.label.slice(menu.prefix.length)}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});
