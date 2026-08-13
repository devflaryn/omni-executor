/* Lua editor: a transparent textarea sitting exactly on top of a highlighted
   <pre>. Both share .editor-metrics so the caret lands on the glyphs. */

import { useEffect, useMemo, useRef, useState } from "react";
import { highlightLua, SAMPLE_SCRIPT } from "../lua.js";
import { api } from "../api.js";
import { useEngine } from "../engine.jsx";
import { Button, IconButton, Lamp } from "./ui.jsx";
import { CheckIcon, CopyIcon, EraserIcon, FileIcon, PlayIcon } from "./icons.jsx";

export default function EditorView({ active, showToast }) {
  const inputRef = useRef(null);
  const highlightRef = useRef(null);
  const gutterRef = useRef(null);
  const dirtyTimer = useRef(null);

  const [value, setValue] = useState(SAMPLE_SCRIPT);
  const [caret, setCaret] = useState({ line: 1, col: 1 });
  const [dirty, setDirty] = useState(false);
  const [running, setRunning] = useState(false);
  const [copied, setCopied] = useState(false);

  const { accounts, running: runningAccounts } = useEngine();
  const [target, setTarget] = useState("");
  const [result, setResult] = useState(null);

  // Default the target to a running account (else the first known account).
  useEffect(() => {
    if (target && accounts.some((a) => a.name === target)) return;
    const pick = runningAccounts[0] || accounts[0];
    if (pick) setTarget(pick.name);
  }, [accounts, runningAccounts, target]);

  const html = useMemo(() => highlightLua(value), [value]);
  const lineCount = useMemo(() => value.split("\n").length, [value]);

  useEffect(() => {
    inputRef.current?.setSelectionRange(0, 0);
  }, []);

  useEffect(() => {
    if (active) inputRef.current?.focus();
  }, [active]);

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
    setCaret({
      line: (upToCaret.match(/\n/g) || []).length + 1,
      col: input.selectionStart - upToCaret.lastIndexOf("\n"),
    });
  };

  const markDirty = () => {
    setDirty(true);
    clearTimeout(dirtyTimer.current);
    dirtyTimer.current = setTimeout(() => setDirty(false), 1200);
  };

  // execCommand keeps the native undo stack alive; fall back if unavailable.
  const insertText = (text) => {
    const input = inputRef.current;
    if (!document.execCommand("insertText", false, text)) {
      input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
      setValue(input.value);
    }
  };

  const run = async () => {
    if (running) return;
    if (!target) {
      showToast("No target account — add or select one in Accounts.", "error");
      return;
    }
    setRunning(true);
    setResult(null);
    const res = await api("execute_script", target, value);
    setRunning(false);
    if (!res.ok) {
      setResult({ tone: "error", text: res.message || res.error || "Execute failed" });
      showToast(res.message || "Execute failed", "error");
      return;
    }
    if (res.ran === false) {
      setResult({ tone: "error", text: res.output || "Script error" });
      showToast("Script error", "error");
    } else if (res.pending) {
      setResult({ tone: "warn", text: res.output || "Running…" });
      showToast("Sent — still running…", "success");
    } else {
      setResult({ tone: "ok", text: res.output || "ok" });
      showToast(`Executed on ${target} ✓`, "success");
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      run();
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

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      inputRef.current?.select();
      document.execCommand("copy");
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  const clear = () => {
    const input = inputRef.current;
    input.focus();
    input.select();
    insertText("\n"); // replace everything, undo-friendly
    if (input.value === "\n") {
      input.value = "";
      setValue("");
    }
    syncScroll();
  };

  return (
    <div className={`min-h-0 flex-1 flex-col p-5 ${active ? "flex" : "hidden"}`}>
      <div className="animate-rise flex min-h-0 flex-1 flex-col overflow-hidden">
        {/* Toolbar */}
        <div className="rule-b flex h-11 shrink-0 items-center gap-2 px-3.5">
          <FileIcon className="h-3.5 w-3.5 text-ink-3" />
          <span className="font-mono text-[12px] text-ink-2">untitled.lua</span>
          <span
            title="Unsaved edits"
            className={`h-1.5 w-1.5 rounded-full bg-accent transition-opacity duration-300 ${
              dirty ? "opacity-100" : "opacity-0"
            }`}
          />
          <div className="ml-auto flex items-center gap-1">
            <select
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              title="Target account — the running game to execute in"
              className="mr-1 max-w-[150px] rounded border border-white/10 bg-black/20 px-2 py-1
                         font-mono text-[11px] text-ink-2 outline-none"
            >
              {accounts.length === 0 && <option value="">no accounts</option>}
              {accounts.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                  {a.running ? " ●" : ""}
                </option>
              ))}
            </select>
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
              disabled={running}
              title="Run script (Ctrl+Enter)"
            >
              <PlayIcon className="h-3 w-3" />
              {running ? "Running…" : "Run"}
            </Button>
          </div>
        </div>

        {/* Code surface */}
        <div className="flex min-h-0 flex-1">
          <div
            ref={gutterRef}
            aria-hidden="true"
            className="editor-metrics w-[52px] shrink-0 overflow-hidden py-4
                       pr-3 text-right font-mono text-ink-3 select-none"
          >
            {Array.from({ length: lineCount }, (_, i) => (
              <div key={i} className={i + 1 === caret.line ? "text-accent" : "opacity-55"}>
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
              onChange={(e) => {
                setValue(e.target.value);
                markDirty();
              }}
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

        {/* Exec output */}
        {result && (
          <div
            className={`rule-t max-h-28 shrink-0 overflow-auto px-3.5 py-2 font-mono text-[11px] whitespace-pre-wrap ${
              result.tone === "error" ? "text-red-400" : result.tone === "warn" ? "text-amber-400" : "text-live"
            }`}
          >
            {result.text}
          </div>
        )}

        {/* Status bar */}
        <div className="rule-t flex h-8 shrink-0 items-center gap-4 px-3.5 font-mono text-[10.5px] text-ink-3">
          <span className="flex items-center gap-2">
            <Lamp tone={running ? "busy" : "live"} pulse={running} size={6} />
            {running ? "Running" : "Ready"}
          </span>
          <span>Lua</span>
          <span className="ml-auto">
            Ln {caret.line}, Col {caret.col}
          </span>
          <span>
            {lineCount} lines · {value.length} chars
          </span>
          <span className="hidden sm:inline">UTF-8</span>
        </div>
      </div>
    </div>
  );
}
