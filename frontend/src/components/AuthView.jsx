/* The gate. Nothing in the app is reachable until an Omni account is signed in:
   the accounts, the cookies and the ability to run a script all belong to a
   user now, not to a machine.

   Signing up is FREE and asks for no license key — a key is redeemed later, in
   Settings, to put the account on a plan. So the one field registering adds is
   the username: the name the app greets you by, and the one thing here that has
   to be unique across every Omni account. */

import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Button, Field } from "./ui.jsx";
import { CpuIcon } from "./icons.jsx";

const INPUT =
  "h-9 w-full rounded-lg border border-line bg-raised px-3 text-[14px] text-ink " +
  "outline-none placeholder:text-ink-3 focus:border-accent focus:ring-2 focus:ring-accent/30";

// Kept in step with the server's own rule (auth.controller.js): checking it
// here only saves a round trip, it is never what enforces it.
const USERNAME_OK = /^[A-Za-z0-9_]{3,24}$/;

export default function AuthView({ onSignedIn, apiBase, deviceName }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const firstField = useRef(null);

  useEffect(() => {
    firstField.current?.focus();
  }, []);

  // Switching mode must clear the error: a "that username is taken" left
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
    if (mode === "register" && !USERNAME_OK.test(username.trim())) {
      setError("Pick a username: 3–24 letters, numbers or underscores.");
      return;
    }
    setBusy(true);
    setError(null);
    const res =
      mode === "register"
        ? await api("auth_register", email.trim(), username.trim(), password)
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
          <h1 className="text-[16px] font-semibold tracking-tight">Omni Executor</h1>
          <p className="text-[12.5px] text-ink-3">
            {isRegister ? "Create your free account" : "Sign in to continue"}
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

          {/* Between email and password rather than after both: it is part of
              who you are, not a credential, and a form that asks for a password
              and then keeps going reads as though it is not finished. */}
          {isRegister && (
            <Field label="Username" htmlFor="auth-username" hint="Your display name. 3–24 characters, must be unique.">
              <input
                id="auth-username"
                className={INPUT}
                autoComplete="off"
                spellCheck={false}
                maxLength={24}
                placeholder="berat"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </Field>
          )}

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

          {error && (
            <p className="rounded-lg border border-danger/35 bg-danger/8 px-3 py-2 text-[13px] text-danger">
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
            {busy ? "Working…" : isRegister ? "Create free account" : "Sign in"}
          </Button>
          {/* A form with one button submits on Enter only if that button is a
              real submit; the shared Button renders type="button". */}
          <button type="submit" className="hidden" aria-hidden="true" tabIndex={-1} />
        </form>

        <p className="mt-5 text-center text-[13px] text-ink-3">
          {isRegister ? "Already have an account?" : "No account?"}{" "}
          <button
            type="button"
            className="text-accent hover:underline"
            onClick={() => switchMode(isRegister ? "login" : "register")}
          >
            {isRegister ? "Sign in" : "Create one, free"}
          </button>
        </p>

        <p className="mt-7 text-center text-[11.5px] text-ink-3">
          {deviceName ? `This device: ${deviceName}` : null}
          {deviceName && apiBase ? " · " : null}
          {apiBase ? apiBase.replace(/^https?:\/\//, "") : null}
        </p>
      </div>
    </div>
  );
}
