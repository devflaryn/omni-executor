//! The Python backend, spoken to over its own stdin/stdout.
//!
//! `main.py` used to BE the window (pywebview) and every method on its `Api`
//! class was reachable from JS as `window.pywebview.api.<name>`. It is now a
//! headless child process, and this module is the bridge that replaced that
//! call path:
//!
//!     JS  invoke("call", {method, args})
//!       -> {"id": 7, "method": "engine_start", "args": [...]}   (child stdin)
//!       <- {"id": 7, "ok": true, "result": {...}}               (child stdout)
//!
//! and, in the other direction, the push bus the frontend already had:
//!
//!     {"event": "engine-progress", "payload": {...}}  ->  emit("omni://event")
//!
//! STDIO AND NOT A SOCKET, deliberately. A loopback server would need a port,
//! a token and a firewall exception, and this is a machine where closed
//! loopback ports blackhole rather than refuse. A pipe has none of that, and
//! it dies with the parent for free.
//!
//! Every call is multiplexed by `id`: one writer behind a mutex, one reader
//! thread, and a pending-map of one-shot channels. That is what keeps a
//! 40-second `bootstrap_start` from blocking a 5 ms `get_settings` behind it.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

/// The Tauri event every backend push arrives on. `api.js` re-fires these into
/// `window.omniEvent`, so the frontend's own event bus is unchanged.
pub const EVENT: &str = "omni://event";

/// Name of the frozen backend binary that sits beside this one in an install.
#[cfg(windows)]
const BACKEND_EXE: &str = "omni-exec-py.exe";
#[cfg(not(windows))]
const BACKEND_EXE: &str = "omni-exec-py";

pub struct Backend {
    /// `Option` so `shutdown` can TAKE it: dropping the write end is what the
    /// backend sees as EOF, and EOF is the whole shutdown protocol.
    stdin: Mutex<Option<ChildStdin>>,
    /// id -> the one-shot that the reader thread hands the reply to.
    pending: Arc<Mutex<HashMap<u64, SyncSender<Value>>>>,
    next_id: AtomicU64,
    child: Mutex<Child>,
}

impl Backend {
    /// Start the backend and wire its stdout to `app`'s event bus.
    pub fn spawn(app: &AppHandle) -> Result<Arc<Backend>, String> {
        let (program, args, cwd) = resolve(app)?;
        let mut command = Command::new(&program);
        command
            .args(&args)
            .current_dir(&cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // stderr is NOT piped: the backend logs there, and inheriting it
            // means those lines land wherever this process's stderr goes
            // instead of filling a pipe nobody drains (which would eventually
            // block the backend mid-log).
            .stderr(Stdio::inherit());
        // A GUI app must not flash a console window per launch.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = command.spawn().map_err(|e| {
            format!("could not start the backend ({}): {e}", program.display())
        })?;
        let stdin = child.stdin.take().expect("stdin was piped");
        let stdout = child.stdout.take().expect("stdout was piped");

        let pending: Arc<Mutex<HashMap<u64, SyncSender<Value>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let backend = Arc::new(Backend {
            stdin: Mutex::new(Some(stdin)),
            pending: Arc::clone(&pending),
            next_id: AtomicU64::new(1),
            child: Mutex::new(child),
        });

        let app = app.clone();
        std::thread::Builder::new()
            .name("omni-backend-reader".into())
            .spawn(move || read_loop(BufReader::new(stdout), pending, app))
            .map_err(|e| format!("could not start the backend reader: {e}"))?;

        Ok(backend)
    }

    /// Call a backend method and wait for its reply.
    ///
    /// Blocking on purpose — the command that calls this hands it to a
    /// blocking thread, and some of these methods legitimately take minutes
    /// (`bootstrap_start` downloads gigabytes). There is no timeout for that
    /// reason; a backend that dies drops the pending sender instead, and the
    /// receive fails immediately rather than hanging forever.
    pub fn call(&self, method: &str, args: Value) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        // Bound 1: exactly one reply per id, and the reader must never block.
        let (tx, rx) = sync_channel::<Value>(1);
        self.pending.lock().unwrap().insert(id, tx);

        let line = format!(
            "{}\n",
            json!({"id": id, "method": method, "args": args})
        );
        let write = {
            let mut guard = self.stdin.lock().unwrap();
            match guard.as_mut() {
                Some(stdin) => stdin.write_all(line.as_bytes()).and_then(|_| stdin.flush()),
                None => Err(std::io::Error::other("the backend has been shut down")),
            }
        };
        if let Err(e) = write {
            self.pending.lock().unwrap().remove(&id);
            return Err(format!("the backend is not accepting calls: {e}"));
        }

        match rx.recv() {
            Ok(reply) => Ok(reply),
            // The reader thread dropped the sender: the backend's stdout
            // closed, which means the backend is gone.
            Err(_) => Err("the backend stopped responding".into()),
        }
    }

    /// Stop the backend. Best-effort, and safe to call twice.
    ///
    /// ASK FIRST, THEN INSIST. Dropping our end of the pipe is what
    /// `rpc.serve()` sees as EOF, and it returns — which lets `main.py` run its
    /// `finally` and `atexit` handlers (the heartbeat and update threads get
    /// asked to stop, and the presence lease is deliberately left to lapse).
    /// Killing it outright skips all of that, so the kill is the fallback for a
    /// backend that is wedged, not the first move.
    ///
    /// Instances it started keep running either way — that is the engine's
    /// contract, and closing the app has never powered a VM off.
    pub fn shutdown(&self) {
        if let Ok(mut guard) = self.stdin.lock() {
            drop(guard.take()); // EOF
        }
        let Ok(mut child) = self.child.lock() else { return };
        // Short: this runs while the window is closing and the user is already
        // looking at an empty desktop. A backend that has not noticed EOF in a
        // second is not going to.
        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while std::time::Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => return, // exited on its own
                Ok(None) => std::thread::sleep(Duration::from_millis(25)),
                Err(_) => break,
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// Read replies and pushes until the child's stdout closes.
fn read_loop<R: BufRead>(
    reader: R,
    pending: Arc<Mutex<HashMap<u64, SyncSender<Value>>>>,
    app: AppHandle,
) {
    for line in reader.lines() {
        let Ok(line) = line else { break };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(message) = serde_json::from_str::<Value>(line) else {
            // Not a frame. The backend repoints sys.stdout at stderr precisely
            // so this cannot happen, but a corrupt line must never take the
            // bridge down with it.
            eprintln!("[omni-exec] unparseable backend line: {line}");
            continue;
        };

        if let Some(id) = message.get("id").and_then(Value::as_u64) {
            if let Some(tx) = pending.lock().unwrap().remove(&id) {
                let _ = tx.send(message);
            }
            continue;
        }
        if message.get("event").is_some() {
            let _ = app.emit(EVENT, message);
        }
    }
    // Backend gone: fail every in-flight call rather than leave them hanging.
    pending.lock().unwrap().clear();
}

/// Where the backend lives: (program, args, working directory).
///
/// Three layouts, checked in the order that makes the shipped one cheapest:
///
///   1. `$OMNI_BACKEND` — an explicit path, for tests and for running a
///      checkout's backend against a built shell.
///   2. Beside this executable (Windows/Linux install), or in
///      `Contents/Resources/backend` (the macOS bundle).
///   3. A source checkout: this repo's `main.py` under its own venv.
fn resolve(app: &AppHandle) -> Result<(PathBuf, Vec<String>, PathBuf), String> {
    let rpc = vec!["--rpc".to_string()];

    if let Some(explicit) = std::env::var_os("OMNI_BACKEND") {
        let path = PathBuf::from(explicit);
        let cwd = parent_of(&path);
        return Ok((path, rpc, cwd));
    }

    let exe = std::env::current_exe()
        .map_err(|e| format!("cannot locate this executable: {e}"))?;
    let exe_dir = parent_of(&exe);

    for candidate in [
        exe_dir.join(BACKEND_EXE),
        // macOS: Contents/MacOS/omni-exec -> Contents/Resources/backend/
        exe_dir.join("../Resources/backend").join(BACKEND_EXE),
    ] {
        if candidate.is_file() {
            let cwd = parent_of(&candidate);
            return Ok((candidate, rpc, cwd));
        }
    }

    source_checkout(app, rpc)
}

/// Dev fallback: `python main.py --rpc` out of the checkout this shell was
/// built from. Never reached by an installed build, which always finds the
/// frozen backend beside it.
fn source_checkout(
    _app: &AppHandle,
    mut args: Vec<String>,
) -> Result<(PathBuf, Vec<String>, PathBuf), String> {
    let project = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("the crate has no parent directory")?
        .to_path_buf();
    let main_py = project.join("main.py");
    if !main_py.is_file() {
        return Err(format!(
            "no backend found: neither {BACKEND_EXE} beside the app nor \
             {} in the checkout. Set OMNI_BACKEND to point at one.",
            main_py.display()
        ));
    }

    // Prefer the checkout's own venv: it is where pywebview's replacement
    // dependencies (and selenium, and PIL) actually are.
    let venv = [
        project.join(".venv/Scripts/python.exe"),
        project.join(".venv/bin/python"),
    ]
    .into_iter()
    .find(|p| p.is_file());
    let python = venv.unwrap_or_else(|| {
        PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
    });

    args.insert(0, main_py.to_string_lossy().into_owned());
    Ok((python, args, project))
}

fn parent_of(path: &Path) -> PathBuf {
    path.parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}
