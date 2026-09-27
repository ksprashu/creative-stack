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

# Build whisper.cpp from source and install `whisper-cli` into ~/.local/bin.
#
# Debian/Ubuntu package neither whisper.cpp nor a usable equivalent, and the
# binary is small and quick to build, so a source install is the honest answer
# rather than pulling in a 2GB PyTorch stack for transcription.
#
# CUDA is enabled automatically when nvcc is present — /vidkit transcription on
# a GPU box is roughly an order of magnitude faster.
set -euo pipefail

VERSION="${WHISPER_CPP_VERSION:-v1.7.4}"
PREFIX="${CREATIVE_STACK_BIN:-$HOME/.local/bin}"
SRC="${XDG_CACHE_HOME:-$HOME/.cache}/creative-stack/src/whisper.cpp"

echo "▸ whisper.cpp $VERSION -> $PREFIX/whisper-cli"

for tool in git cmake cc; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "❌ $tool is required to build whisper.cpp" >&2; exit 1; }
done

mkdir -p "$(dirname "$SRC")" "$PREFIX"

if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --depth 1 origin "$VERSION"
  git -C "$SRC" checkout --force FETCH_HEAD
else
  git clone --depth 1 --branch "$VERSION" \
    https://github.com/ggml-org/whisper.cpp.git "$SRC"
fi

CMAKE_FLAGS=(-DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF
             -DWHISPER_BUILD_EXAMPLES=ON)
if command -v nvcc >/dev/null 2>&1; then
  echo "    nvcc found — building with CUDA"
  CMAKE_FLAGS+=(-DGGML_CUDA=ON)
fi

JOBS="$(nproc 2>/dev/null || echo 4)"

build() {
  rm -rf "$SRC/build"
  cmake -S "$SRC" -B "$SRC/build" "$@" >/dev/null
  cmake --build "$SRC/build" --config Release -j "$JOBS"
}

# ggml's -DGGML_NATIVE=ON probes the CPU and synthesises an -mcpu string like
# `native+nodotprod+noi8mm+nosve`. GCC 13 on aarch64 rejects that spelling, so
# the stock Ubuntu 24.04 compiler cannot build the tuned path at all. Retrying
# without it costs some throughput and always compiles.
if ! build "${CMAKE_FLAGS[@]}"; then
  echo "    tuned build failed — retrying portable (-DGGML_NATIVE=OFF)" >&2
  build "${CMAKE_FLAGS[@]}" -DGGML_NATIVE=OFF
fi

# The binary moved around across releases; take whichever name exists.
BIN="$(find "$SRC/build" -type f \( -name whisper-cli -o -name main \) \
        -perm -u+x | head -1)"
[ -n "$BIN" ] || { echo "❌ build produced no whisper-cli binary" >&2; exit 1; }

install -m 755 "$BIN" "$PREFIX/whisper-cli"
echo "✓ $PREFIX/whisper-cli"
