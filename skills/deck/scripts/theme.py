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

"""Generate a Marp theme from brand/tokens.json.

The theme is derived, never hand-maintained: change a colour in tokens.json and
the deck, the screenshots and the video captions all move together.

    python3 theme.py --profile google            # -> .generated/creative-google.css
    python3 theme.py --profile personal --stdout

build.py calls this automatically whenever the theme is older than the tokens,
so you rarely run it directly.
"""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CREATIVE_STACK_ROOT",
                                Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import brand  # noqa: E402

THEME_DIR = REPO_ROOT / "templates/deck/.generated"

TEMPLATE = """/* @theme creative-{profile} */
/* Generated from brand/tokens.json by skills/deck/scripts/theme.py.
   Do not edit — edit the tokens and rebuild. */

@import 'default';

:root {{
  --bg: {bg};
  --surface: {surface};
  --surface-alt: {surface_alt};
  --border: {border};
  --text: {text};
  --muted: {muted};
  --accent: {accent};
  --brand-blue: {blue};
  --brand-red: {red};
  --brand-yellow: {yellow};
  --brand-green: {green};
  --gradient-hero: {gradient_hero};
  --gradient-subtle: {gradient_subtle};
}}

section {{
  width: {slide_w}px;
  height: {slide_h}px;
  padding: {pad}px {pad_x}px;
  background: var(--bg);
  color: var(--text);
  font-family: {font_body};
  font-size: {size_body}px;
  line-height: {line_height};
  letter-spacing: -0.005em;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
}}

h1, h2, h3 {{
  font-family: {font_display};
  color: var(--text);
  line-height: {tight};
  letter-spacing: -0.02em;
  margin: 0 0 {gap}px;
}}
h1 {{ font-size: {size_h1}px; font-weight: 700; }}
h2 {{ font-size: {size_h2}px; font-weight: 600; }}
h3 {{ font-size: {size_h3}px; font-weight: 600; color: var(--muted); }}

p, li {{ margin: 0 0 {gap_sm}px; }}
ul, ol {{ padding-left: 1.1em; }}
li::marker {{ color: var(--accent); }}
strong {{ color: var(--accent); font-weight: 600; }}
em {{ color: var(--muted); font-style: normal; }}
a {{ color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--border); }}

code {{
  font-family: {font_mono};
  font-size: 0.85em;
  background: var(--surface-alt);
  border-radius: {radius_sm}px;
  padding: 0.12em 0.36em;
}}
pre {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: {radius_md}px;
  padding: {gap}px {gap}px;
  font-size: {size_code}px;
  line-height: 1.45;
  overflow: hidden;   /* a slide that scrolls is a slide with too much on it */
}}
pre code {{ background: none; padding: 0; }}

blockquote {{
  border-left: 4px solid var(--accent);
  margin: 0 0 {gap}px;
  padding-left: {gap}px;
  color: var(--muted);
}}

table {{ border-collapse: collapse; font-size: 0.9em; }}
th, td {{ border-bottom: 1px solid var(--border); padding: 0.5em 0.9em; text-align: left; }}
th {{ color: var(--muted); font-weight: 600; }}

img {{ border-radius: {radius_md}px; }}
section img[alt~="shadow"] {{ box-shadow: {shadow}; }}

/* --- title slide: `<!-- _class: lead -->` ------------------------------- */
section.lead {{
  background: var(--gradient-hero);
  color: #FFFFFF;
  justify-content: center;
  align-items: flex-start;
}}
section.lead h1 {{ font-size: {size_hero}px; color: #FFFFFF; max-width: 18ch; }}
section.lead h2,
section.lead p {{ color: rgba(255, 255, 255, .88); font-size: {size_h3}px; }}
section.lead strong,
section.lead a {{ color: #FFFFFF; }}
/* Muted grey is unreadable on the gradient — the chrome has to lift too. */
section.lead::after,
section.lead footer,
section.lead header {{ color: rgba(255, 255, 255, .72); }}

/* --- chapter divider: `<!-- _class: section -->` ------------------------ */
section.section {{
  background: var(--surface);
  justify-content: center;
}}
section.section h1 {{
  font-size: {size_h1}px;
  border-left: 8px solid var(--accent);
  padding-left: {gap}px;
}}

/* --- statement slide: `<!-- _class: quote -->` -------------------------- */
section.quote {{ justify-content: center; }}
section.quote h1,
section.quote p {{ font-size: {size_h2}px; max-width: 24ch; font-weight: 600; }}

/* --- two columns: `<!-- _class: split -->` then a `---` inside the slide */
section.split {{ display: grid; grid-template-columns: 1fr 1fr; gap: {gap_lg}px;
                 align-content: start; }}
section.split h1, section.split h2 {{ grid-column: 1 / -1; }}

/* --- chrome ------------------------------------------------------------- */
section::after {{
  color: var(--muted);
  font-size: {size_footer}px;
  right: {pad_x}px;
  bottom: {gap}px;
}}
footer {{ color: var(--muted); font-size: {size_footer}px; left: {pad_x}px;
          bottom: {gap}px; }}
header {{ color: var(--muted); font-size: {size_footer}px; left: {pad_x}px;
          top: {gap}px; }}

/* --- four brand accents, for `<!-- _class: blue|red|yellow|green -->` --- */
section.blue   {{ --accent: var(--brand-blue); }}
section.red    {{ --accent: var(--brand-red); }}
section.yellow {{ --accent: var(--brand-yellow); }}
section.green  {{ --accent: var(--brand-green); }}

/* --- dark deck: set `class: dark` in the front matter ------------------- */
section.dark {{
  --bg: {dark_bg};
  --surface: {dark_surface};
  --surface-alt: {dark_surface_alt};
  --border: {dark_border};
  --text: {dark_text};
  --muted: {dark_muted};
  --accent: {dark_accent};
}}
"""


def build(profile=None):
    tokens = brand.load(profile)
    light, dark = tokens["color"]["light"], tokens["color"]["dark"]
    scale = tokens["type"]["scale"]
    space = tokens["space"]["scale"]
    width, height = 1280, 720

    return TEMPLATE.format(
        profile=tokens["name"],
        bg=light["bg"], surface=light["surface"], surface_alt=light["surfaceAlt"],
        border=light["border"], text=light["text"], muted=light["textMuted"],
        accent=light["accent"],
        dark_bg=dark["bg"], dark_surface=dark["surface"],
        dark_surface_alt=dark["surfaceAlt"], dark_border=dark["border"],
        dark_text=dark["text"], dark_muted=dark["textMuted"],
        dark_accent=dark["accent"],
        blue=tokens["color"]["brand"]["blue"], red=tokens["color"]["brand"]["red"],
        yellow=tokens["color"]["brand"]["yellow"],
        green=tokens["color"]["brand"]["green"],
        gradient_hero=tokens["gradient"]["hero"],
        gradient_subtle=tokens["gradient"]["subtle"],
        font_body=tokens["font"]["body"], font_display=tokens["font"]["display"],
        font_mono=tokens["font"]["mono"],
        # 1280x720 is half of 1080p, so token sizes authored for video read
        # one step small on a slide; step up the scale rather than inventing
        # a second set of numbers.
        size_body=scale[4], size_h1=scale[6], size_h2=scale[5], size_h3=scale[4],
        size_hero=scale[7], size_code=scale[2], size_footer=scale[0],
        line_height=tokens["type"]["lineHeight"], tight=tokens["type"]["tightLineHeight"],
        gap=space[3], gap_sm=space[1], gap_lg=space[4],
        pad=space[5], pad_x=space[6],
        radius_sm=tokens["radius"]["sm"], radius_md=tokens["radius"]["md"],
        shadow=tokens["shadow"]["lg"],
        slide_w=width, slide_h=height,
    )


def ensure(profile=None):
    """Write the theme if tokens.json is newer. Returns (theme_dir, css_path)."""
    tokens_path = REPO_ROOT / "brand/tokens.json"
    local_path = REPO_ROOT / "brand/tokens.local.json"
    name = brand.load(profile)["name"]
    css = THEME_DIR / f"creative-{name}.css"

    newest = max([p.stat().st_mtime for p in (tokens_path, local_path,
                                              Path(__file__)) if p.exists()])
    if css.exists() and css.stat().st_mtime >= newest:
        return THEME_DIR, css

    THEME_DIR.mkdir(parents=True, exist_ok=True)
    css.write_text(build(profile))
    return THEME_DIR, css


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    if args.stdout:
        print(build(args.profile))
        return 0

    _, css = ensure(args.profile)
    css.write_text(build(args.profile))       # --force semantics when run directly
    print(f"✓ {css}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
