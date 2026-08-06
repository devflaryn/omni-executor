/* Transient confirmations, bottom-right, announced to screen readers. */

import { Lamp } from "./ui.jsx";

const TONES = { success: "live", error: "fault", info: "busy" };

export default function Toast({ toast }) {
  return (
    <div
      aria-live="polite"
      className={`pointer-events-none fixed right-5 bottom-5 z-50 flex max-w-[22rem] items-start gap-2.5
                  rounded-xl border border-line bg-canvas px-3.5 py-2.5 text-[12.5px] leading-snug
                  text-ink shadow-xl shadow-black/30 transition-all duration-300
                  ${toast ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0"}`}
    >
      <Lamp tone={TONES[toast?.tone] || "busy"} className="mt-[5px]" />
      <span>{toast?.message}</span>
    </div>
  );
}
