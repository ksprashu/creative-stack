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

# Install (or update) a current Rust toolchain via rustup.
#
# Debian and Ubuntu package Rust, but on a frozen LTS it goes stale fast:
# Ubuntu 24.04 ships 1.75, and gifski, oxipng, typst and silicon all refuse to
# build on it. Installing the distro package would satisfy `command -v cargo`
# while leaving every cargo fallback broken, so on Debian we skip apt and come
# straight here. Arch keeps `rust` current and never reaches this script.
set -euo pipefail

MIN_MINOR="${RUST_MIN_MINOR:-82}"

cargo_minor() {
  command -v cargo >/dev/null 2>&1 || return 1
  cargo --version 2>/dev/null | awk '{split($2, v, "."); print v[2]}'
}

current="$(cargo_minor || echo 0)"
if [ "${current:-0}" -ge "$MIN_MINOR" ] 2>/dev/null; then
  echo "✓ cargo 1.$current already meets the 1.$MIN_MINOR minimum"
  exit 0
fi

if command -v rustup >/dev/null 2>&1; then
  echo "▸ rustup present but cargo is 1.${current:-?} — updating stable"
  rustup update stable
  rustup default stable
else
  echo "▸ installing rustup (cargo 1.${current:-none} < 1.$MIN_MINOR)"
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal
fi

export PATH="$HOME/.cargo/bin:$PATH"
new="$(cargo_minor || echo 0)"
[ "${new:-0}" -ge "$MIN_MINOR" ] 2>/dev/null || {
  echo "❌ cargo is still 1.${new:-none} after rustup" >&2; exit 1; }

echo "✓ $(cargo --version)"
echo "  add to your shell: export PATH=\"\$HOME/.cargo/bin:\$PATH\""
