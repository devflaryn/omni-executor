/* Network: is the outside world reachable, and how fast.

   Two things live here, and they belong together because they are the same
   subject: the LINK CHECK, which says whether Roblox and the Omni server are
   answering, and the PROXY, which is the one setting that changes the answer.
   The proxy used to sit in Settings among theme and profile — a network knob
   filed under decoration, with no way to tell whether it worked. Here it gets
   its own row in the check the moment it is set, so typing an address and
   seeing it come up green is one motion.

   Everything on this page is a measurement, so nothing on it is invented:
   a target with no reading shows a dash, never a guess. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Button, Field, Lamp, Panel, PanelHead, Toggle } from "./ui.jsx";
import { GlobeIcon, RefreshIcon, RouteIcon, SignalIcon } from "./icons.jsx";

// While the tab is open and auto-check is on. Long enough that the page is
// not a traffic generator, short enough that a link going down is noticed
// before the user has moved on.
const AUTO_MS = 20000;

// How many readings the trace keeps per target. Twelve at 20s is four
// minutes — enough to see a link flapping, short enough to stay a glance.
const TRACE = 12;

// The three verdicts netcheck.classify() can return, plus the state the UI
// owns on its own while a probe is in flight.
const STATUS = {
  ok: { label: "OK", tone: "live" },
  slow: { label: "Slow", tone: "warn" },
  down: { label: "Down", tone: "fault" },
  checking: { label: "Checking", tone: "busy" },
  unknown: { label: "—", tone: "off" },
};

const RANK = { ok: 0, slow: 1, down: 2 };

const ICONS = { proxy: RouteIcon };

/** The worst thing any target is saying, which is what the header reports:
    one green lamp over a list containing a dead host would be a lie. */
function overall(targets) {
  if (!targets?.length) return "unknown";
  return targets.reduce((worst, t) => (RANK[t.status] > RANK[worst] ? t.status : worst), "ok");
}

function summarise(targets) {
  if (!targets?.length) return "Not checked yet";
  const bad = targets.filter((t) => t.status !== "ok");
  if (!bad.length) return "Everything is answering";
  // One problem is worth naming; several are worth counting, because a list
  // of three names in a header strip is no longer readable at a glance.
  if (bad.length === 1) return `${bad[0].label} ${bad[0].status === "down" ? "is down" : "is slow"}`;
  const down = bad.filter((t) => t.status === "down").length;
  return down ? `${down} of ${targets.length} down` : `${bad.length} links are slow`;
}

function fmtMs(ms) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} s` : `${Math.round(ms)} ms`;
}

function agoLabel(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 10) return "just now";
  if (s < 90) return `${Math.round(s)}s ago`;
  return `${Math.round(s / 60)} min ago`;
}

export default function NetworkView({ active, onSummary }) {
  // Bumped whenever the proxy is saved, so the check re-runs against what was
  // just typed instead of waiting out the auto interval.
  const [proxyRev, setProxyRev] = useState(0);

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-5 py-5 ${active ? "" : "hidden"}`}>
      <div className="animate-rise mx-auto flex w-full max-w-[640px] flex-col gap-4">
        <LinkCheck active={active} proxyRev={proxyRev} onSummary={onSummary} />
        <ProxyPanel onSaved={() => setProxyRev((n) => n + 1)} />
      </div>
    </div>
  );
}

/* The check itself. It polls only while the tab is on screen: a background
   tab quietly pinging two hosts forever is exactly the behaviour this app
   should not have. */
function LinkCheck({ active, proxyRev, onSummary }) {
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [auto, setAuto] = useState(true);
  const [tick, setTick] = useState(0); // re-render so "12s ago" keeps counting
  // History per target id. A ref, not state: it is written from inside the
  // probe and read during the same render that sets the report.
  const trace = useRef({});
  const inFlight = useRef(false);
  const alive = useRef(true);

  useEffect(() => () => { alive.current = false; }, []);

  const check = useCallback(async () => {
    // A probe can take a full timeout. Overlapping calls would double the
    // traffic and interleave their traces out of order.
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    const res = await api("net_probe");
    inFlight.current = false;
    if (!alive.current) return;
    setBusy(false);
    if (res?.ok === false) {
      setReport(res);
      return;
    }
    for (const t of res?.targets || []) {
      const prev = trace.current[t.id] || [];
      trace.current[t.id] = [...prev, { ms: t.ms, status: t.status }].slice(-TRACE);
    }
    // A target that vanished (the proxy was cleared) must not keep a trace
    // that would reappear, stale, the next time one is configured.
    const live = new Set((res?.targets || []).map((t) => t.id));
    for (const id of Object.keys(trace.current)) if (!live.has(id)) delete trace.current[id];
    setReport(res);
  }, []);

  // First look at the tab, and every time the proxy changes underneath it.
  useEffect(() => {
    if (active) check();
  }, [active, proxyRev, check]);

  useEffect(() => {
    if (!active || !auto) return;
    const id = setInterval(check, AUTO_MS);
    return () => clearInterval(id);
  }, [active, auto, check]);

  // Keeps the "checked N s ago" line honest between probes.
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setTick((n) => n + 1), 5000);
    return () => clearInterval(id);
  }, [active]);
  void tick;

  const targets = report?.targets || [];
  const state = busy && !targets.length ? "checking" : overall(targets);
  const failed = report?.ok === false;
  const headline = failed
    ? "Check failed"
    : busy && !targets.length
      ? "Checking…"
      : summarise(targets);

  // The context bar carries the same sentence this panel's header does, so
  // the two can never disagree about how the link is doing.
  useEffect(() => {
    if (active) onSummary?.(headline);
  }, [active, headline, onSummary]);

  return (
    <Panel className="overflow-hidden">
      <PanelHead
        icon={SignalIcon}
        title="Connection"
        right={
          <span className="flex items-center gap-2 text-[12.5px] text-ink-2">
            <Lamp tone={STATUS[state].tone} pulse={busy} size={6} />
            {headline}
          </span>
        }
      />

      <div className="flex flex-col gap-4 p-4">
        {failed ? (
          <p className="text-[12.5px] leading-relaxed text-danger">
            {report.message || "The probe could not run."}
          </p>
        ) : targets.length === 0 ? (
          <p className="text-[12.5px] text-ink-3">Checking the link…</p>
        ) : (
          <ul className="flex flex-col">
            {targets.map((t, i) => (
              <TargetRow
                key={t.id}
                target={t}
                trace={trace.current[t.id] || []}
                first={i === 0}
                busy={busy}
              />
            ))}
          </ul>
        )}

        <div className="rule-t flex items-center gap-2 pt-4">
          <Button size="sm" onClick={check} disabled={busy}>
            <RefreshIcon className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
            {busy ? "Checking…" : "Check now"}
          </Button>
          <span className="silk ml-auto text-ink-3">{agoLabel(report?.checkedAt)}</span>
        </div>

        <Toggle
          id="net-auto"
          checked={auto}
          onChange={setAuto}
          label="Keep checking"
          hint={`Re-check every ${AUTO_MS / 1000} seconds while this tab is open.`}
        />

        {/* The legend comes from the same numbers the verdicts do, so it can
            never describe a threshold the backend no longer uses. */}
        {report?.okMs != null && (
          <p className="text-[12px] leading-relaxed text-ink-3">
            Under {report.okMs} ms is <span className="text-ink-2">OK</span>; anything slower that
            still answers is <span className="text-ink-2">Slow</span>; no answer within{" "}
            {Math.round((report.timeoutMs || 6000) / 1000)} s is{" "}
            <span className="text-ink-2">Down</span>. Each reading is a whole request, so it sits
            above what a ping would show.
          </p>
        )}
      </div>
    </Panel>
  );
}

/* One host: what it is, what it is for, how long it took, and the last few
   readings so a number that just spiked is distinguishable from a link that
   has been bad all along. */
function TargetRow({ target, trace, first, busy }) {
  const status = STATUS[target.status] || STATUS.unknown;
  const Icon = ICONS[target.id] || GlobeIcon;
  return (
    <li className={`flex items-start gap-3 py-3 ${first ? "" : "rule-t"}`} style={{ "--rule-inset": "0px" }}>
      <Icon className="mt-[3px] h-4 w-4 shrink-0 text-ink-3" />

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-baseline gap-2">
          <span className="text-[13.5px] font-medium text-ink">{target.label}</span>
          {/* Mono, not the `silk` label style the rest of the app uses for
              system data: a URL is a literal string, and the one place the
              app shows one it should be set in the face that does not make
              its own decisions about character width. */}
          <span
            className="truncate font-mono text-[11.5px] tracking-[0.02em] text-ink-3"
            title={target.url}
          >
            {target.url.replace(/^https?:\/\//, "")}
          </span>
        </div>
        <p className="text-[12px] leading-snug text-ink-3">
          {/* The detail is the interesting half whenever there is one — "Name
              not resolved (DNS)" tells the user what to fix; the note only
              says what the host is for. */}
          {target.detail || target.note}
        </p>
        <Trace samples={trace} />
      </div>

      <div className="flex shrink-0 flex-col items-end gap-1">
        <span
          className={`font-mono text-[16px] leading-none tabular-nums ${
            target.status === "down" ? "text-ink-3" : "text-ink"
          } ${busy ? "opacity-60" : ""} transition-opacity duration-200`}
        >
          {fmtMs(target.ms)}
        </span>
        <span className="flex items-center gap-1.5">
          <Lamp tone={status.tone} size={6} />
          <span className="silk text-ink-3">{status.label}</span>
        </span>
      </div>
    </li>
  );
}

/* The last few readings as bars, scaled against the worst of them. A failed
   probe draws full height in the fault colour rather than nothing at all —
   "no answer" is the loudest reading there is, and a gap would read as
   "nothing happened". */
function Trace({ samples }) {
  if (samples.length < 2) return null;
  const peak = Math.max(1, ...samples.map((s) => s.ms || 0));
  return (
    <div className="flex h-4 items-end gap-[3px]" aria-hidden="true">
      {samples.map((s, i) => {
        const down = s.status === "down";
        const height = down ? 100 : Math.max(12, ((s.ms || 0) / peak) * 100);
        return (
          <span
            key={i}
            className={`w-[3px] rounded-[1px] ${
              down ? "bg-danger/70" : s.status === "slow" ? "bg-warn/70" : "bg-ink-3/50"
            }`}
            style={{ height: `${height}%` }}
          />
        );
      })}
    </div>
  );
}

/* The outbound proxy for account creation. This is NOT about captchas: Roblox
   rate-limits signup per IP (after roughly ten attempts the form returns "an
   unknown error occurred" and never even shows a challenge), so a fresh
   address is what keeps a batch running. The third-party captcha providers
   that used to share this panel were removed — Arkose refuses to issue a
   puzzle to a solver's IP at all, so no service could produce a token for
   Roblox.

   It lives on the Network tab rather than in Settings because the check above
   is what tells you whether the address you pasted actually works. */
function ProxyPanel({ onSaved }) {
  const [proxy, setProxy] = useState("");
  const [proxyErr, setProxyErr] = useState("");
  const [loaded, setLoaded] = useState(false);
  const saveTimer = useRef(null);

  useEffect(() => {
    let alive = true;
    api("creation_get_config").then((cfg) => {
      if (!alive) return;
      setProxy(String(cfg?.captcha?.proxy || ""));
      setLoaded(true);
    });
    return () => {
      alive = false;
      clearTimeout(saveTimer.current);
    };
  }, []);

  const save = (nextProxy) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      const res = await api("creation_save_config", {
        captcha: { proxy: nextProxy.trim() },
      });
      // The proxy can be malformed and the backend validates it, so surface
      // its verdict rather than silently keeping a string that fails a batch.
      const bad = res?.ok === false;
      setProxyErr(bad ? String(res.message || "Invalid proxy.") : "");
      // Only a saved value is worth re-probing: a rejected one is not what
      // the backend would dial.
      if (!bad) onSaved?.();
    }, 500);
  };

  return (
    <Panel className="overflow-hidden">
      <PanelHead
        icon={RouteIcon}
        title="Account creation proxy"
        right={
          <span className="flex items-center gap-2 text-[12.5px] text-ink-2">
            <Lamp tone={proxy.trim() ? "live" : "off"} size={6} />
            {proxy.trim() ? "Via proxy" : "Direct"}
          </span>
        }
      />
      <div className="flex flex-col gap-4 p-4">
        <Field
          label="Outbound proxy"
          htmlFor="net-proxy"
          hint="host:port:user:pass — a fresh IP per batch avoids Roblox's signup rate limit."
        >
          <input
            id="net-proxy"
            type="text"
            className="input font-mono tracking-wide"
            placeholder="gate.provider.io:7000:user:pass"
            value={proxy}
            disabled={!loaded}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              setProxy(e.target.value);
              save(e.target.value);
            }}
          />
        </Field>

        {proxyErr && <p className="text-[12px] leading-relaxed text-danger">{proxyErr}</p>}

        <p className="text-[12px] leading-relaxed text-ink-3">
          Set one and it joins the check above, measured the way creation uses it — a real request
          to Roblox through the proxy. Only the browser that signs accounts up goes through it;
          nothing else in the app does.
        </p>

        {/* Kept from the old Settings panel: the thing people most often come
            to this field looking for is automatic captcha solving. */}
        <p className="text-[12px] leading-relaxed text-ink-3">
          Captchas are solved by hand in the browser window that opens. Automatic solving is being
          rebuilt to play the puzzle in that window.
        </p>
      </div>
    </Panel>
  );
}
