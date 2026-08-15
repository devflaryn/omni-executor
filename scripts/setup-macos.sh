#!/usr/bin/env bash
#
# One command that takes a Mac from nothing to a runnable Omni Executor.
#
#   scripts/setup-macos.sh              install everything, then say how to run
#   scripts/setup-macos.sh --check      report only; change nothing
#   scripts/setup-macos.sh --run        install, then launch the app
#   scripts/setup-macos.sh --gpu        ALSO build a GPU-capable QEMU (slow)
#
# --gpu is separate, and stays separate, because it is a 30-60 minute source
# build of QEMU against a hand-installed virglrenderer+ANGLE — not something
# to do silently inside a setup everyone runs. Without it the app works and
# the guest renders on the CPU; the normal run says so rather than leaving it
# to be discovered as "why is this slow".
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
WANT_GPU=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --run)   RUN_AFTER=1 ;;
    --gpu)   WANT_GPU=1 ;;
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
  # "$PY" quoted: the default checkout path is ~/Desktop/Omni Apps/…, and an
  # unquoted expansion split it at the space and reported a missing /Users/…/Omni
  ok "venv — $("$PY" -V 2>&1)"
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
GPU_QEMU="$HOME/Library/Application Support/OmniExec/qemu-gl"

# Which QEMU the engine will actually run: the GPU build if one is installed,
# otherwise whatever is on PATH.
gpu_qemu_bin() {
  [ -x "$GPU_QEMU/bin/qemu-system-aarch64" ] && echo "$GPU_QEMU/bin/qemu-system-aarch64" \
    || command -v qemu-system-aarch64 2>/dev/null
}

has_virgl() {
  [ -n "${1:-}" ] && "$1" -device help 2>/dev/null | grep -q virtio-gpu-gl
}

QB="$(gpu_qemu_bin)"
if has_virgl "$QB"; then
  ok "GPU rendering available — $QB has virtio-gpu-gl"
  case "$QB" in "$GPU_QEMU"/*) ok "engine is pointed at the GPU build" ;; esac
elif [ "$WANT_GPU" = 0 ]; then
  # Not a failure: the app runs, it just draws on the CPU. Reported plainly
  # because it is the single biggest thing separating this machine from a
  # smooth session, and because it is invisible otherwise.
  warn "the guest renders in SOFTWARE — this QEMU has no virtio-gpu-gl."
  warn "Homebrew ships no virglrenderer on macOS, so GPU rendering needs a"
  warn "QEMU built --enable-opengl --enable-virglrenderer. Run this script"
  warn "with --gpu to build one (30-60 min, ~4 GB). The app works without it."
else
  say "6b. Building a GPU-capable QEMU (--gpu)"
  # WHY BUILD, rather than install something. Checked 2026-08-15, all four:
  #   homebrew core qemu            no virglrenderer, no gl display backend
  #   knazarov/qemu-virgl           QEMU pinned to a 2021 revision, and its
  #                                 test-image resource 404s, so it cannot
  #                                 install -- but its libangle /
  #                                 libepoxy-angle / virglrenderer formulae are
  #                                 the hard, macOS-specific part and DO work.
  #   startergo/...-kosmickrisp     current QEMU, but the formula is named
  #                                 `qemu`, so Homebrew refuses to install it
  #                                 beside the working one -- it REPLACES it.
  #   UTM.app (prebuilt, cask)      ships virglrenderer + ANGLE, but its QEMU
  #                                 is a Mach-O SHARED LIBRARY that UTM dlopens,
  #                                 not an executable, so a manager that spawns
  #                                 a QEMU process cannot use it at all.
  # So: take the three dependency formulae that work, and build a matching
  # QEMU against them into our OWN prefix. The system qemu is never touched,
  # which is the property that makes this safe to try and trivial to undo
  # (`rm -rf "$GPU_QEMU"`).
  if [ "$CHECK_ONLY" = 1 ]; then
    info "(--check) would build a GPU QEMU into $GPU_QEMU"
  else
    # Xcode CLT gates every source build. It is NOT pre-checked, deliberately:
    # a dry-run of a BOTTLED formula never invokes a compiler, so it comes back
    # clean on a machine that cannot build anything (measured — a `--dry-run
    # meson` probe passed here and the very next real install failed on exactly
    # this). The reliable signal is the failure itself, so attempt the install
    # and read the reason out of the log.
    info "installing ANGLE + virglrenderer (the macOS-specific half)…"
    brew tap knazarov/qemu-virgl >/dev/null 2>&1
    brew trust knazarov/qemu-virgl >/dev/null 2>&1 || true
    if brew install knazarov/qemu-virgl/virglrenderer \
                    knazarov/qemu-virgl/libangle \
                    knazarov/qemu-virgl/libepoxy-angle \
                    >/tmp/omni-virgl.log 2>&1; then
      ok "virglrenderer + ANGLE installed"
    elif grep -qi "Command Line Tools are too outdated\|no developer tools" /tmp/omni-virgl.log; then
      bad "Xcode Command Line Tools are too outdated to build anything."
      warn "This blocks the GPU build and NOTHING else — the app itself is"
      warn "fine. The fix needs a password and a GUI, so a script cannot do"
      warn "it. Either:"
      warn "  • System Settings -> General -> Software Update, or"
      warn "  • sudo rm -rf /Library/Developer/CommandLineTools"
      warn "    sudo xcode-select --install"
      warn "then re-run:  scripts/setup-macos.sh --gpu"
    else
      bad "virglrenderer/ANGLE install failed — see /tmp/omni-virgl.log"
    fi
    if [ "$FAILED" = 0 ]; then
      brew install meson ninja pkg-config glib pixman >/dev/null 2>&1
      QV="$(qemu-system-aarch64 --version 2>/dev/null | head -1 | awk '{print $4}')"
      QV="${QV:-11.1.0}"
      SRC="/tmp/omni-qemu-src"
      if [ -x "$GPU_QEMU/bin/qemu-system-aarch64" ]; then
        ok "a GPU QEMU is already built at $GPU_QEMU"
      elif [ "$FAILED" = 0 ]; then
        info "building QEMU $QV against them — this is the slow part…"
        rm -rf "$SRC"; mkdir -p "$SRC"
        if curl -fsSL "https://download.qemu.org/qemu-$QV.tar.xz" \
             | tar xJ -C "$SRC" --strip-components=1 2>/dev/null; then
          # Only the two targets the engine ever invokes; building all ~58
          # system emulators would multiply the build time for nothing.
          ( cd "$SRC" && \
            PKG_CONFIG_PATH="$(brew --prefix virglrenderer)/lib/pkgconfig:$(brew --prefix libepoxy-angle)/lib/pkgconfig:${PKG_CONFIG_PATH:-}" \
            ./configure --prefix="$GPU_QEMU" \
                        --target-list=aarch64-softmmu,x86_64-softmmu \
                        --enable-cocoa --enable-opengl --enable-virglrenderer \
                        --enable-hvf --disable-gtk --disable-sdl \
                        --disable-docs --disable-guest-agent \
              >/tmp/omni-qemu-build.log 2>&1 \
            && make -j"$(sysctl -n hw.ncpu)" >>/tmp/omni-qemu-build.log 2>&1 \
            && make install >>/tmp/omni-qemu-build.log 2>&1 ) \
            && ok "built into $GPU_QEMU" \
            || bad "QEMU build failed — see /tmp/omni-qemu-build.log"
        else
          bad "could not download QEMU $QV source"
        fi
      fi
      QB="$(gpu_qemu_bin)"
      if has_virgl "$QB"; then
        ok "virtio-gpu-gl present in the new build"
        # Point the ENGINE at it rather than putting it on PATH: omnidroid's
        # qemu_bin() consults config `qemu.dir` first, so this is a one-key
        # change that leaves every other user of qemu on the system copy.
        "$PY" - "$GPU_QEMU/bin" <<'PY' && ok "engine config points at the GPU build"
import json, os, sys
cfg = os.path.expanduser("~/Library/Application Support/OmniExec/paths.json")
try:
    d = json.load(open(cfg))
except (OSError, ValueError):
    d = {}
d.setdefault("qemu", {})["dir"] = sys.argv[1]
json.dump(d, open(cfg, "w"), indent=2)
PY
        warn "ONE MORE PIECE, and it is in the IMAGE, not here: the arm base"
        warn "ships ro.hardware.egl=angle, which routes the guest's GL to"
        warn "SwiftShader (software) no matter what the host offers. It is a"
        warn "read-only property, so it cannot be changed on a running guest —"
        warn "the base needs rebuilding with ro.hardware.egl=mesa before the"
        warn "GPU actually gets used. Check with:"
        warn "  omnidroid start <name> && adb shell dumpsys SurfaceFlinger | grep GLES:"
        warn "  want 'Mesa, virgl'; 'ANGLE ... SwiftShader' means still software."
      fi
    fi
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
