#!/usr/bin/env bash
# Copyright 2026 Creative Stack Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ==============================================================================
# Creative Stack — one-shot bootstrap for macOS, Arch and Debian/Ubuntu
#
#   bash scripts/bootstrap.sh                  # full install
#   bash scripts/bootstrap.sh --headless       # no desktop apps (servers, CI)
#   bash scripts/bootstrap.sh --skip-heavy     # no LaTeX / Blender / Resolve
#   bash scripts/bootstrap.sh --skip-packages  # everything except the long run
#   bash scripts/bootstrap.sh --doctor         # verify only, change nothing
#   bash scripts/bootstrap.sh --yes            # never prompt (CI / cloud-init)
#
# Idempotent: safe to re-run. Every stage detects existing state first.
#
# Fully unattended on Linux requires passwordless sudo. Without it the script
# asks once up front and then keeps the timestamp alive, so the install still
# runs to completion without babysitting.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$REPO_ROOT/.venv"
export CREATIVE_STACK_ROOT="$REPO_ROOT"

PROFILE=full
SKIP_PACKAGES=0
SKIP_HEAVY=0
DOCTOR_ONLY=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --headless)      PROFILE=headless ;;
    --skip-packages|--skip-brew) SKIP_PACKAGES=1 ;;
    --skip-heavy)    SKIP_HEAVY=1 ;;
    --doctor)        DOCTOR_ONLY=1 ;;
    --yes|-y)        ASSUME_YES=1 ;;
    -h|--help)       sed -n '17,31p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "❌ Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------- platform detection ---
# Deliberately duplicated from scripts/hostenv.py in shell: this runs before
# python is guaranteed to exist. Everything after stage 0 uses hostenv.py.
FAMILY=unknown
if [ "$(uname -s)" = "Darwin" ]; then
  FAMILY=macos
elif [ -r /etc/os-release ]; then
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in
    *" arch "*|*" archlinux "*) FAMILY=arch ;;
    *" debian "*|*" ubuntu "*)  FAMILY=debian ;;
  esac
fi
if [ "$FAMILY" = unknown ]; then
  command -v pacman  >/dev/null 2>&1 && FAMILY=arch
  command -v apt-get >/dev/null 2>&1 && FAMILY=debian
fi
[ "$FAMILY" = unknown ] && die "Unsupported platform. Need macOS, Arch or Debian/Ubuntu."

echo "========================================================================"
echo " 🎬 CREATIVE STACK — AI-native content production toolchain"
echo "========================================================================"
echo " Repo:     $REPO_ROOT"
echo " Platform: $FAMILY  ($(uname -srm))"
echo " Profile:  $PROFILE"

# ------------------------------------------------------------------- sudo ---
SUDO=""
if [ "$FAMILY" != macos ] && [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "sudo not found and not running as root."
  SUDO=sudo
  if ! sudo -n true 2>/dev/null; then
    if [ "$ASSUME_YES" -eq 1 ]; then
      die "--yes requires passwordless sudo. Add to /etc/sudoers.d/creative-stack:
    $(id -un) ALL=(ALL) NOPASSWD: ALL"
    fi
    say "Elevating once — the rest of the install is unattended"
    sudo -v
  fi
  # Keep the sudo timestamp warm for the length of the install so a two-hour
  # package run does not stall on a password prompt an hour in.
  ( while true; do sudo -n true 2>/dev/null; sleep 50; done ) &
  SUDO_KEEPALIVE=$!
  trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT
fi

# --------------------------------------------------- 0. package manager etc ---
say "0/7 Package manager + language toolchains"

ensure_macos() {
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found — installing"
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL \
      https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    for prefix in /opt/homebrew /usr/local; do
      [ -x "$prefix/bin/brew" ] && eval "$("$prefix/bin/brew" shellenv)"
    done
  fi
  ok "brew $(brew --version | head -1)"
}

ensure_arch() {
  $SUDO pacman -Sy --noconfirm --needed base-devel git curl python go rust \
    >/dev/null || warn "pacman base install had warnings"
  ok "pacman ready"

  # A lot of creative tooling on Arch lives in the AUR. paru is the helper that
  # runs cleanly non-interactively; build it once from the AUR itself.
  if ! command -v paru >/dev/null 2>&1 && ! command -v yay >/dev/null 2>&1; then
    warn "no AUR helper — building paru-bin"
    tmp="$(mktemp -d)"
    if git clone --depth 1 https://aur.archlinux.org/paru-bin.git "$tmp/paru-bin" \
       && ( cd "$tmp/paru-bin" && makepkg -si --noconfirm --needed ); then
      ok "paru installed"
    else
      warn "paru build failed — AUR packages will fall back to cargo/go/pip"
    fi
    rm -rf "$tmp"
  else
    ok "AUR helper: $(command -v paru || command -v yay)"
  fi
}

ensure_debian() {
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq
  $SUDO apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates gnupg unzip cmake pkg-config \
    python3 python3-venv python3-pip software-properties-common
  ok "apt base packages"

  # Ubuntu LTS ships an ancient node; marp-cli and mermaid-cli need 18+.
  if ! command -v node >/dev/null 2>&1 || \
     [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 18 ]; then
    warn "node missing or too old — installing NodeSource 22.x"
    curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO -E bash -
    $SUDO apt-get install -y nodejs
  fi
  ok "node $(node -v)"

  # Several tools have no apt package and fall back to cargo/go. Note this
  # checks the cargo *version*, not just its presence: apt's rustc on 24.04 is
  # 1.75 and oxipng, gifski, typst and silicon all need 1.82+.
  bash "$SCRIPT_DIR/install/rustup.sh" || warn "rust toolchain setup failed"
  export PATH="$HOME/.cargo/bin:$PATH"
  command -v go >/dev/null 2>&1 || $SUDO apt-get install -y golang-go
  ok "cargo $(cargo --version 2>/dev/null | cut -d' ' -f2 || echo '?') + go ready"
}

ensure_"$FAMILY"

export PATH="$HOME/.cargo/bin:$HOME/go/bin:$HOME/.local/bin:$PATH"

command -v python3 >/dev/null 2>&1 || die "python3 still missing after stage 0"
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found — installing"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version 2>/dev/null || echo '?')"

if [ "$DOCTOR_ONLY" -eq 1 ]; then
  exec python3 "$SCRIPT_DIR/doctor.py"
fi

# -------------------------------------------------------------- 1. packages ---
if [ "$SKIP_PACKAGES" -eq 0 ]; then
  say "1/7 System packages (the long one — up to ~10GB with LaTeX and Blender)"
  PKG_ARGS=(--install --profile "$PROFILE")
  [ "$SKIP_HEAVY" -eq 1 ] && PKG_ARGS+=(--skip-heavy)
  python3 "$SCRIPT_DIR/packages.py" "${PKG_ARGS[@]}" || \
    warn "some packages did not install — doctor.py will list what is missing"
else
  say "1/7 System packages — skipped"
fi

# --------------------------------------------------------------- 2. python ---
say "2/7 Python environment"
[ -d "$VENV" ] || uv venv "$VENV" --python 3.13
uv pip install --python "$VENV/bin/python" -r "$REPO_ROOT/requirements.txt"

# Playwright needs its own OS libraries on Linux; --with-deps installs them.
if [ "$FAMILY" = macos ]; then
  "$VENV/bin/python" -m playwright install chromium
else
  "$VENV/bin/python" -m playwright install --with-deps chromium || \
    "$VENV/bin/python" -m playwright install chromium
fi
ok "python deps + chromium installed"

# ------------------------------------------------------------------ 3. npm ---
say "3/7 Node CLIs"
# Global npm installs need a writable prefix; on Linux the default is root-owned.
if [ "$FAMILY" != macos ] && [ "$(npm config get prefix)" = "/usr" ]; then
  npm config set prefix "$HOME/.local"
  warn "npm global prefix moved to ~/.local (was /usr)"
fi
npm install --global --silent @mermaid-js/mermaid-cli svgo @marp-team/marp-cli \
  || warn "npm global install had failures"
ok "node CLIs installed"

# -------------------------------------------------------------- 4. bridges ---
say "4/7 App bridges (headless entry points, soundfont, Resolve env)"
"$VENV/bin/python" "$SCRIPT_DIR/install_bridges.py"

# ------------------------------------------------------------------ 5. mcp ---
say "5/7 MCP servers + plugin marketplaces (opt-in, disabled by default)"
"$VENV/bin/python" "$SCRIPT_DIR/install_mcp.py"

# --------------------------------------------------------------- 6. skills ---
say "6/7 Skills"
"$VENV/bin/python" "$SCRIPT_DIR/validate_skills.py"
"$VENV/bin/python" "$SCRIPT_DIR/sync_skills.py" --fix --prune

# ---------------------------------------------------------------- 7. doctor ---
say "7/7 Verification"
"$VENV/bin/python" "$SCRIPT_DIR/doctor.py" || \
  warn "doctor reported problems — see the table above"

RC_FILE="$HOME/.zshrc"; [ "${SHELL##*/}" = bash ] && RC_FILE="$HOME/.bashrc"
echo ""
echo "========================================================================"
echo " ✅ CREATIVE STACK READY"
echo "========================================================================"
echo " Skills:  /shot /vidkit /deck /vector /raster /render3d /sound /artgen"
echo ""
echo " Next:"
echo "   1. source $REPO_ROOT/creative-stack.env    # add this to $RC_FILE"
echo "   2. Fill in brand/tokens.json with real values"
echo "   3. End-to-end smoke test:   make demo"
echo "   4. Enable a GUI MCP server: claude mcp enable blender"
echo "========================================================================"
