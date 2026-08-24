# GUI refresh — titlebar, theme, home, tabbed editor, bulk launch

Date: 2026-08-23. Scope: `omni-executor` (pywebview + React). Engine untouched.

## 1. Brainstorm — what is actually wrong, and the choice made for each

### 1.1 Titlebar / window chrome

**Symptom.** `frameless=True` on Windows makes pywebview set
`FormBorderStyle.None`. That removes the *whole* native frame: no resize
borders (hence "the canvas is not resizable with the cursor"), no Aero Snap
(drag to the top edge to maximise, Win+arrows), no double-click-to-maximise,
no DWM shadow / rounded corners. pywebview's `.pywebview-drag-region` then
moves the window by setting `Location` from JS mouse deltas, which is why a
drag never snaps.

**Options.**
1. Keep frameless, re-implement resize/snap in JS + Python. Rejected: we would
   be rebuilding the window manager and still lose snap layouts, shadows and
   animations.
2. Go back to the native titlebar. Rejected: the point is a VS Code / Discord
   style bar.
3. **Keep the native frame styles (WS_CAPTION | WS_THICKFRAME) and remove only
   the caption by handling `WM_NCCALCSIZE`** — the technique Chromium,
   Electron (`titleBarStyle: hidden`) and VS Code use. The OS keeps owning
   resize on left/right/bottom, snap, shadows, minimise/restore animation,
   the taskbar thumbnail. Dragging is started natively by posting
   `WM_NCLBUTTONDOWN / HTCAPTION` (the same call Tauri's `startDragging`
   makes), so drag-to-top-maximises and Win+Shift+arrows all work. The top
   resize edge (which now lies over the WebView) is a 5 px JS strip that asks
   Python for `WM_NCLBUTTONDOWN / HTTOP|HTTOPLEFT|HTTOPRIGHT`. **Chosen.**

**Per platform.**
- *Windows*: keep pywebview's `frameless=True` (WinForms `FormBorderStyle.None`)
  so WinForms' own size maths assumes no non-client area, then on `shown`
  put the frame styles back on the HWND (`WS_CAPTION | WS_THICKFRAME |
  WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU`) and subclass WndProc with
  ctypes: `WM_NCCALCSIZE` returns the full window as client (inset by one
  frame on every side while maximised, since a maximised window hangs
  off-screen by that much). Snap, Win+arrows, DWM shadow/rounded corners and
  the native move/size loops come from the styles; resize handles on every
  edge are 5 px JS strips that post `WM_NCLBUTTONDOWN / HT*`. *(First cut
  used `frameless=False` + caption-only removal; WinForms then re-added the
  caption height on every maximise→restore — measured +31 px — so the
  full-client approach won.)* Minimise / maximise / close stay on the **right**.
- *macOS*: pywebview's frameless is already a *titled* window with a
  transparent, full-size-content titlebar; `main.py` re-shows the traffic
  lights (**left**). Keep that. Drag becomes native:
  `performWindowDragWithEvent:` on the main thread (Tauri does the same), so
  Spaces/Mission Control/double-click behave. Double-click honours
  `AppleActionOnDoubleClick` (Zoom / Minimize / None).
- *Linux (GTK)*: undecorated window as now; drag → `Gtk.Window.begin_move_drag`,
  top resize → `begin_resize_drag`, both via `GLib.idle_add`. Buttons on the
  **right**. Not verifiable here; coded defensively (every call is best-effort).
- *Browser preview*: no chrome at all (unchanged).

**Frontend.** `WindowBar` → `TitleBar`. Drag surfaces use a new class
`app-drag` (not `.pywebview-drag-region`, whose JS fallback would fight the
native drag loop). mousedown (button 0, not on an interactive element) →
`api("begin_window_drag")`; dblclick → `api("titlebar_double_click")`.
A `ResizeEdges` component renders the top strip/corners on Windows/Linux when
not maximised. Maximised state comes from Python (`toggle_maximize` return +
a `window-state` event pushed on the OS maximise/restore events so Win+Up
updates the button glyph).

### 1.2 Theme is too dark

Canvas `#0a0a0a` → a warm-neutral graphite. New dark sheet:
canvas `#17181b`, surface `#1d1e22`, raised `#25262b`, line `#31333a`,
line-soft `#282a30`. Inks lifted slightly. `index.html` pre-paint and
`create_window(background_color=…)` follow. Light theme unchanged.
The monochrome *syntax* palette is replaced by a restrained coloured one
(see 1.4) in both themes.

### 1.3 Home tab

First nav item (Ctrl+1), default tab on launch. One screen, no scrolling on a
1024×720 window:
- Header: greeting + engine lamp/label.
- Stat tiles: **Accounts** (total), **Running** (local running / elsewhere),
  **Scripts** (open editor tabs). Tiles are buttons → Instances / Editor.
- Quick actions: **New script** (new editor tab, switch to Editor),
  **Launch all** (every stopped local account, current launch settings),
  **Stop all** (only shown while something runs), **Add account**, **Settings**.
- Two columns: **Recent scripts** (editor tabs by last edit; click opens) and
  **Instances** (name, lamp, mode chip; click → Instances tab).

### 1.4 Tabbed editor with persistence and real syntax colours

- Store: `{ tabs: [{ id, name, content, createdAt, updatedAt, caret, scroll }], activeId }`.
- Persisted to `editor.json` in the config dir via new `get_editor_state` /
  `save_editor_state` Python calls (localStorage in a browser), debounced
  400 ms, flushed on `beforeunload`. Everything is autosaved — "remember the
  states of the tabs" means what you see on relaunch is what you left.
- One `<CodeSurface>` (gutter + highlight + textarea) is mounted per tab and
  hidden when inactive, so native undo, scroll and caret survive tab switches
  for free.
- Tab bar above the toolbar: name, dirty pulse, ✕ (middle-click closes too),
  ＋ new, double-click to rename, Ctrl+N / Ctrl+W / Ctrl+Tab.
- Tokenizer gains classes: keyword, builtin, **function** (identifier
  followed by `(` or declared with `function`), **variable** (other
  identifiers), **property** (after `.`/`:`), number, string, comment,
  **operator**. Colours: kw violet, fn blue, var default ink, prop teal,
  str green, num amber, com grey italic, op muted — tuned per theme.
- Target selector: **All** (default) + only *running* accounts. "All" runs the
  script on every running account and shows one result per account.
  Nothing running → the Run button explains instead of failing.

### 1.5 Instances: bulk run with a selected mode

- Rows get a checkbox (visible on hover / when any is checked) and the panel
  head gets a select-all checkbox. Click still focuses one row as before.
- With ≥2 checked, the Launch bay's button becomes **Launch N selected** using
  the bay's own Mode / Graphics / Place (that *is* "a selected mode" — no
  second selector). A bulk launch is multi-instance by definition, so the
  single-instance guard is skipped and the bay says so. **Stop N** appears
  for checked running rows.
- `engine.startMany(names, launch)` / `engine.stopMany(names)` fire one
  engine call per account (pywebview runs each bridge call on its own
  thread), staggered 1.2 s so fifty QEMU spawns do not land in the same
  millisecond.

## 2. Data flow / interfaces

Python `Api` additions: `begin_window_drag()`, `begin_window_resize(edge)`,
`titlebar_double_click()`, `get_editor_state()`, `save_editor_state(state)`.
Events pushed: `window-state {maximized}`. `DEFAULT_SETTINGS.activeTab = "home"`.

Frontend additions: `components/TitleBar.jsx` (+`ResizeEdges`),
`components/HomeView.jsx`, `editorStore.js` (state + persistence),
`components/EditorView.jsx` rewritten around tabs, `lua.js` tokenizer
extended, `AccountsView.jsx` selection + bulk bay, `engine.jsx` `startMany`/
`stopMany`, `styles.css` palette.

## 3. Testing

- `pytest tests/test_titlebar.py`: pure helpers (`_nccalcsize_top`, edge →
  hit-test code map), editor-state round-trip through a temp config dir.
- `npm run build` must pass; visual check of every view in the Vite preview
  with the browser; Windows run of `python main.py` for drag/snap/resize.

## 4. Out of scope

Win11 snap-layout flyout on hover over the maximise button (needs
`HTMAXBUTTON` hit-testing through WebView2), a real file system for scripts,
Linux verification.
