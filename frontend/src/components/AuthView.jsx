/* The gate. Nothing in the app is reachable until an Omni account is signed in:
   the accounts, the cookies and the ability to run a script all belong to a
   user now, not to a machine.

   Two modes on one screen rather than two screens, because the difference is a
   single field — registering also takes a license key. */

import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Button, Field } from "./ui.jsx";
import { CpuIcon } from "./icons.jsx";

const INPUT =
  "h-9 w-full rounded-lg border border-line bg-raised px-3 text-[13px] text-ink " +
  "outline-none placeholder:text-ink-3 focus:border-accent focus:ring-2 focus:ring-accent/30";

export default function AuthView({ onSignedIn, apiBase, deviceName }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const firstField = useRef(null);

  useEffect(() => {
    firstField.current?.focus();
  }, []);

  // Switching mode must clear the error: a "that key is already redeemed" left
  // hanging over the sign-in form reads as though sign-in itself failed.
  const switchMode = (next) => {
    setMode(next);
    setError(null);
  };

  const submit = async (e) => {
    e?.preventDefault();
    if (busy) return;
    if (!email.trim() || !password) {
      setError("Email and password are required.");
      return;
    }
    if (mode === "register" && !key.trim()) {
      setError("A license key is required to register.");
      return;
    }
    setBusy(true);
    setError(null);
    const res =
      mode === "register"
        ? await api("auth_register", email.trim(), password, key.trim())
        : await api("auth_login", email.trim(), password);
    setBusy(false);
    if (res?.ok) onSignedIn?.(res);
    else setError(res?.message || "Sign-in failed.");
  };

  const isRegister = mode === "register";

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center bg-canvas p-8 text-ink">
      <div className="w-full max-w-[340px]">
        <div className="mb-7 flex flex-col items-center gap-2">
          <CpuIcon className="h-5 w-5 text-accent" />
          <h1 className="text-[15px] font-semibold tracking-tight">Omni Executor</h1>
          <p className="text-[11.5px] text-ink-3">
            {isRegister ? "Create your account" : "Sign in to continue"}
          </p>
        </div>

        <form className="flex flex-col gap-3.5" onSubmit={submit}>
          <Field label="Email" htmlFor="auth-email">
            <input
              id="auth-email"
              ref={firstField}
              type="email"
              autoComplete="username"
              className={INPUT}
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>

          <Field label="Password" htmlFor="auth-password">
            <input
              id="auth-password"
              type="password"
              autoComplete={isRegister ? "new-password" : "current-password"}
              className={INPUT}
              placeholder={isRegister ? "at least 6 characters" : "••••••••"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>

          {isRegister && (
            <Field label="License key" htmlFor="auth-key" hint="30-day, 90-day or lifetime.">
              <input
                id="auth-key"
                className={`${INPUT} font-mono tracking-wide`}
                placeholder="OMNI-XXXX-XXXX-XXXX"
                value={key}
                // Keys are printed and dictated in upper case; accepting lower
                // case silently and then rejecting it would be needless friction.
                onChange={(e) => setKey(e.target.value.toUpperCase())}
              />
            </Field>
          )}

          {error && (
            <p className="rounded-lg border border-danger/35 bg-danger/8 px-3 py-2 text-[12px] text-danger">
              {error}
            </p>
          )}

          <Button
            variant="solid"
            size="lg"
            className="mt-1 w-full justify-center"
            disabled={busy}
            onClick={submit}
          >
            {busy ? "Working…" : isRegister ? "Create account" : "Sign in"}
          </Button>
          {/* A form with one button submits on Enter only if that button is a
              real submit; the shared Button renders type="button". */}
          <button type="submit" className="hidden" aria-hidden="true" tabIndex={-1} />
        </form>

        <p className="mt-5 text-center text-[12px] text-ink-3">
          {isRegister ? "Already have an account?" : "Have a license key?"}{" "}
          <button
            type="button"
            className="text-accent hover:underline"
            onClick={() => switchMode(isRegister ? "login" : "register")}
          >
            {isRegister ? "Sign in" : "Register"}
          </button>
        </p>

        <p className="mt-7 text-center text-[10.5px] text-ink-3">
          {deviceName ? `This device: ${deviceName}` : null}
          {deviceName && apiBase ? " · " : null}
          {apiBase ? apiBase.replace(/^https?:\/\//, "") : null}
        </p>
      </div>
    </div>
  );
}
