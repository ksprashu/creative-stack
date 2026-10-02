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
# Prove bootstrap.sh survives a genuinely clean Linux box.
#
#   bash scripts/verify_linux.sh              # arch + ubuntu, --headless
#   bash scripts/verify_linux.sh arch         # one distro
#   bash scripts/verify_linux.sh --skip-heavy # no LaTeX (~4GB and ~15 min less)
#   bash scripts/verify_linux.sh --keep       # leave the containers for triage
#   bash scripts/verify_linux.sh --demo       # also run `make demo` in each one
#
# Runs as a NON-ROOT user with passwordless sudo, because that is what an
# Omarchy or Ubuntu desktop actually looks like. Running the bootstrap as root
# would skip the entire $SUDO branch and the npm-prefix relocation, which is
# most of what there is to get wrong on Linux.
#
# Logs land in out/verify/<distro>.log and are the artifact worth reading.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOGS="$REPO_ROOT/out/verify"

DISTROS=()
BOOTSTRAP_ARGS=(--headless --yes)
KEEP=0
DEMO=0
for arg in "$@"; do
  case "$arg" in
    arch|ubuntu) DISTROS+=("$arg") ;;
    --keep)      KEEP=1 ;;
    --demo)      DEMO=1 ;;
    --skip-heavy) BOOTSTRAP_ARGS+=(--skip-heavy) ;;
    -h|--help)   sed -n '18,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "❌ Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
[ ${#DISTROS[@]} -eq 0 ] && DISTROS=(arch ubuntu)

# Docker Desktop, colima and podman all provide `docker`, and on a machine with
# more than one installed the name on PATH is not necessarily the live one.
DOCKER="${DOCKER:-docker}"
command -v "$DOCKER" >/dev/null 2>&1 || { echo "❌ no docker on PATH" >&2; exit 1; }
"$DOCKER" info >/dev/null 2>&1 || {
  echo "❌ $DOCKER cannot reach a daemon." >&2
  echo "   colima:  export DOCKER_HOST=unix://\$HOME/.colima/default/docker.sock" >&2
  exit 1; }

# The official archlinux image is amd64-only. Emulating it on Apple Silicon is
# both glacial and broken (pacman's alpm sandbox user cannot be entered under
# qemu), so on arm64 use Arch Linux ARM. It reports ID=archarm with ID_LIKE=arch,
# which is exactly the case bootstrap.sh's ID_LIKE matching exists to cover.
image_for() {
  if [ "$1" = ubuntu ]; then echo ubuntu:24.04
  elif [ "$(uname -m)" = arm64 ] || [ "$(uname -m)" = aarch64 ]; then
    echo menci/archlinuxarm:base-devel
  else echo archlinux:latest
  fi
}

# Enough to create a user and run sudo. Everything else is bootstrap's job —
# pre-installing more here would test a machine nobody has.
seed_for() {
  if [ "$1" = arch ]; then
    # pacman 7 sandboxes downloads with Landlock, which the container runtime's
    # seccomp profile blocks. This is an artefact of testing in a container, not
    # something bootstrap.sh should ever do on a real machine — hence it lives
    # here and not in pacman handling anywhere else in the repo.
    # -Syu before installing anything: the image's package db was frozen when
    # the image was built, and Arch mirrors keep only the current version of
    # each package, so `-Sy && install` asks for versions that were deleted
    # months ago and gets a 404 from every mirror.
    echo 'grep -q "^DisableSandbox" /etc/pacman.conf ||
            sed -i "/^\[options\]/a DisableSandbox" /etc/pacman.conf
          pacman -Syu --noconfirm >/dev/null
          pacman -S --noconfirm --needed sudo git base-devel >/dev/null'
  else
    echo 'apt-get update -qq &&
          apt-get install -y -qq sudo git ca-certificates >/dev/null'
  fi
}

mkdir -p "$LOGS"
pids=() names=()

for distro in "${DISTROS[@]}"; do
  name="cs-verify-$distro"
  log="$LOGS/$distro.log"
  names+=("$distro")
  echo "▸ $distro → $log"

  # Both result files, not just the log: a .rc left by the previous run is read
  # by the summary as this run's answer if anything stops us writing a new one.
  rm -f "$log.rc" "$log.demo.rc"
  "$DOCKER" rm -f "$name" >/dev/null 2>&1 || true
  "$DOCKER" run -d --name "$name" --security-opt seccomp=unconfined \
    "$(image_for "$distro")" sleep infinity >/dev/null

  # A seeding failure is this distro's problem, not the whole run's — the other
  # one still has something useful to tell us.
  #
  # Under `timeout` because this step can hang rather than fail: an Arch Linux
  # ARM mirror that accepts the connection and then never sends core.db leaves
  # pacman waiting forever, and seeding runs before the bootstraps are
  # backgrounded, so one wedged mirror blocks the whole run — including the
  # other distro, which has not even been created yet. 20 minutes is far past
  # honest slowness for four small packages.
  if ! timeout 1200 "$DOCKER" exec "$name" bash -ec "$(seed_for "$distro")
        id -u builder >/dev/null 2>&1 || useradd -m -s /bin/bash builder
        mkdir -p /etc/sudoers.d
        echo 'builder ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/creative-stack
        chmod 440 /etc/sudoers.d/creative-stack" >"$log" 2>&1; then
    echo "  ✗ $distro: container seeding failed — see $log"
    echo 1 > "$log.rc"
    continue
  fi

  # A clean checkout, not the host's working tree: .venv holds macOS wheels and
  # Mach-O binaries that would poison the Linux run in ways that look like bugs.
  # --no-xattrs: without it bsdtar ships com.apple.provenance on every file and
  # GNU tar inside the container prints a warning per file — 900 lines of noise
  # ahead of the output that matters.
  COPYFILE_DISABLE=1 tar -C "$REPO_ROOT" -cf - --no-xattrs \
      --exclude=.venv --exclude=node_modules --exclude=out \
      --exclude=__pycache__ --exclude=.git . \
    | "$DOCKER" exec -i -u builder "$name" tar -C /home/builder -xf - --one-top-level=creative-stack

  ( "$DOCKER" exec -u builder -e HOME=/home/builder -w /home/builder/creative-stack \
      "$name" bash scripts/bootstrap.sh "${BOOTSTRAP_ARGS[@]}" \
      >"$log" 2>&1; echo "$?" > "$log.rc" ) &
  pids+=("$!")
done

echo "▸ running ${#pids[@]} bootstrap(s) in parallel — this takes a while"
if [ ${#pids[@]} -gt 0 ]; then
  for pid in "${pids[@]}"; do wait "$pid" || true; done
fi

# `doctor` says every tool is present; `make demo` says they compose. Six
# artefacts out of one command is the difference between a box that installed
# cleanly and a box that can actually produce something.
#
# Its result goes in its own .demo.rc. Writing a demo failure into the
# bootstrap's .rc made the summary report "arch bootstrap exited 1" for a
# bootstrap that had exited 0, which points triage at the install log for an
# install that worked. Two questions, two answers.
if [ "$DEMO" -eq 1 ]; then
  echo "▸ running make demo in each container"
  for distro in "${names[@]}"; do
    [ "$(cat "$LOGS/$distro.log.rc" 2>/dev/null || echo 1)" = 0 ] || continue
    if "$DOCKER" exec -u builder -e HOME=/home/builder \
         -e PATH="/home/builder/.local/bin:/home/builder/.cargo/bin:/home/builder/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
         -w /home/builder/creative-stack "cs-verify-$distro" \
         make demo >>"$LOGS/$distro.log" 2>&1; then
      echo 0 > "$LOGS/$distro.log.demo.rc"
      printf '  \033[32m✓\033[0m %-8s make demo produced all six artefacts\n' "$distro"
    else
      echo 1 > "$LOGS/$distro.log.demo.rc"
      printf '  \033[31m✗\033[0m %-8s make demo failed — see %s\n' "$distro" "$LOGS/$distro.log"
    fi
  done
fi

echo
echo "======================================================================"
failed=0
for distro in "${names[@]}"; do
  rc="$(cat "$LOGS/$distro.log.rc" 2>/dev/null || echo '?')"
  if [ "$rc" = 0 ]; then
    printf '  \033[32m✓\033[0m %-8s bootstrap exited 0\n' "$distro"
  else
    printf '  \033[31m✗\033[0m %-8s bootstrap exited %s — see %s\n' \
      "$distro" "$rc" "$LOGS/$distro.log"
    failed=1
  fi
  if [ "$DEMO" -eq 1 ]; then
    case "$(cat "$LOGS/$distro.log.demo.rc" 2>/dev/null || echo skipped)" in
      0) printf '    \033[32m✓\033[0m %-6s make demo produced all six artefacts\n' "" ;;
      skipped) printf '    \033[2m-\033[0m %-6s make demo skipped (bootstrap failed)\n' "" ;;
      *) printf '    \033[31m✗\033[0m %-6s make demo failed\n' ""; failed=1 ;;
    esac
  fi
  # doctor.py is the acceptance test; bootstrap only warns when it fails, so
  # the exit code above is not on its own proof that the stack works.
  grep -E '^ *(✗|❌)' "$LOGS/$distro.log" | head -12 | sed 's/^/      /' || true
done
echo "======================================================================"

if [ "$KEEP" -eq 1 ]; then
  echo "Containers kept: $(printf 'cs-verify-%s ' "${names[@]}")"
  echo "  $DOCKER exec -it -u builder cs-verify-arch bash"
else
  for distro in "${names[@]}"; do "$DOCKER" rm -f "cs-verify-$distro" >/dev/null; done
  echo "Containers removed (--keep to triage inside them)."
fi

exit "$failed"
