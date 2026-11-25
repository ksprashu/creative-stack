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


"""Sprite recolouring regressions, using only the standard library."""
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills/vector/scripts"))
import sprite


class SpriteCurrentColorTest(unittest.TestCase):
    def symbol(self, inner, enabled=True, attributes=""):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.svg"
            path.write_text('<svg viewBox="0 0 24 24" ' + attributes + '>' + inner + '</svg>')
            symbol, problem = sprite.symbol_for(path, "icon-", enabled)
        self.assertIsNone(problem)
        return symbol, ET.fromstring(symbol)

    def test_unpainted_paths_inherit_currentcolor(self):
        symbol, node = self.symbol('<path d="M2 2h20v20H2z"/>')
        self.assertEqual(node.get("fill"), "currentColor")

    def test_inline_style_and_stylesheet_paints_are_recoloured(self):
        symbol, node = self.symbol(
            '<style>.a { fill: #123456; stroke: rgb(1, 2, 3) !important; }</style>'
            '<path style="fill:#1a73e8;stroke:RED!important;opacity:.5"/>')
        self.assertIn('fill: currentColor;', symbol)
        self.assertIn('stroke: currentColor !important;', symbol)
        self.assertIn('fill:currentColor;stroke:currentColor!important;opacity:.5', symbol)

    def test_none_urls_and_inheritance_values_survive(self):
        symbol, node = self.symbol(
            '<defs><linearGradient id="g"/></defs>'
            '<path fill="none" stroke="url(#g)" '
            'style="fill:none;stroke:url(#g);color:red;opacity:.4"/>'
            '<path fill="transparent" style="fill:inherit;stroke:currentColor"/>')
        self.assertIn('fill="none"', symbol)
        self.assertIn('stroke="url(#icon-sample-g)"', symbol)
        self.assertIn('fill:none;stroke:url(#icon-sample-g);color:red;opacity:.4', symbol)
        self.assertIn('fill="transparent"', symbol)
        self.assertIn('fill:inherit;stroke:currentColor', symbol)

    def test_root_paint_and_style_are_preserved(self):
        for attributes, fill, stroke, style in [
            ('fill="none" stroke="url(#g)"', 'none', 'url(#icon-sample-g)', None),
            ("fill='url(#g)' stroke='red'", 'url(#icon-sample-g)', 'currentColor', None),
            ('style="fill:none;stroke:red;opacity:.4"', 'currentColor', None,
             'fill:none;stroke:currentColor;opacity:.4'),
        ]:
            with self.subTest(attributes=attributes):
                symbol, node = self.symbol(
                    '<defs><linearGradient id="g"/></defs><path/>', attributes=attributes)
                self.assertEqual(node.get("fill"), fill)
                self.assertEqual(node.get("stroke"), stroke)
                self.assertEqual(node.get("style"), style)

    def test_only_paint_properties_are_rewritten(self):
        symbol, node = self.symbol(
            """<path fill='#f00' data-fill="red" style='--fill:red;fill:red;stroke:none;filter:url(#external)'/>""")
        self.assertEqual(node[0].get("fill"), "currentColor")
        self.assertEqual(node[0].get("data-fill"), "red")
        self.assertIn('--fill:red;fill:currentColor;stroke:none;filter:url(#external)', symbol)

    def test_disabled_keeps_original_paints(self):
        inner = '<path fill="red" style="fill:blue;stroke:green"/>'
        symbol, node = self.symbol(inner, enabled=False)
        self.assertIsNone(node.get("fill"))
        self.assertIn(inner, symbol)

    def test_cli_prints_fill_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "icon.svg"
            path.write_text('<svg viewBox="0 0 24 24"><path d="M0 0h1v1z"/></svg>')
            result = subprocess.run(
                [sys.executable, str(ROOT / "skills/vector/scripts/sprite.py"),
                 "-i", str(path), "-o", str(Path(tmp) / "sprite.svg"),
                 "--currentcolor", "--no-optimise"],
                capture_output=True, text=True, check=True)
            self.assertIn('fill: currentColor;', result.stdout)


if __name__ == "__main__":
    unittest.main()
