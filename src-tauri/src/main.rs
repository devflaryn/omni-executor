// A GUI app must not open a console window on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Omni Executor — the window.
//!
//! This process owns exactly two things: the window and the pipe to the Python
//! backend. Every feature of the app — the engine, accounts, bootstrap, cloud
//! sync, updates — is still Python, unchanged, and reached through
//! `backend::Backend::call`. The frozen backend beside this binary is also the
//! omnidroid engine (`--omnidroid` in-binary dispatch), which is why the two
//! ship in the same folder.
//!
//! The window is frameless and transparent on every platform so the page can
//! draw its own 33 px sheet:
//!
//!   macOS         a titled window with an OVERLAY titlebar and a hidden
//!                 title, which is what leaves the native traffic lights in
//!                 place at top-left over the page.
//!   Windows/Linux undecorated; the page draws minimise/maximise/close on the
//!                 right and hands drags and resizes back to the OS through
//!                 `startDragging` / `startResizeDragging`.

mod backend;
mod chrome;

use std::sync::Arc;

use serde_json::Value;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

/// The window is created hidden so the first frame the user sees is a painted
/// page rather than an empty transparent hole. The frontend calls `app_ready`
/// once React has mounted; this is the backstop for a frontend that fails to
/// boot, so a broken page still gives the user a window they can close.
const SHOW_FALLBACK_MS: u64 = 2500;

struct AppState {
    backend: Arc<backend::Backend>,
}

/// The whole JS -> Python surface. `method` is a method name on Python's `Api`
/// class and `args` its positional arguments, exactly as the pywebview bridge
/// passed them.
#[tauri::command]
async fn call(
    state: tauri::State<'_, AppState>,
    method: String,
    args: Vec<Value>,
) -> Result<Value, String> {
    let backend = Arc::clone(&state.backend);
    // The wait is blocking and some calls take minutes, so it must not sit on
    // an async runtime thread.
    tauri::async_runtime::spawn_blocking(move || backend.call(&method, Value::Array(args)))
        .await
        .map_err(|e| format!("the bridge task failed: {e}"))?
}

/// Reveal the window. Called by the frontend once it has painted.
#[tauri::command]
fn app_ready(window: tauri::WebviewWindow) {
    let _ = window.show();
    let _ = window.set_focus();
}

/// Say so, plainly, when a DEBUG build is started without its dev server.
///
/// A debug build points at `build.devUrl` and does NOT embed `frontend/dist` —
/// that is Tauri's design, not a setting here — so `target/debug/omni-exec.exe`
/// cannot run on its own, ever. Started from Explorer it looks like the app and
/// then shows a WebView2 connection error, in the OS's language, naming
/// `localhost` and nothing else. That is a genuinely baffling thing to hand
/// someone who just double-clicked what looks like the program.
///
/// So: probe the dev server first and, if it is not there, explain which binary
/// they wanted. Same reasoning as `_fatal_dialog` in main.py — a windowed build
/// has no console, so the message has to go somewhere a GUI user will see it.
#[cfg(debug_assertions)]
fn require_dev_server(app: &tauri::App) {
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::Duration;

    let Some(dev_url) = app.config().build.dev_url.clone() else {
        return; // no dev server expected: this build embeds its assets
    };
    let host = dev_url.host_str().unwrap_or("localhost").to_string();
    let port = dev_url.port_or_known_default().unwrap_or(80);

    // A SHORT timeout, per address. On this machine a closed loopback port can
    // blackhole rather than refuse, and Vite binds `::1` only unless told
    // otherwise -- so both families get tried and neither may hang the launch.
    let reachable = format!("{host}:{port}")
        .to_socket_addrs()
        .map(|addrs| {
            addrs
                .take(4)
                .any(|a| TcpStream::connect_timeout(&a, Duration::from_millis(750)).is_ok())
        })
        .unwrap_or(false);
    if reachable {
        return;
    }

    let message = format!(
        "This is a DEBUG build, and it has no frontend of its own.\n\n\
         It loads the page from the dev server at {dev_url}, which is not \
         running, so all it can show you is a connection error.\n\n\
         Run the app:            npm run tauri dev\n\
         Or use a real build:    src-tauri\\target\\release\\omni-exec.exe\n\
         (build it with:         cargo build --release)"
    );
    eprintln!("[omni-exec] {message}");
    #[cfg(windows)]
    unsafe {
        use windows::core::HSTRING;
        use windows::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONINFORMATION, MB_OK};
        MessageBoxW(
            None,
            &HSTRING::from(message),
            &HSTRING::from("Omni Executor — development build"),
            MB_OK | MB_ICONINFORMATION,
        );
    }
    std::process::exit(1);
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![call, app_ready])
        .setup(|app| {
            #[cfg(debug_assertions)]
            require_dev_server(app);

            let handle = app.handle().clone();
            let backend = backend::Backend::spawn(&handle)?;
            app.manage(AppState {
                backend: Arc::clone(&backend),
            });

            let builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Omni Executor")
                .inner_size(1380.0, 840.0)
                .min_inner_size(720.0, 480.0)
                .resizable(true)
                // Transparent so the page's own 33 px radius is the window's
                // edge: a pixel the page never paints is alpha 0, and the
                // corners fall outside the sheet.
                .transparent(true)
                .center()
                .visible(false);

            #[cfg(target_os = "macos")]
            let builder = {
                use tauri::TitleBarStyle;
                // Overlay + hidden title = the page runs full-bleed under a
                // transparent titlebar, and the traffic lights stay where a
                // Mac user expects them: top-LEFT, drawn by the OS. Set at
                // CREATION because macOS builds the titlebar backdrop at first
                // show and ignores later changes.
                builder
                    .decorations(true)
                    .title_bar_style(TitleBarStyle::Overlay)
                    .hidden_title(true)
            };

            #[cfg(not(target_os = "macos"))]
            let builder = builder.decorations(false).shadow(true);

            let window = builder.build()?;
            chrome::configure(&window);
            chrome::apply(&window);

            #[cfg(debug_assertions)]
            if std::env::args().any(|a| a == "--devtools")
                || std::env::var("OMNI_DEVTOOLS").as_deref() == Ok("1")
            {
                window.open_devtools();
            }

            // Keep the cut in step with the window. Resized covers drag-resize
            // and maximise/restore alike; ScaleFactorChanged covers a drag to
            // a monitor at a different DPI.
            let chrome_window = window.clone();
            window.on_window_event(move |event| match event {
                WindowEvent::Resized(_) | WindowEvent::ScaleFactorChanged { .. } => {
                    chrome::apply(&chrome_window);
                }
                _ => {}
            });

            let fallback = window.clone();
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(SHOW_FALLBACK_MS));
                if matches!(fallback.is_visible(), Ok(false)) {
                    let _ = fallback.show();
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to start Omni Executor")
        .run(|app, event| {
            // The backend outlives the window only long enough to be told to
            // stop. Instances it started keep running — that is the engine's
            // contract, and closing the app has never powered a VM off.
            if let RunEvent::Exit = event {
                if let Some(state) = app.try_state::<AppState>() {
                    state.backend.shutdown();
                }
            }
        });
}
