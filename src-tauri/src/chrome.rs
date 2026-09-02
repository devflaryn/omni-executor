//! Platform window chrome that the page cannot draw for itself.
//!
//! Everything visible about this window — the titlebar, the buttons, the
//! rounded sheet — is HTML. Two things are not, and they live here:
//!
//!   * Windows: the 33 px corner is CUT OUT OF THE HWND with a round-rect
//!     region. WebView2's windowed hosting honours alpha 0 but has no PARTIAL
//!     per-pixel alpha, so a page-drawn `border-radius` alone leaves the
//!     corners as hard steps against whatever is behind the window. Clipping
//!     the window itself is what makes the arc real, and it is the same
//!     technique (and the same number) the pywebview build used.
//!   * Windows 11 additionally rounds and outlines every window itself, at its
//!     own 8 px radius. Both are turned off, or they fight the 33 px arc.
//!
//! macOS and Linux need neither: their compositors composite partial alpha, so
//! the page's own `border-radius` is the window's edge.

#[cfg(windows)]
use tauri::WebviewWindow;

/// The window's corner radius in CSS pixels at 100% scaling.
///
/// ONE number in three places: here, `--window-radius` in `styles.css`, and
/// the pre-mount `html::before` in `index.html`. They must agree, or the
/// page's corner and the window's corner part company.
#[cfg(windows)]
pub const CORNER_RADIUS: f64 = 33.0;

/// One-time setup: stop Windows 11 from drawing its own corner and border.
#[cfg(windows)]
pub fn configure(window: &WebviewWindow) {
    use windows::Win32::Graphics::Dwm::{
        DwmSetWindowAttribute, DWMWA_BORDER_COLOR, DWMWA_WINDOW_CORNER_PREFERENCE,
        DWMWCP_DONOTROUND,
    };

    let Some(hwnd) = hwnd(window) else { return };
    // Pre-Windows-11 DWM does not know these attributes and returns an error.
    // That is fine: there is no system rounding there to turn off.
    unsafe {
        let pref = DWMWCP_DONOTROUND;
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            &pref as *const _ as *const std::ffi::c_void,
            std::mem::size_of_val(&pref) as u32,
        );
        const DWMWA_COLOR_NONE: u32 = 0xFFFF_FFFE;
        let color = DWMWA_COLOR_NONE;
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_BORDER_COLOR,
            &color as *const _ as *const std::ffi::c_void,
            std::mem::size_of_val(&color) as u32,
        );
    }
}

/// Re-cut the round-rect region. Cheap and safe to call on every resize.
///
/// A MAXIMISED window gets NO region: Windows squares every maximised window,
/// and a rounded one shows desktop through the notches at the screen edge. The
/// page mirrors that by dropping its own radius (`.window-shell.is-maximized`).
#[cfg(windows)]
pub fn apply(window: &WebviewWindow) {
    use windows::Win32::Foundation::{POINT, RECT};
    use windows::Win32::Graphics::Gdi::{
        ClientToScreen, CreateRoundRectRgn, DeleteObject, SetWindowRgn, HGDIOBJ,
    };
    use windows::Win32::UI::HiDpi::GetDpiForWindow;
    use windows::Win32::UI::WindowsAndMessaging::{GetClientRect, GetWindowRect, IsZoomed};

    let Some(hwnd) = hwnd(window) else { return };
    unsafe {
        let mut frame = RECT::default();
        let mut client = RECT::default();
        if GetWindowRect(hwnd, &mut frame).is_err() || GetClientRect(hwnd, &mut client).is_err() {
            return;
        }
        let w = client.right;
        let h = client.bottom;
        if IsZoomed(hwnd).as_bool() || w <= 0 || h <= 0 {
            // NULL region = "the whole window", which is what a maximised
            // window wants.
            SetWindowRgn(hwnd, None, true);
            return;
        }

        // THE REGION IS CUT AROUND THE CLIENT RECT, NOT THE WINDOW RECT, and
        // that is the whole trick. An undecorated window still carries the
        // invisible resize frame — measured here at 8px on the sides and 1px
        // at the top — so the window rect is bigger than the page and offset
        // from it. Cutting the window rect puts the arc 8px out from the one
        // the page draws: the two corners disagree, the frame margin paints
        // its own pale edge outside the sheet, and the clip does nothing
        // useful. Anchoring it to the client rect is what makes the HWND's
        // corner and the page's corner the same corner.
        let mut origin = POINT::default();
        let _ = ClientToScreen(hwnd, &mut origin);
        let (dx, dy) = (origin.x - frame.left, origin.y - frame.top);

        // WebView2 scales the page by the window's DPI, so the cut has to
        // scale with it or the two corners disagree at anything but 100%.
        let dpi = match GetDpiForWindow(hwnd) {
            0 => 96,
            d => d,
        };
        let radius = (CORNER_RADIUS * dpi as f64 / 96.0).round().max(1.0) as i32;
        // CreateRoundRectRgn takes the ELLIPSE size, not the radius. The +1 on
        // right/bottom is not slop: the rect is exclusive there, and without
        // it the window loses its last row and column.
        let region = CreateRoundRectRgn(dx, dy, dx + w + 1, dy + h + 1, radius * 2, radius * 2);
        if region.is_invalid() {
            return;
        }
        // SetWindowRgn takes ownership ON SUCCESS. Only free it if it refused.
        if SetWindowRgn(hwnd, Some(region), true) == 0 {
            let _ = DeleteObject(HGDIOBJ(region.0));
        }
    }
}

/// Tauri and this crate may not agree on a `windows` version, so the handle
/// makes the trip through a plain integer rather than being passed as a typed
/// HWND. They happen to agree today, which is exactly why this is written down:
/// the day Tauri bumps its `windows` dependency, this keeps compiling instead
/// of failing with a type error nobody expected in a window-corner routine.
#[cfg(windows)]
fn hwnd(window: &WebviewWindow) -> Option<windows::Win32::Foundation::HWND> {
    let raw = window.hwnd().ok()?.0 as isize;
    Some(windows::Win32::Foundation::HWND(raw as *mut std::ffi::c_void))
}

#[cfg(not(windows))]
pub fn configure(_window: &tauri::WebviewWindow) {}

/// macOS and Linux composite partial alpha, so the page's own `border-radius`
/// is already the window's edge and there is nothing to cut.
#[cfg(not(windows))]
pub fn apply(_window: &tauri::WebviewWindow) {}
