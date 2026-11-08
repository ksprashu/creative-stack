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
# Creative Stack — Homebrew manifest
#
#   GENERATED from packages/manifest.json. Do not edit.
#   Regenerate:  python3 scripts/packages.py --emit brewfile
#
# This file is the macOS *projection* of the cross-platform manifest, kept in
# the repo so `brew bundle` works on its own. Arch and Debian are resolved from
# the same manifest at install time by scripts/packages.py.
# ==============================================================================

tap "charmbracelet/tap"


# ----------------------------------- base -----------------------------------
# Toolchains the rest of the manifest is installed with.
brew "git"
brew "curl"
brew "unzip"
# whisper.cpp source build on Debian
brew "cmake"
brew "pkg-config"
brew "node"
# the headers live in source-build-deps, not here — `provides: python3` is
# already satisfied on every machine that can run this script, so this entry
# resolves to `have` and its package list is never installed.
brew "python@3.13"
# /vidkit checks caption fonts with fc-match
brew "fontconfig"

# ---------------------------------- raster ----------------------------------
# swiss-army raster. Ubuntu 24.04 still ships ImageMagick 6, whose binary is
# `convert`, not `magick` — hostenv.magick_cmd() papers over the difference.
brew "imagemagick"
# fast large-image pipeline
brew "vips"
# HEIC/AVIF decode (iPhone screenshots)
brew "libheif"
# metadata read/strip before publishing
brew "exiftool"
# high-quality GIF encoder
brew "gifski"
# lossless PNG recompression
brew "oxipng"
# lossy PNG quantization
brew "pngquant"
# Arch splits the cwebp/dwebp binaries out of the library into libwebp-utils
brew "webp"

# ---------------------------------- vector ----------------------------------
# SVG -> PNG/PDF
brew "librsvg"
# strict SVG renderer, second opinion vs rsvg
brew "resvg"
# raster -> vector tracing
brew "potrace"
# SVG minifier
brew "svgo"
# font inspection / subsetting
brew "fontforge"

# --------------------------------- diagrams ---------------------------------
brew "graphviz"
# declarative diagrams, good defaults
brew "d2"
# one of /vector's four diagram engines, and the only one that needs a JVM.
# Absent on Arch Linux ARM: it lives in x86_64 extra, so ALARM does not
# build it and the AUR does not shadow an official package. Left as a
# warning rather than fallen back to the upstream jar, because d2, graphviz
# and mermaid cover the same ground without adding Java to the stack.
brew "plantuml"

# -------------------------------- documents ---------------------------------
# hard-required by /deck and the document skills. pandoc is Haskell, so the
# smaller distro ports do not carry it — Arch Linux ARM has neither pandoc-
# cli nor pandoc-bin (x86_64 only). The fallback installs upstream's static
# release tarball, which is dependency-free on every Linux.
brew "pandoc"
brew "ghostscript"
brew "poppler"
brew "qpdf"
brew "mupdf-tools"
# modern LaTeX alternative, fast + scriptable
brew "typst"
# self-contained LaTeX engine
brew "tectonic"
# only needed for manim Tex()/MathTex(). ~2-5GB.
cask "mactex-no-gui"

# ---------------------------------- slides ----------------------------------
# markdown -> deck (PPTX/PDF/HTML)
brew "marp-cli"
# terminal-native presentations
brew "presenterm"
# marp --pptx-editable converts through soffice; without it PPTX export is
# one image per slide
cask "libreoffice"

# ---------------------------------- video -----------------------------------
brew "ffmpeg"
# Homebrew's ffmpeg has no libass/freetype/fontconfig, so ass, subtitles and
# drawtext are missing. ffmpeg-full has them and is keg-only, so it does not
# shadow the stock binary. Linux distro ffmpeg already includes them.
brew "ffmpeg-full"
# encode ladders / device presets. Convenience over ffmpeg, never the only
# route — /vidkit encodes with ffmpeg. Absent on Arch Linux ARM (x86_64
# extra only), which is a warning and nothing more.
brew "handbrake"
# chapter + track surgery without re-encode
brew "mkvtoolnix"
# pull reference footage
brew "yt-dlp"
# silence / jump-cut detection
brew "auto-editor"

# --------------------------------- capture ----------------------------------
# scripted terminal recordings (.tape -> GIF/MP4)
brew "vhs"
# vhs needs ttyd to drive a terminal
brew "ttyd"
brew "asciinema"
# asciinema cast -> GIF. Installed from the repository, not crates.io: the
# `agg` crate name is taken by an unrelated library, and `cargo install agg`
# fails with "nothing to install ... it has no binaries".
brew "agg"
# code -> PNG
brew "silicon"
brew "termshot"
# code/terminal -> SVG or PNG
brew "charmbracelet/tap/freeze"
# on-screen keystrokes for tutorials
cask "keycastr"

# ---------------------------------- audio -----------------------------------
brew "sox"
# time-stretch / pitch-shift preserving formants
brew "rubberband"
brew "lame"
brew "flac"
brew "libsndfile"
# needed to build python-rtmidi / sounddevice
brew "portaudio"
# MIDI -> audio via SoundFont
brew "fluid-synth"
# score engraving
brew "lilypond"
# programmatic synthesis
brew "csound"
# CLI to list and switch CoreAudio / Loopback / BlackHole input & output
# devices on macOS
brew "switchaudio-osx"
# open-source 2-channel CoreAudio HAL virtual loopback driver on macOS
cask "blackhole-2ch"

# ------------------------------ speech-vision -------------------------------
# local transcription; Metal on macOS, CPU/CUDA on Linux
brew "whisper.cpp"
# OCR for screenshot text extraction
brew "tesseract"

# -------------------------------- animation ---------------------------------
# programmatic explanatory animation
brew "manim"

# ---------------------------------- utils -----------------------------------
brew "jq"
brew "gh"
# Debian installs the binary as batcat, hence two provides
brew "bat"
# terminal image preview — verify renders over SSH
brew "chafa"

# ----------------------------------- apps -----------------------------------
# Desktop creative apps. On macOS these are casks and mostly already
# installed (bootstrap adopts them); on Linux they are ordinary packages,
# which is why a Linux box bootstraps cleanly from scratch.
cask "blender"
cask "inkscape"
cask "gimp"
cask "obs"
cask "audacity"
# DAW with ReaScript — most agent-scriptable
cask "reaper"
# Rogue Amoeba node-graph audio recorder with JavaScript & Shortcuts
# automation on macOS
cask "audio-hijack"
# Rogue Amoeba virtual CoreAudio passthrough devices on macOS (ARK.driver)
cask "loopback"
cask "supercollider"
cask "sonic-pi"

# ---------------------------------- fonts -----------------------------------
cask "font-roboto"
# Debian's fonts-roboto is a 2017 snapshot of the proportional family only —
# it installs cleanly and leaves no Roboto Mono, which fc-list then reports
# as missing. Upstream's repo carries the built ttf/otf on its default
# branch.
cask "font-roboto-mono"
cask "font-inter"
cask "font-jetbrains-mono"
cask "font-google-sans-code"
