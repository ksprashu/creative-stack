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

# Install pandoc from its official GitHub release into ~/.local/bin.
#
# Only runs when no native package answered. pandoc is Haskell, which means it
# is a package the smaller distro ports simply do not carry: Arch Linux ARM has
# neither `pandoc-cli` nor any of its Haskell dependencies, and the AUR's
# `pandoc-bin` is x86_64-only. Since /deck and the document skills treat pandoc
# as hard-required, "no route on aarch64" is not an acceptable answer.
#
# Upstream publishes static linux-amd64 and linux-arm64 tarballs with no runtime
# dependencies at all, which is the one route that works on every Linux box.
set -euo pipefail

VERSION="${PANDOC_VERSION:-3.11}"
PREFIX="${CREATIVE_STACK_BIN:-$HOME/.local/bin}"

case "$(uname -s)" in
  Linux) ;;
  *) echo "❌ the pandoc release tarballs are Linux-only; use brew on macOS" >&2
     exit 1 ;;
esac

case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "❌ no pandoc release build for $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="pandoc-$VERSION-linux-$ARCH.tar.gz"
URL="https://github.com/jgm/pandoc/releases/download/$VERSION/$TARBALL"

echo "▸ pandoc $VERSION ($ARCH) -> $PREFIX/pandoc"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# curl over urllib/wget: it is present on every distro we target, follows the
# release redirect to objects.githubusercontent.com, and uses the OS trust store.
curl -fsSL --retry 3 -o "$tmp/$TARBALL" "$URL"
tar -C "$tmp" -xzf "$tmp/$TARBALL"

mkdir -p "$PREFIX"
installed=0
# The tarball ships pandoc alongside pandoc-lua and pandoc-server. Take them
# all: `pandoc --lua-filter` is how /deck applies brand tokens, and the sibling
# binaries are what a filter that shells out expects to find next to it.
for bin in "$tmp"/pandoc-"$VERSION"/bin/*; do
  [ -f "$bin" ] && [ -x "$bin" ] || continue
  install -m 755 "$bin" "$PREFIX/$(basename "$bin")"
  installed=$((installed + 1))
done

[ "$installed" -gt 0 ] || { echo "❌ $TARBALL contained no binaries" >&2; exit 1; }

# Data files (reference.docx, templates, the default reference.pptx) live beside
# the binary in the tarball. pandoc finds them via --data-dir or $XDG_DATA_HOME,
# and without them `-o deck.pptx` falls back to a bare unstyled reference.
share="$tmp/pandoc-$VERSION/share"
if [ -d "$share" ]; then
  target="${XDG_DATA_HOME:-$HOME/.local/share}/pandoc"
  mkdir -p "$target"
  cp -R "$share/." "$target/"
fi

echo "✓ $PREFIX/pandoc ($("$PREFIX/pandoc" --version | head -1))"
