/* The editor's tabs: one store, shared by the Editor (which edits them) and
   Home (which lists them as "recent scripts").

   Every tab is autosaved — there is no file system behind the editor, so
   "saved" would be a lie and "unsaved changes" a nag. What you see when the
   app comes back is what you left: the same tabs, the same text, the same
   active tab. State lives in editor.json next to settings.json (Python
   `get_editor_state` / `save_editor_state`); a plain browser uses
   localStorage. Writes are debounced because this changes on every
   keystroke, and flushed at once on anything structural (new/close/rename). */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { hasBackend } from "./api.js";
import { SAMPLE_SCRIPT } from "./lua.js";

const STORAGE_KEY = "omni-editor";
const SAVE_DELAY_MS = 250;

function newId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function makeTab(name, content = "") {
  const now = Date.now();
  return { id: newId(), name, content, createdAt: now, updatedAt: now };
}

/** `untitled.lua`, then `untitled-2.lua`, … — never a name already open. */
export function nextUntitledName(tabs) {
  const taken = new Set(tabs.map((t) => t.name));
  if (!taken.has("untitled.lua")) return "untitled.lua";
  for (let i = 2; ; i += 1) {
    const name = `untitled-${i}.lua`;
    if (!taken.has(name)) return name;
  }
}

function sanitize(raw) {
  if (!raw || !Array.isArray(raw.tabs)) return null;
  const tabs = raw.tabs
    .filter((t) => t && typeof t.id === "string" && typeof t.content === "string")
    .map((t) => ({
      id: t.id,
      name: typeof t.name === "string" && t.name.trim() ? t.name : "untitled.lua",
      content: t.content,
      createdAt: Number(t.createdAt) || Date.now(),
      updatedAt: Number(t.updatedAt) || Date.now(),
    }));
  if (!tabs.length) return null;
  const activeId = tabs.some((t) => t.id === raw.activeId) ? raw.activeId : tabs[0].id;
  return { tabs, activeId };
}

function firstRun() {
  const tab = makeTab("untitled.lua", SAMPLE_SCRIPT);
  return { tabs: [tab], activeId: tab.id };
}

async function load() {
  try {
    if (await hasBackend()) return sanitize(await window.pywebview.api.get_editor_state());
    return sanitize(JSON.parse(localStorage.getItem(STORAGE_KEY)));
  } catch {
    return null;
  }
}

async function persist(state) {
  try {
    if (await hasBackend()) await window.pywebview.api.save_editor_state(state);
    else localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (err) {
    console.error("Failed to save editor state:", err);
  }
}

const EditorContext = createContext(null);

export function useEditorStore() {
  const store = useContext(EditorContext);
  if (!store) throw new Error("useEditorStore must be used inside <EditorStoreProvider>");
  return store;
}

export function EditorStoreProvider({ children }) {
  const [state, setState] = useState(null); // null until loaded
  const saveTimer = useRef(null);
  const latest = useRef(null);

  useEffect(() => {
    let alive = true;
    load().then((saved) => {
      if (alive) setState(saved || firstRun());
    });
    return () => {
      alive = false;
    };
  }, []);

  // Persist: debounced on edits, immediately on structure. `latest` lets the
  // unload flush write the newest state without a render in between.
  latest.current = state;
  const schedule = useCallback((immediate) => {
    clearTimeout(saveTimer.current);
    if (immediate) {
      persist(latest.current);
      return;
    }
    saveTimer.current = setTimeout(() => persist(latest.current), SAVE_DELAY_MS);
  }, []);

  useEffect(() => {
    const flush = () => {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        persist(latest.current);
      }
    };
    window.addEventListener("beforeunload", flush);
    return () => window.removeEventListener("beforeunload", flush);
  }, []);

  const update = useCallback(
    (fn, immediate = false) => {
      setState((prev) => {
        const next = fn(prev);
        latest.current = next;
        return next;
      });
      // setState callbacks run before paint; schedule after so `latest` holds
      // the new state by the time the timer fires.
      queueMicrotask(() => schedule(immediate));
    },
    [schedule]
  );

  const newTab = useCallback(
    (name, content = "") => {
      const tab = makeTab(name || "", content);
      update((prev) => {
        const named = { ...tab, name: tab.name || nextUntitledName(prev.tabs) };
        return { tabs: [...prev.tabs, named], activeId: named.id };
      }, true);
      return tab.id;
    },
    [update]
  );

  const selectTab = useCallback(
    (id) => update((prev) => (prev.activeId === id ? prev : { ...prev, activeId: id }), true),
    [update]
  );

  const closeTab = useCallback(
    (id) =>
      update((prev) => {
        const index = prev.tabs.findIndex((t) => t.id === id);
        if (index === -1) return prev;
        let tabs = prev.tabs.filter((t) => t.id !== id);
        // Never leave the editor with no tab: closing the last one opens a
        // fresh untitled one, like every editor does.
        if (!tabs.length) tabs = [makeTab("untitled.lua", "")];
        const activeId =
          prev.activeId === id ? tabs[Math.min(index, tabs.length - 1)].id : prev.activeId;
        return { tabs, activeId };
      }, true),
    [update]
  );

  const renameTab = useCallback(
    (id, name) => {
      const clean = String(name || "").trim();
      if (!clean) return;
      update((prev) => ({
        ...prev,
        tabs: prev.tabs.map((t) => (t.id === id ? { ...t, name: clean } : t)),
      }), true);
    },
    [update]
  );

  const updateContent = useCallback(
    (id, content) =>
      update((prev) => ({
        ...prev,
        tabs: prev.tabs.map((t) =>
          t.id === id && t.content !== content ? { ...t, content, updatedAt: Date.now() } : t
        ),
      })),
    [update]
  );

  const value = useMemo(() => {
    const tabs = state?.tabs || [];
    const activeId = state?.activeId || null;
    return {
      loaded: state !== null,
      tabs,
      activeId,
      activeTab: tabs.find((t) => t.id === activeId) || null,
      // Most recently edited first — what Home calls "recent scripts".
      recent: [...tabs].sort((a, b) => b.updatedAt - a.updatedAt),
      newTab,
      selectTab,
      closeTab,
      renameTab,
      updateContent,
    };
  }, [state, newTab, selectTab, closeTab, renameTab, updateContent]);

  return <EditorContext.Provider value={value}>{children}</EditorContext.Provider>;
}
