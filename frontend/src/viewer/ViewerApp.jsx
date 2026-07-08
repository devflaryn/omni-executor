import { useCallback, useEffect, useRef, useState } from "react";
import RFB from "@novnc/novnc";
import { api } from "../api.js";

/* VNC viewer popup. main.py opens one per account (window title = account
   name) and delivers {session, ws_port} through window.initViewer().

   Engine contract: closing the viewer only disconnects — the instance keeps
   running headless. Stopping is always an explicit user choice. */

const STATUS = {
  connecting: { dot: "animate-pulse bg-amber-400", text: "Connecting…" },
  connected: { dot: "bg-emerald-400", text: "Connected — keyboard & mouse are live" },
  disconnected: { dot: "bg-slate-500", text: "Disconnected" },
  lost: { dot: "bg-red-400", text: "Connection lost" },
};

export default function ViewerApp() {
  const [cfg, setCfg] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [stopping, setStopping] = useState(false);
  const screenRef = useRef(null);
  const rfbRef = useRef(null);

  // Config arrives from Python once the window DOM is loaded.
  useEffect(() => {
    window.__viewerConfig?.then(setCfg);
  }, []);

  const connect = useCallback(() => {
    if (!cfg || !screenRef.current) return;
    try {
      rfbRef.current?.disconnect();
    } catch {
      /* already gone */
    }
    setStatus("connecting");
    const rfb = new RFB(screenRef.current, `ws://127.0.0.1:${cfg.ws_port}/`);
    rfb.scaleViewport = true; // fit the guest screen to the window, keep aspect
    rfb.addEventListener("connect", () => setStatus("connected"));
    rfb.addEventListener("disconnect", (e) =>
      setStatus(e.detail?.clean ? "disconnected" : "lost")
    );
    rfbRef.current = rfb;
  }, [cfg]);

  useEffect(() => {
    connect();
    return () => {
      try {
        rfbRef.current?.disconnect();
      } catch {
        /* already gone */
      }
    };
  }, [connect]);

  // main.py intercepts the native close button and calls this instead.
  useEffect(() => {
    window.showCloseDialog = () => setDialogOpen(true);
    return () => {
      delete window.showCloseDialog;
    };
  }, []);

  const keepRunning = () => {
    if (cfg) api("viewer_close", cfg.session, false);
  };

  const stopInstance = async () => {
    if (!cfg || stopping) return;
    setStopping(true);
    await api("viewer_close", cfg.session, true);
  };

  const s = STATUS[status];

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-[#14151d] font-sans text-slate-200 antialiased select-none">
      {/* Toolbar */}
      <header className="flex h-10 shrink-0 items-center gap-2.5 border-b border-white/[.06] bg-[#181a24] px-3">
        <span className={`h-2 w-2 shrink-0 rounded-full ${s.dot}`} />
        <span className="text-[13px] font-semibold tracking-wide">{cfg?.session ?? "…"}</span>
        <span className="truncate text-[11.5px] text-slate-500">{s.text}</span>
        <div className="flex-1" />
        {(status === "disconnected" || status === "lost") && (
          <button
            onClick={connect}
            className="rounded-lg px-2.5 py-1 text-[12px] font-medium text-slate-400 transition-colors
                       duration-150 outline-none hover:bg-white/[.06] hover:text-slate-100"
          >
            Reconnect
          </button>
        )}
        <button
          onClick={() => setDialogOpen(true)}
          className="rounded-lg px-2.5 py-1 text-[12px] font-medium text-red-300/90 transition-colors
                     duration-150 outline-none hover:bg-red-400/10 hover:text-red-300"
        >
          Stop instance
        </button>
      </header>

      {/* noVNC mounts its canvas here */}
      <main ref={screenRef} className="relative min-h-0 flex-1 cursor-default select-auto" />

      {/* Close / stop dialog — keep running is the default */}
      {dialogOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-[360px] rounded-2xl border border-white/[.06] bg-[#1c1e29] p-5 shadow-2xl">
            <h2 className="text-[14px] font-semibold">Close viewer?</h2>
            <p className="mt-1.5 text-[12px] leading-relaxed text-slate-400">
              Disconnecting keeps <span className="font-semibold text-slate-300">{cfg?.session}</span> running
              headless — you can reopen the viewer anytime. Stopping powers the instance off.
            </p>
            <div className="mt-4 flex flex-col gap-2">
              <button
                autoFocus
                onClick={keepRunning}
                className="rounded-xl bg-indigo-500 px-4 py-2 text-[13px] font-semibold text-white
                           transition-colors duration-150 outline-none hover:bg-indigo-400
                           focus-visible:ring-2 focus-visible:ring-indigo-400/60"
              >
                Keep running (just disconnect)
              </button>
              <button
                onClick={stopInstance}
                disabled={stopping}
                className="rounded-xl bg-white/[.04] px-4 py-2 text-[13px] font-medium text-red-300
                           transition-colors duration-150 outline-none hover:bg-red-400/10
                           focus-visible:ring-2 focus-visible:ring-red-400/60
                           disabled:pointer-events-none disabled:opacity-60"
              >
                {stopping ? "Stopping…" : "Stop instance"}
              </button>
              <button
                onClick={() => setDialogOpen(false)}
                className="rounded-xl px-4 py-2 text-[12px] text-slate-400 transition-colors duration-150
                           outline-none hover:bg-white/[.06] hover:text-slate-200"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
