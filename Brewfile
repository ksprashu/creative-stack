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
brew "python@3.13"
# /vidkit checks caption fonts with fc-match
brew "fontconfig"

# ---------------------------------- raster ----------------------------------
# swiss-army raster: convert, composite, montage
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
brew "plantuml"

# -------------------------------- documents ---------------------------------
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

# ---------------------------------- video -----------------------------------
brew "ffmpeg"
# Homebrew's ffmpeg has no libass/freetype/fontconfig, so ass, subtitles and
# drawtext are missing. ffmpeg-full has them and is keg-only, so it does not
# shadow the stock binary. Linux distro ffmpeg already includes them.
brew "ffmpeg-full"
# encode ladders / device presets
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
# asciinema cast -> GIF
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
cask "supercollider"
cask "sonic-pi"

# ---------------------------------- fonts -----------------------------------
cask "font-roboto"
cask "font-roboto-mono"
cask "font-inter"
cask "font-jetbrains-mono"
cask "font-google-sans-code"
