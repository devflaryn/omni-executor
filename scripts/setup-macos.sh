#!/usr/bin/env bash
#
# One command that takes a Mac from nothing to a runnable Omni Executor.
#
#   scripts/setup-macos.sh              install everything, then say how to run
#   scripts/setup-macos.sh --check      report only; change nothing
#   scripts/setup-macos.sh --run        install, then launch the app
#
# IDEMPOTENT. Every step asks before it acts, so running this on a machine that
# is already set up is a fast no-op that prints what it found. That is the
# property that makes it usable as a repair as well as an install — "run the
# setup script again" has to be safe advice.
#
# What it installs, and why each one is here rather than assumed:
#
#   Homebrew                  everything below comes from it
#   qemu                      the emulator. NOTE: Homebrew's build has no
#                             virglrenderer and no gl display backend, so the
#                             guest renders in SOFTWARE on this host. See the
#                             GPU note at the end — it is a real limitation and
#                             this script reports it rather than hiding it.
#   android-platform-tools    adb. omnidroid/adb.py shells the BARE NAME "adb",
#                             so a machine without it fails every guest command
#                             at the exec, not with a readable error.
#   node                      the GUI is a React app; without node there is no
#                             `npm run build` and the window comes up blank or
#                             stale. The one dependency most easily forgotten,
#                             because a checkout with a committed frontend/dist
#                             looks fine until the frontend changes.
#   python + venv + pip deps  pywebview (the window), selenium (add-account
#                             login), pillow (the VNC viewer)
#
# What it deliberately does NOT do: download base images. Those are several
# gigabytes and the app fetches them itself on first boot, into its own runtime
# dir, with sha256 verification and resume. Duplicating that here would give a
# second, worse copy of a thing that already works.
#
set -uo pipefail

cd "$(dirname "$0")/.."
OE="$(pwd)"
OMNIDROID_REPO="${OMNIDROID_REPO:-$OE/../omnidroid}"
BREW_BIN="/opt/homebrew/bin"          # Apple Silicon; Intel uses /usr/local/bin
[ -x "$BREW_BIN/brew" ] || BREW_BIN="/usr/local/bin"
export PATH="$BREW_BIN:$PATH"

CHECK_ONLY=0
RUN_AFTER=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --run)   RUN_AFTER=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

FAILED=0
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
info() { printf '   ..   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }

# Install a brew formula unless the command it provides already exists.
# Keyed on the COMMAND, not on `brew list`: a qemu installed by any other means
# is still a qemu, and reinstalling over it is how a working machine breaks.
need_brew() {
  local cmd="$1" formula="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd — $(command -v "$cmd")"
    return 0
  fi
  if [ "$CHECK_ONLY" = 1 ]; then bad "$cmd is missing (brew install $formula)"; return 1; fi
  info "installing $formula…"
  if brew install "$formula" >/tmp/omni-brew-$formula.log 2>&1; then
    hash -r
    command -v "$cmd" >/dev/null 2>&1 && ok "$cmd installed" || bad "$formula installed but $cmd is still not on PATH"
  else
    bad "brew install $formula failed — see /tmp/omni-brew-$formula.log"
  fi
}

say "1. Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "brew — $(command -v brew)"
elif [ "$CHECK_ONLY" = 1 ]; then
  bad "Homebrew is missing"
else
  info "installing Homebrew (this one asks for your password)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    && export PATH="$BREW_BIN:$PATH" && hash -r
  command -v brew >/dev/null 2>&1 && ok "Homebrew installed" || bad "Homebrew install failed"
fi

say "2. Emulator and device tools"
need_brew qemu-system-aarch64 qemu
need_brew adb android-platform-tools
need_brew node node
need_brew python3 python

say "3. Python environment"
PY="$OE/.venv/bin/python"
if [ -x "$PY" ]; then
  ok "venv — $($PY -V 2>&1)"
elif [ "$CHECK_ONLY" = 1 ]; then
  bad "no .venv"
else
  info "creating .venv…"
  python3 -m venv "$OE/.venv" && ok "venv created" || bad "could not create .venv"
fi
if [ -x "$PY" ] && [ "$CHECK_ONLY" = 0 ]; then
  info "installing Python dependencies…"
  "$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
  if "$PY" -m pip install --quiet -r "$OE/requirements.txt" >/tmp/omni-pip.log 2>&1; then
    ok "requirements.txt installed"
  else
    bad "pip install failed — see /tmp/omni-pip.log"
  fi
fi
if [ -x "$PY" ]; then
  # Import them rather than trusting pip: a wheel that installed and cannot
  # import (wrong arch, missing system library) is the failure worth catching,
  # and it looks identical to success in pip's output.
  "$PY" - <<'PY' && ok "pywebview / selenium / pillow all import" || bad "a dependency installed but does not import"
import webview, selenium, PIL  # noqa: F401
PY
fi

say "4. The GUI (React)"
if [ ! -d "$OE/frontend" ]; then
  bad "no frontend/ directory"
elif [ "$CHECK_ONLY" = 1 ]; then
  [ -f "$OE/frontend/dist/index.html" ] && ok "frontend/dist present" || bad "frontend not built"
elif command -v npm >/dev/null 2>&1; then
  info "npm install…"
  (cd "$OE/frontend" && npm install --silent >/tmp/omni-npm.log 2>&1) \
    && info "npm run build…" \
    && (cd "$OE/frontend" && npm run build >>/tmp/omni-npm.log 2>&1) \
    && ok "frontend built" \
    || bad "frontend build failed — see /tmp/omni-npm.log"
else
  # A stale dist still launches, which is exactly why this must be said out
  # loud: the window comes up looking fine and showing last month's UI.
  [ -f "$OE/frontend/dist/index.html" ] \
    && warn "no npm — using the COMMITTED frontend/dist, which may be stale" \
    || bad "no npm and no frontend/dist: the window would have nothing to show"
fi

say "5. The engine"
if [ -d "$OMNIDROID_REPO/omnidroid" ]; then
  ok "omnidroid checkout — $OMNIDROID_REPO"
  # omnidroid has zero Python dependencies, so the executor's interpreter runs
  # it as-is; this only proves the package imports.
  PYTHONPATH="$OMNIDROID_REPO" "$PY" -c 'import omnidroid' 2>/dev/null \
    && ok "omnidroid imports" || bad "omnidroid does not import"
else
  bad "no omnidroid checkout beside this one (expected $OMNIDROID_REPO)"
fi

say "6. Virtualisation and GPU"
if sysctl -n kern.hv_support 2>/dev/null | grep -q 1; then
  ok "Hypervisor.framework available — arm64 guests run NATIVELY (no translation)"
else
  bad "kern.hv_support is 0 — no hardware virtualisation"
fi
if command -v qemu-system-aarch64 >/dev/null 2>&1; then
  if qemu-system-aarch64 -device help 2>/dev/null | grep -q virtio-gpu-gl; then
    ok "QEMU has virtio-gpu-gl — the guest can render on the GPU"
  else
    # Not a failure: the app runs, it just draws on the CPU. Reported plainly
    # because it is the single biggest thing separating this machine from a
    # smooth session, and because it is invisible otherwise.
    warn "this QEMU has NO virtio-gpu-gl and no gl display backend, so the"
    warn "guest renders in SOFTWARE (llvmpipe). Homebrew does not ship"
    warn "virglrenderer on macOS; GPU rendering needs a QEMU built with"
    warn "--enable-opengl --enable-virglrenderer. The app still works."
  fi
fi

say "Result"
if [ "$FAILED" = 0 ]; then
  ok "everything this machine needs is present"
  cat <<EOF

   Run it:
     cd "$OE" && .venv/bin/python main.py

   First launch downloads the base images by itself (several GB, resumable,
   sha256-verified) into ~/Library/Application Support/OmniExec.
EOF
  if [ "$RUN_AFTER" = 1 ]; then
    say "Launching"
    exec "$PY" "$OE/main.py"
  fi
else
  printf '\n   \033[31mSomething above needs attention.\033[0m Fix it and run this again —\n'
  printf '   every step is idempotent, so re-running costs nothing.\n\n'
  exit 1
fi
