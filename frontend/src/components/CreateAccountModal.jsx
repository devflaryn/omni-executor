/* Create account — the batch signup dialog.

   One form (amount, username style, optional custom password) drives a
   background Python thread that opens roblox.com's registration page for each
   account, fills an 18+ birthday plus generated credentials, handles the
   captcha (automatically when a provider key is saved in Settings, otherwise
   by the human in the browser window it opened), and harvests the session
   cookie off roblox.com/home. Progress arrives as omniEvent pushes, so the
   dialog can be closed mid-run — the batch keeps going and reopening the
   dialog reattaches to it via creation_status(). */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, onEngineEvent } from "../api.js";
import { Button, Field, Modal, Toggle } from "./ui.jsx";
import { CopyIcon } from "./icons.jsx";

const STYLE_OPTIONS = [
  { id: "name_no", label: "Name + numbers", example: "Eric848381 · John_858289" },
  { id: "adj_noun", label: "Adjective + noun", example: "FrozenWolf1192 · Ultra_Hawk948" },
  { id: "gamertag", label: "Gamer tag", example: "Bright_Shark858 · EpicWizard436" },
  { id: "stealth", label: "Stealth", example: "mwu78cnas782n" },
];

function copyText(text, showToast) {
  const done = () => showToast?.("Copied to clipboard", "success");
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    done?.();
  } finally {
    ta.remove();
  }
}

export default function CreateAccountModal({ onClose, showToast, onCreated }) {
  const [amount, setAmount] = useState(1);
  const [style, setStyle] = useState("name_no");
  const [override, setOverride] = useState(false);
  const [customPassword, setCustomPassword] = useState("");
  const [loaded, setLoaded] = useState(false);

  const [running, setRunning] = useState(false);
  const [at, setAt] = useState({ index: 0, total: 0 });
  const [log, setLog] = useState([]);
  const [results, setResults] = useState([]);
  // Refs mirror the live state inside the event subscription, which React
  // mounts once — reading state there directly would see the first render's.
  const logRef = useRef(null);
  const startedHere = useRef(false);

  // Seed the form from saved preferences and reattach to a run already going.
  useEffect(() => {
    let alive = true;
    Promise.all([api("creation_get_config"), api("creation_status")]).then(
      ([cfg, status]) => {
        if (!alive) return;
        const c = cfg?.creation || {};
        setAmount(Math.min(50, Math.max(1, Number(c.amount) || 1)));
        if (STYLE_OPTIONS.some((s) => s.id === c.usernameStyle)) setStyle(c.usernameStyle);
        if (status?.running) {
          setRunning(true);
          setAt({ index: status.index || 0, total: status.total || 0 });
          setLog((l) => [...l, "Reattached to the running batch."]);
        }
        setLoaded(true);
      }
    );
    return () => {
      alive = false;
    };
  }, []);

  useEffect(
    () =>
      onEngineEvent((event, p) => {
        if (event === "creation-progress") {
          setAt({ index: p.index || 0, total: p.total || 0 });
          if (p.message) setLog((l) => [...l.slice(-120), String(p.message)]);
        } else if (event === "creation-account") {
          setResults((r) => [...r, p]);
          setLog((l) => [
            ...l.slice(-120),
            p.ok ? `${p.index}: created ${p.username}` : `${p.index}: failed — ${p.error}`,
          ]);
        } else if (event === "creation-done") {
          setRunning(false);
          setAt({ index: 0, total: 0 });
          setLog((l) => [...l.slice(-120), p.message || "Batch finished."]);
          if ((p.created || 0) > 0) onCreated?.();
        }
      }),
    [onCreated]
  );

  // Keep the newest line visible without stealing focus from the form.
  useEffect(() => {
    const el = logRef.current;
    if (el && running) el.scrollTop = el.scrollHeight;
  }, [log, running]);

  const persistPrefs = useCallback((nextAmount, nextStyle) => {
    api("creation_save_config", {
      creation: { amount: nextAmount, usernameStyle: nextStyle },
    });
  }, []);

  const start = async () => {
    if (override && customPassword.length < 8) {
      showToast("A custom password needs at least 8 characters", "error");
      return;
    }
    const payload = { amount: Number(amount), usernameStyle: style };
    if (override) payload.customPassword = customPassword;
    const res = await api("creation_start", payload);
    if (!res.ok) {
      showToast(res.message || "Could not start account creation", "error");
      return;
    }
    persistPrefs(Number(amount), style);
    startedHere.current = true;
    setRunning(true);
    setResults([]);
    setAt({ index: 0, total: res.total || Number(amount) });
    setLog((l) => [...l, `Creating ${res.total} account(s)...`]);
  };

  const stop = async () => {
    await api("creation_stop");
    setLog((l) => [...l, "Stopping after the current step..."]);
  };

  const styleExample = STYLE_OPTIONS.find((s) => s.id === style)?.example;

  return (
    <Modal title="Create Roblox accounts" onClose={onClose}>
      {!running ? (
        <div className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="How many" htmlFor="create-amount" hint="1–50, created one by one.">
              <input
                id="create-amount"
                type="number"
                min={1}
                max={50}
                value={amount}
                disabled={!loaded}
                onChange={(e) =>
                  setAmount(e.target.value === "" ? "" : Math.min(50, Math.max(1, Number(e.target.value) || 1)))
                }
                className="input font-mono"
              />
            </Field>
            <Field label="Username style" htmlFor="create-style">
              <select
                id="create-style"
                value={style}
                disabled={!loaded}
                onChange={(e) => {
                  setStyle(e.target.value);
                  persistPrefs(amount, e.target.value);
                }}
                className="input cursor-pointer"
              >
                {STYLE_OPTIONS.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
              <p className="font-mono text-[11.5px] leading-snug text-ink-3">{styleExample}</p>
            </Field>
          </div>

          <p className="text-[12px] leading-relaxed text-ink-3">
            Each account gets a random birthday (18+), a random gender and a secure password.
            A browser window opens per account; captchas are solved automatically when an API
            key is set in Settings → Captcha, otherwise you solve them in that window.
          </p>

          <div className="rule-t pt-3">
            <Toggle
              id="create-password-override"
              checked={override}
              onChange={setOverride}
              label="Use a custom password"
              hint="Off, every account gets its own generated one."
            />
          </div>
          {override && (
            <Field label="Password for every account" htmlFor="create-password">
              <input
                id="create-password"
                type="text"
                value={customPassword}
                onChange={(e) => setCustomPassword(e.target.value)}
                className="input font-mono"
                placeholder="At least 8 characters"
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
          )}

          <div className="mt-1 flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="solid" onClick={start} disabled={!loaded}>
              Create {Number(amount) || 1} {Number(amount) === 1 ? "account" : "accounts"}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between text-[13px] text-ink-2">
            <span>
              {at.total > 0
                ? `Account ${Math.min(at.index + 1, at.total)} of ${at.total}`
                : "Working…"}
            </span>
            <span className="font-mono text-[12px] text-ink-3">
              {results.filter((r) => r.ok).length} created ·{" "}
              {results.filter((r) => !r.ok).length} failed
            </span>
          </div>

          <div
            ref={logRef}
            className="h-[168px] overflow-y-auto rounded-lg border border-line bg-raised px-2.5 py-2
                       font-mono text-[11.5px] leading-relaxed text-ink-3"
          >
            {log.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all">
                {line}
              </div>
            ))}
          </div>

          {results.length > 0 && (
            <ul className="flex max-h-[140px] flex-col gap-1 overflow-y-auto">
              {results.map((r, i) => (
                <li
                  key={i}
                  className="flex items-center gap-2 rounded-md border border-line px-2 py-1.5 text-[13px]"
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      r.ok ? "bg-live" : "bg-danger"
                    }`}
                    aria-hidden="true"
                  />
                  <span className={`truncate font-mono ${r.ok ? "text-ink" : "text-danger"}`}>
                    {r.username || `#${r.index}`}
                  </span>
                  {r.ok ? (
                    <>
                    <button
                      type="button"
                      title="Copy username"
                      aria-label={`Copy username for ${r.username}`}
                      onClick={() => copyText(r.username, showToast)}
                      className="ring-focus ml-auto rounded p-1 text-ink-3 hover:text-accent"
                    >
                      <CopyIcon className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      title="Copy password"
                      aria-label={`Copy password for ${r.username}`}
                      onClick={() => r.password && copyText(r.password, showToast)}
                      className="ring-focus rounded p-1 text-ink-3 hover:text-accent"
                    >
                      <CopyIcon className="h-3 w-3 rotate-180" />
                    </button>
                    </>
                  ) : (
                    <span className="ml-auto truncate pl-2 text-right text-[11.5px] text-ink-3">
                      {r.error}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}

          <p className="text-[12px] leading-relaxed text-ink-3">
            You can close this window — the batch keeps running in the background.
          </p>

          <div className="flex justify-end gap-2">
            <Button onClick={stop}>Stop batch</Button>
            <Button variant="solid" onClick={onClose}>
              Hide
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
