/* Lua editor with tabs. Each tab is its own <CodeSurface> — a transparent
   textarea sitting exactly on a highlighted <pre> — mounted once and hidden
   when not active, so native undo, scroll and caret survive a tab switch.
   Tabs, text and the active tab persist across relaunches (editorStore). */

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { highlightLua } from "../lua.js";
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
        <TabBar
          tabs={tabs}
          activeId={activeId}
          onSelect={store.selectTab}
          onClose={store.closeTab}
          onNew={() => store.newTab()}
          onRename={store.renameTab}
        />

        {/* Toolbar. It used to restate the active tab's name under the tab
            bar, one line below the tab already showing it; the file icon moved
            onto the tab itself and the restatement went. */}
        <div className="rule-b flex h-11 shrink-0 items-center gap-2 px-3.5">
          <div className="ml-auto flex items-center gap-1">
            <select
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              title="Where to run — every running instance, or one of them"
              className="mr-1 max-w-[170px] cursor-pointer rounded-md border border-line bg-raised px-2 py-1
                         font-mono text-[11px] text-ink-2 outline-none focus:border-accent/60"
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
          <div className="rule-t max-h-32 shrink-0 overflow-auto px-3.5 py-2 font-mono text-[11px]">
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
        <div className="rule-t flex h-8 shrink-0 items-center gap-4 px-3.5 font-mono text-[10.5px] text-ink-3">
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
    <div className="flex h-9 shrink-0 items-end gap-0.5 overflow-x-auto px-1" role="tablist" ref={barRef}>
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
                        rounded-t-md px-2.5 pr-1.5 font-mono text-[11.5px] transition-colors duration-150
                        ${selected ? "bg-surface text-ink" : "text-ink-3 hover:bg-raised/60 hover:text-ink-2"}`}
            title={tab.name}
          >
            <FileIcon className={`h-3 w-3 shrink-0 ${selected ? "text-ink-2" : "text-ink-3"}`} />
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
                className="w-[140px] rounded border border-accent/50 bg-raised px-1 py-0.5 font-mono text-[11.5px]
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

/* Gutter + highlight layer + textarea, sharing .editor-metrics so the caret
   lands on the glyphs. Owns its own scroll and caret; reports the caret up. */
const CodeSurface = forwardRef(function CodeSurface({ value, visible, onChange, onCaret, onRun }, ref) {
  const inputRef = useRef(null);
  const highlightRef = useRef(null);
  const gutterRef = useRef(null);
  const [line, setLine] = useState(1);

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

  const onKeyDown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      onRun();
    } else if (e.key === "Tab") {
      e.preventDefault();
      insertText("    ");
    } else if (e.key === "Enter") {
      e.preventDefault();
      const input = inputRef.current;
      const before = input.value.slice(0, input.selectionStart);
      const currentLine = before.slice(before.lastIndexOf("\n") + 1);
      const indent = (currentLine.match(/^[ \t]*/) || [""])[0];
      const opensBlock = /\b(function|then|do|repeat|else)\s*$|{\s*$/.test(currentLine);
      insertText("\n" + indent + (opensBlock ? "    " : ""));
    }
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
          onKeyUp={updateCaret}
          onClick={updateCaret}
          className="editor-metrics absolute inset-0 resize-none overflow-auto bg-transparent py-4 pr-4
                     pl-4 font-mono whitespace-pre text-transparent caret-accent outline-none
                     select-text selection:bg-accent/25"
        />
      </div>
    </div>
  );
});
