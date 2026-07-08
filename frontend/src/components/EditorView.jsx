import { useEffect, useMemo, useRef, useState } from "react";
import { highlightLua, SAMPLE_SCRIPT } from "../lua.js";
import { FileIcon, RocketIcon } from "./icons.jsx";

export default function EditorView({ active, showToast }) {
  const inputRef = useRef(null);
  const highlightRef = useRef(null);
  const gutterRef = useRef(null);
  const dirtyTimer = useRef(null);

  const [value, setValue] = useState(SAMPLE_SCRIPT);
  const [caret, setCaret] = useState({ line: 1, col: 1 });
  const [dirty, setDirty] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [copied, setCopied] = useState(false);

  const html = useMemo(() => highlightLua(value), [value]);
  const gutter = useMemo(() => {
    const lines = value.split("\n").length;
    let nums = "";
    for (let i = 1; i <= lines; i++) nums += i + "\n";
    return nums;
  }, [value]);

  // Start with the caret at the top of the sample file.
  useEffect(() => {
    const input = inputRef.current;
    if (input) input.setSelectionRange(0, 0);
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
    const line = (upToCaret.match(/\n/g) || []).length + 1;
    const col = input.selectionStart - upToCaret.lastIndexOf("\n");
    setCaret({ line, col });
  };

  const markDirty = () => {
    setDirty(true);
    clearTimeout(dirtyTimer.current);
    dirtyTimer.current = setTimeout(() => setDirty(false), 1200);
  };

  // execCommand keeps the native undo stack; fall back if unavailable.
  const insertText = (text) => {
    const input = inputRef.current;
    if (!document.execCommand("insertText", false, text)) {
      input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
      setValue(input.value);
    }
  };

  const launch = () => {
    if (launching) return;
    setLaunching(true);
    // No execution backend is wired up yet — this is where it would plug in.
    setTimeout(() => {
      setLaunching(false);
      showToast("Script launched");
    }, 900);
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      launch();
    } else if (e.key === "Tab") {
      e.preventDefault();
      insertText("    ");
    } else if (e.key === "Enter") {
      e.preventDefault();
      const input = inputRef.current;
      const before = input.value.slice(0, input.selectionStart);
      const currentLine = before.slice(before.lastIndexOf("\n") + 1);
      const indent = (currentLine.match(/^[ \t]*/) || [""])[0];
      // Indent one level further after block openers.
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
    setTimeout(() => setCopied(false), 1000);
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
    <section className={`min-h-0 flex-1 flex-col ${active ? "flex" : "hidden"}`}>
      <main className="card animate-rise flex min-h-0 flex-1 flex-col overflow-hidden">
        {/* Toolbar */}
        <div
          className="flex items-center justify-between border-b border-slate-200 px-4 py-2.5
                     transition-colors duration-300 dark:border-white/[.06]"
        >
          <div className="flex items-center gap-2 text-[12.5px] text-slate-500 dark:text-slate-400">
            <FileIcon className="h-3.5 w-3.5 opacity-70" />
            <span className="font-medium">untitled.lua</span>
            <span
              className={`h-1.5 w-1.5 rounded-full bg-amber-400 transition-opacity duration-300 ${dirty ? "opacity-100" : "opacity-0"}`}
            />
          </div>
          <div className="flex items-center gap-1.5">
            <button onClick={copy} className="btn-ghost">
              {copied ? "Copied!" : "Copy"}
            </button>
            <button
              onClick={clear}
              className="rounded-lg px-2.5 py-1 text-[12px] font-medium text-slate-500 transition-all
                         duration-150 outline-none hover:bg-red-50 hover:text-red-600 focus-visible:ring-2
                         focus-visible:ring-indigo-400/60 active:scale-95 dark:text-slate-400
                         dark:hover:bg-red-400/10 dark:hover:text-red-300"
            >
              Clear
            </button>
            <button
              onClick={launch}
              disabled={launching}
              title="Launch script (Ctrl+Enter)"
              className="btn-primary ml-1 px-3"
            >
              <RocketIcon className="h-3.5 w-3.5" />
              Launch
            </button>
          </div>
        </div>

        {/* Code area */}
        <div className="flex min-h-0 flex-1">
          {/* Line numbers */}
          <div
            ref={gutterRef}
            className="editor-metrics w-14 shrink-0 overflow-hidden py-4 pr-3 text-right font-mono
                       whitespace-pre text-slate-300 transition-colors duration-300 dark:text-[#3d4157]"
          >
            {gutter}
          </div>

          {/* Highlight layer + input overlay */}
          <div className="relative min-w-0 flex-1">
            <pre
              ref={highlightRef}
              aria-hidden="true"
              className="editor-metrics pointer-events-none absolute inset-0 m-0 overflow-hidden py-4 pr-4
                         font-mono whitespace-pre text-slate-800 transition-colors duration-300 dark:text-[#e6e8f0]"
            >
              <code dangerouslySetInnerHTML={{ __html: html }} />
            </pre>

            <textarea
              ref={inputRef}
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
                         font-mono whitespace-pre text-transparent caret-indigo-600 outline-none
                         selection:bg-indigo-500/20 dark:caret-indigo-400 dark:selection:bg-indigo-400/25"
            />
          </div>
        </div>

        {/* Status bar */}
        <div
          className="flex items-center justify-between border-t border-slate-200 px-4 py-2 text-[11.5px]
                     text-slate-400 transition-colors duration-300 dark:border-white/[.06] dark:text-slate-500"
        >
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1.5">
              <span
                className={`h-1.5 w-1.5 rounded-full ${launching ? "animate-pulse bg-amber-400" : "bg-emerald-400"}`}
              />
              <span>{launching ? "Launching…" : "Ready"}</span>
            </span>
            <span>{value.length} chars</span>
          </div>
          <div className="flex items-center gap-3">
            <span>
              Ln {caret.line}, Col {caret.col}
            </span>
            <span>UTF-8</span>
          </div>
        </div>
      </main>
    </section>
  );
}
