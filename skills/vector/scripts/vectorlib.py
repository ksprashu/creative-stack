#!/usr/bin/env python3
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

"""Shared plumbing for the /vector scripts.

Small on purpose: tool lookup, subprocess handling with a readable failure, and
the one piece of SVG knowledge every script here needs — that svgo will happily
delete the attribute the file depends on.
"""

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hostenv  # noqa: E402

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

VIEWBOX_RE = re.compile(r'\bviewBox\s*=\s*"([^"]+)"')
SVG_OPEN_RE = re.compile(r"<svg\b", re.I)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
CSS_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6})[\t\n\r\f ]?|\\([^\r\n\f])")
CSS_URL_RE = re.compile(r"url\(\s*(?:(['\"])(.*?)\1|([^)]*?))\s*\)", re.I | re.S)
SVG_STYLED_ATTRIBUTES = {
    "clip-path", "color-profile", "cursor", "fill", "filter", "marker-end",
    "marker-mid", "marker-start", "mask", "stroke", "style",
}


class _SVGTreeBuilder(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.stylesheets = []

    def doctype(self, name, pubid, system):
        raise ValueError("SVG DTD and entity declarations are not allowed")

    def pi(self, target, data):
        if target.lower() == "xml-stylesheet":
            self.stylesheets.append(data)


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def need(binary, package_id=None):
    """Resolve a binary or explain, in this machine's terms, how to get it."""
    found = hostenv.which(binary)
    if not found:
        die(f"{binary} not found. {hostenv.package_hint(package_id or binary)}")
    return found


def run(cmd, what, capture=False):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        die(f"{what} failed:\n    " + "\n    ".join(detail[-6:]))
    return result.stdout if capture else None


def magick(verb=None):
    cmd = hostenv.magick_cmd(verb)
    if not cmd:
        die("ImageMagick not found. "
            f"{hostenv.package_hint('imagemagick')}")
    return cmd


def svgo_optimise(src, dst, precision=3, required=False):
    """svgo `src` into `dst`, keeping the viewBox whatever svgo's version wants.

    `removeViewBox` sits in svgo 3's default preset and was taken out of
    svgo 4's, and the two disagree about the CLI flag that would turn it off.
    Rather than branch on a version string, run the default preset and put the
    attribute back if it went missing: an SVG without a viewBox cannot scale,
    which turns every icon in a `srcset` or a sprite into its intrinsic size or
    into nothing at all.

    Returns a "12KB → 8KB" string, or None when svgo is not installed and the
    caller said it was optional.
    """
    tool = hostenv.which("svgo")
    if not tool:
        if required:
            die(f"svgo not found. {hostenv.package_hint('svgo')}")
        return None

    original = src.read_text(errors="replace")
    before = len(original.encode())
    run([tool, "--quiet", "--precision", str(precision),
         str(src), "-o", str(dst)], "svgo")

    text = dst.read_text(errors="replace")
    if not VIEWBOX_RE.search(text):
        viewbox = VIEWBOX_RE.search(original)
        if viewbox:
            text = SVG_OPEN_RE.sub(f'<svg viewBox="{viewbox.group(1)}"',
                                   text, count=1)
            dst.write_text(text)

    after = dst.stat().st_size
    return f"{before // 1024 or 1}KB → {after // 1024 or 1}KB"


def svg_sources(inputs, recursive):
    """Resolve files, globs and directories to a sorted list of SVGs."""
    found = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            found += sorted(path.rglob("*.svg") if recursive
                            else path.glob("*.svg"))
        elif any(ch in item for ch in "*?["):
            found += sorted(Path().glob(item))
        elif path.is_file():
            found.append(path)
        else:
            die(f"no such file or directory: {item}")
    if not found:
        die("no SVG files matched the input")
    return found


def _css_unescape(match):
    if match.group(1):
        codepoint = int(match.group(1), 16)
        return chr(codepoint) if 0 < codepoint <= 0x10ffff else "\ufffd"
    return match.group(2)


def _fully_unquote(reference):
    while True:
        decoded = unquote(reference)
        if decoded == reference:
            return decoded
        reference = decoded


def validate_svg_resources(src):
    """Reject SVG resources that resolve outside the source directory."""
    root = Path(src).resolve().parent
    checked = set()

    def resolve_reference(reference, owner, base_dir=None):
        reference = reference.strip()
        if not reference or reference.startswith("#"):
            return None

        normalized = _fully_unquote(reference)
        parsed = urlsplit(normalized)
        scheme = parsed.scheme.lower()
        if scheme == "data":
            media_type = parsed.path.split(",", 1)[0].split(";", 1)[0].lower()
            if media_type in ("image/svg+xml", "application/xml", "text/xml"):
                raise ValueError(
                    f"Embedded SVG documents are not allowed ({owner.name})"
                )
            return None
        if scheme in ("http", "https"):
            return None
        if scheme or parsed.netloc:
            raise ValueError(
                f"SVG external resource schemes are not allowed in {owner.name}"
            )

        path_text = parsed.path
        windows_path = PureWindowsPath(path_text)
        if (path_text.startswith(("/", "\\")) or windows_path.is_absolute()
                or windows_path.drive):
            raise ValueError(
                f"SVG resources must stay inside the source directory ({owner.name})"
            )

        target = ((base_dir or owner.parent) / path_text.replace("\\", "/")).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"SVG resources must stay inside the source directory ({owner.name})"
            ) from error
        if target.suffix.lower() == ".svgz":
            raise ValueError(
                f"Compressed SVG resources are not supported ({owner.name})"
            )
        return target

    def resolve_base(reference, owner, base_dir):
        normalized = _fully_unquote(reference.strip())
        parsed = urlsplit(normalized)
        windows_path = PureWindowsPath(parsed.path)
        if (parsed.scheme or parsed.netloc or parsed.path.startswith(("/", "\\"))
                or windows_path.is_absolute() or windows_path.drive):
            raise ValueError(
                f"SVG base URIs must stay inside the source directory ({owner.name})"
            )
        target = (base_dir / parsed.path.replace("\\", "/")).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"SVG base URIs must stay inside the source directory ({owner.name})"
            ) from error
        if parsed.path.endswith(("/", "\\")) or target.is_dir():
            return target
        return target.parent

    checked_css = set()

    def validate_css_file(path):
        path = Path(path).resolve()
        if path not in checked_css:
            checked_css.add(path)
            validate_css(path.read_text(errors="replace"), path)

    def validate_css(css, owner, base_dir=None):
        css = CSS_COMMENT_RE.sub("", css or "")
        css = CSS_ESCAPE_RE.sub(_css_unescape, css)
        css = re.sub(r"\\(?:\r\n|[\r\n\f])", "", css)
        if re.search(r"@import\b", css, re.I):
            raise ValueError(
                f"External CSS imports are not allowed in SVG resources ({owner.name})"
            )
        for match in CSS_URL_RE.finditer(css):
            reference = match.group(2) if match.group(1) else match.group(3).strip()
            target = resolve_reference(reference, owner, base_dir)
            if target and target.suffix.lower() == ".svg" and target.is_file():
                validate_file(target)
            elif target and target.suffix.lower() == ".css" and target.is_file():
                validate_css_file(target)

    def validate_file(path):
        path = Path(path).resolve()
        if path in checked:
            return
        checked.add(path)

        data = path.read_bytes()
        builder = _SVGTreeBuilder()
        try:
            document = ET.fromstring(data, parser=ET.XMLParser(target=builder))
        except ET.ParseError as error:
            raise ValueError(f"Invalid SVG XML in {path.name}: {error}") from error

        for instruction in builder.stylesheets:
            href = re.search(r"\bhref\s*=\s*(['\"])(.*?)\1", instruction, re.I | re.S)
            if href:
                target = resolve_reference(href.group(2), path)
                if target and target.suffix.lower() == ".svg" and target.is_file():
                    validate_file(target)
                elif target and target.suffix.lower() == ".css" and target.is_file():
                    validate_css_file(target)

        stack = [(document, path.parent)]
        while stack:
            element, base_dir = stack.pop()
            tag = element.tag.rsplit("}", 1)[-1].lower()
            xml_base = element.attrib.get("{http://www.w3.org/XML/1998/namespace}base")
            if xml_base is not None:
                base_dir = resolve_base(xml_base, path, base_dir)
            for name, value in element.attrib.items():
                attribute = name.rsplit("}", 1)[-1].lower()
                if attribute in ("href", "src") and tag != "a":
                    target = resolve_reference(value, path, base_dir)
                    if target and target.suffix.lower() == ".svg" and target.is_file():
                        validate_file(target)
                    elif target and target.suffix.lower() == ".css" and target.is_file():
                        validate_css_file(target)
                elif attribute in SVG_STYLED_ATTRIBUTES:
                    validate_css(value, path, base_dir)
            if tag == "style":
                validate_css(element.text, path, base_dir)
            stack.extend((child, base_dir) for child in reversed(list(element)))

    validate_file(Path(src).resolve())
