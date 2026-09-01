/* Shared primitives. The Lamp is the app's signature device: a recessed
   indicator that reports engine, instance and editor state the same way
   everywhere, so one glance reads the whole panel. */

import { useEffect, useRef } from "react";
import { CloseIcon } from "./icons.jsx";

const LAMP_TONES = {
  live: { dot: "bg-live", glow: "bg-live/40" },
  busy: { dot: "bg-accent", glow: "bg-accent/45" },
  // "Working, but not well" — the Network tab's slow link. Its own tone
  // rather than a borrowed one: `busy` is white and means work in progress,
  // and a link that answers in two seconds is not in progress, it is bad.
  warn: { dot: "bg-warn", glow: "bg-warn/45" },
  fault: { dot: "bg-danger", glow: "bg-danger/45" },
  off: { dot: "bg-ink-3/55", glow: "bg-transparent" },
};

export function Lamp({ tone = "off", pulse = false, size = 7, className = "" }) {
  const { dot, glow } = LAMP_TONES[tone] || LAMP_TONES.off;
  return (
    <span
      className={`relative inline-flex shrink-0 ${pulse ? "animate-pulse" : ""} ${className}`}
      style={{ width: size, height: size }}
    >
      <span className={`absolute -inset-[3px] rounded-full blur-[3px] ${glow}`} />
      <span className={`relative h-full w-full rounded-full ${dot}`} />
    </span>
  );
}

/** A region of the sheet, not a card: no background, no border. Its head
    carries the label and a floating hairline does the separating. */
export function Panel({ className = "", children, ...rest }) {
  return (
    <section className={className} {...rest}>
      {children}
    </section>
  );
}

export function PanelHead({ icon: Icon, title, count, right }) {
  return (
    <header className="rule-b flex h-13 shrink-0 items-center gap-2.5 px-4">
      {Icon && <Icon className="h-4 w-4 text-ink-3" />}
      {/* Body-weight, sentence case — a panel is titled the way a column on a
          board is, not labelled the way a form field is. */}
      <h2 className="text-[14px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
      {count != null && (
        <span className="rounded-full bg-raised px-2 py-[2px] text-[11.5px] font-semibold text-ink-3">
          {count}
        </span>
      )}
      <div className="ml-auto flex items-center gap-1.5">{right}</div>
    </header>
  );
}

const BTN_SIZES = {
  sm: "h-8 px-3 text-[12.5px]",
  md: "h-9.5 px-4",
  lg: "h-11 px-5 text-[15px]",
};

export function Button({ variant = "quiet", size = "md", className = "", children, ...rest }) {
  return (
    <button
      type="button"
      className={`btn btn-${variant} ${BTN_SIZES[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

/** Icon-only control. `label` is required — it becomes both the tooltip and
    the accessible name. */
export function IconButton({ label, tone = "", className = "", children, ...rest }) {
  const toneClass =
    tone === "danger"
      ? "hover:bg-danger/12 hover:text-danger"
      : tone === "accent"
        ? "hover:bg-accent/12 hover:text-accent"
        : "";
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={`icon-btn ${toneClass} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Field({ label, hint, htmlFor, children }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="silk text-ink-3">
        {label}
      </label>
      {children}
      {hint && <p className="text-[12px] leading-snug text-ink-3">{hint}</p>}
    </div>
  );
}

export function Toggle({ checked, onChange, label, hint, id }) {
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-center justify-between gap-4 py-0.5"
    >
      <span className="min-w-0">
        <span className="block text-[13.5px] font-medium text-ink">{label}</span>
        {hint && <span className="block text-[12px] leading-snug text-ink-3">{hint}</span>}
      </span>
      <span className="relative shrink-0">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="peer sr-only"
        />
        <span
          className="block h-[24px] w-[42px] rounded-full border border-line bg-raised transition-colors
                     duration-200 peer-checked:border-accent peer-checked:bg-accent
                     peer-focus-visible:ring-2 peer-focus-visible:ring-accent/55
                     peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-canvas"
        />
        <span
          className="pointer-events-none absolute top-[5px] left-[5px] h-[14px] w-[14px] rounded-full
                     bg-ink-3 transition-all duration-200 peer-checked:translate-x-[18px]
                     peer-checked:bg-accent-ink"
        />
      </span>
    </label>
  );
}

/** A compact on/off switch with no label of its own.

    `Toggle` above is a full row — label, hint, control — which is right in
    Settings and wrong anywhere the switch sits INSIDE something that already
    names it: a list row, a panel header. This is the same visual language at
    list scale, as a real `role="switch"` button so it is reachable by keyboard
    and announced correctly, and it stops click propagation so a switch inside
    a clickable row toggles instead of opening the row. */
export function MiniSwitch({ checked, onChange, label, disabled = false, title }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={title || label}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        e.preventDefault();
        if (!disabled) onChange(!checked);
      }}
      className={`ring-focus relative inline-flex h-[18px] w-[32px] shrink-0 items-center rounded-full
                  border transition-colors duration-200
                  ${disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer"}
                  ${checked ? "border-accent bg-accent" : "border-line bg-raised"}`}
    >
      <span
        className={`pointer-events-none absolute h-[10px] w-[10px] rounded-full transition-all duration-200
                    ${checked ? "left-[18px] bg-accent-ink" : "left-[3px] bg-ink-3"}`}
      />
    </button>
  );
}

/** Modal dialog: closes on Escape and on backdrop click, focuses its first
    field on open, and restores focus to whatever opened it. */
export function Modal({ title, tone = "default", onClose, children, footer }) {
  const panelRef = useRef(null);
  const returnFocus = useRef(null);

  useEffect(() => {
    returnFocus.current = document.activeElement;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    panelRef.current?.querySelector("input, textarea, button")?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      returnFocus.current?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="animate-fade fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6 backdrop-blur-[2px]"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="animate-rise w-full max-w-md overflow-hidden rounded-3xl border border-line bg-canvas shadow-2xl shadow-black/40"
      >
        <header className="rule-b flex h-13 items-center gap-2 px-4.5">
          <h2
            className={`text-[14px] font-semibold tracking-[-0.01em] ${
              tone === "danger" ? "text-danger" : "text-ink"
            }`}
          >
            {title}
          </h2>
          <IconButton label="Close" onClick={onClose} className="ml-auto -mr-1">
            <CloseIcon className="h-3.5 w-3.5" />
          </IconButton>
        </header>
        <div className="p-4.5">{children}</div>
        {footer && (
          <footer className="rule-t flex items-center justify-end gap-2 px-4.5 py-3.5">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

/** Inline notice used for engine problems and preview-mode hints. */
export function Notice({ tone = "info", icon: Icon, children, action }) {
  const tones = {
    info: "border-line text-ink-2",
    warn: "border-accent/30 bg-accent/5 text-ink",
    error: "border-danger/35 bg-danger/8 text-danger",
  };
  return (
    <div
      className={`animate-rise flex items-start gap-3 rounded-xl border px-4 py-3.5 text-[13.5px] leading-relaxed ${tones[tone]}`}
    >
      {Icon && <Icon className="mt-[2px] h-4 w-4 shrink-0" />}
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}
