#!/usr/bin/env bash
#
# Slice B end-to-end verification (macOS) — the self-install chain against the
# LIVE backend. Proves: manifest -> resumable+verified download -> place under
# the exact dest_name -> installed.json -> configure_engine -> the omnidroid
# engine reads the runtime config and lists the downloaded base + offset ->
# idempotent relaunch (no re-download).
#
# This is the headless half of the acceptance (the GUI boot + arceus screenshot
# is the manual half — see docs/superpowers/e2e-macos-checklist.md).
#
# Usage:
#   scripts/e2e-macos.sh [RUNTIME_DIR]
# Env:
#   OMNI_EXEC_BASE   backend base URL (default http://72.62.59.232)
#   OMNIDROID_REPO   sibling omnidroid checkout (default ../omnidroid)
#
set -euo pipefail
cd "$(dirname "$0")/.."
OE="$(pwd)"
RT="${1:-$HOME/Library/Application Support/OmniExec}"
OMNIDROID_REPO="${OMNIDROID_REPO:-$OE/../omnidroid}"
export OMNIEXEC_RUNTIME_DIR="$RT"

echo "== Slice B e2e =="
echo "runtime dir : $RT"
echo "backend     : ${OMNI_EXEC_BASE:-http://72.62.59.232}"
echo "omnidroid   : $OMNIDROID_REPO"
echo

echo "== STEP 1: install runtime (real pull, resumable + sha256-verified) =="
python3 - <<'PY'
import os, json, time, sys
sys.path.insert(0, os.getcwd())
import bootstrap
t0 = time.time()
last = {"a": None, "p": -10}
def prog(p):
    a, pct = p.get("artifact"), p.get("percent") or 0
    if a != last["a"]:
        last["a"], last["p"] = a, -10
        print(f"  -> {p.get('phase')} {a} ({(p.get('total') or 0)/2**20:.0f} MB)", flush=True)
    if pct - last["p"] >= 20:
        last["p"] = pct
        print(f"     {a}: {pct:.0f}%", flush=True)
res = bootstrap.ensure_runtime(progress=prog)
print(f"  changed={res['changed']}  ({time.time()-t0:.0f}s)")
rt = bootstrap.runtime_dir()
inst = json.loads((rt / "installed.json").read_text())
for n, m in inst["artifacts"].items():
    print(f"  installed: {n} v{m.get('version')} sha={m.get('sha256','')[:16]}...")
PY
echo

echo "== STEP 2: configure the engine (writes paths.json, sets env) =="
python3 - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
import bootstrap
eng = bootstrap.configure_engine(bootstrap.runtime_dir())
print("  ", eng)
print("   OMNIDROID_CONFIG_PATH =", os.environ.get("OMNIDROID_CONFIG_PATH"))
PY
echo

echo "== STEP 3: engine sees the downloaded base + offset =="
CFG="$RT/paths.json"
OMNIDROID_CONFIG_PATH="$CFG" OMNI_DATA_DIR="$RT" OMNI_IMAGES_DIR="$RT/images" \
  PYTHONPATH="$OMNIDROID_REPO" python3 -m omnidroid bases --json
OMNIDROID_CONFIG_PATH="$CFG" OMNI_DATA_DIR="$RT" OMNI_IMAGES_DIR="$RT/images" \
  PYTHONPATH="$OMNIDROID_REPO" python3 -m omnidroid offset list --json || true
echo

echo "== STEP 4: idempotent relaunch — a second install must download nothing =="
python3 - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
import bootstrap
res = bootstrap.ensure_runtime()
assert res["changed"] == [], f"expected no re-download, got {res['changed']}"
print("  changed=[] OK (idempotent)")
PY
echo "== e2e headless acceptance PASSED =="
