# Omni Executor

Desktop GUI (pywebview + React) for the **omnidroid** engine — each account is
an isolated, headless Android (Bliss OS) instance managed via QEMU. The GUI
drives the engine exclusively through its CLI (`omnidroid.exe`, JSON output);
see `omnidroid.md` for the engine's own manual.

## Setup

```sh
pip install -r requirements.txt
cd frontend && npm install && npm run build
```

Put `omnidroid.exe` (Windows) or `omnidroid` (Linux) next to `main.py`.
The engine self-bootstraps on first use; the Accounts tab shows a readiness
banner (with a "Run setup" button) if anything is missing.

## Run

```sh
python main.py
```

- **Editor** — Lua editor (execution backend not wired yet).
- **Accounts** — create/start/stop/remove accounts, live engine state.
  Double-click or "Open viewer" attaches a controllable noVNC view; closing
  the viewer only disconnects — instances keep running until explicitly
  stopped.
- **Settings** — profile + theme.

## Frontend development

```sh
cd frontend && npm run dev     # hot-reload dev server (browser preview)
npm run build                  # what python main.py serves (frontend/dist)
```
