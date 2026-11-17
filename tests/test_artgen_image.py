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

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "artgen" / "scripts"))

import image  # noqa: E402


class ImageVerificationTests(unittest.TestCase):
    def test_keeps_generated_image_when_verifier_backend_exits(self):
        payload = b"generated-image-bytes"
        stdout = io.StringIO()
        fake_types = SimpleNamespace(Part=SimpleNamespace(from_bytes=mock.Mock()))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "render.png"
            argv = ["image.py", "--prompt", "a blue bird", "--output", str(target)]
            with mock.patch.object(image.G, "backends"), \
                    mock.patch.object(image.G, "apply_brand", return_value="a blue bird"), \
                    mock.patch.object(image.G, "prepare", return_value=target), \
                    mock.patch.object(image.G, "load_genai", return_value=(None, fake_types)), \
                    mock.patch.object(image.G, "attempt", side_effect=SystemExit(1)) as attempt, \
                    mock.patch.object(image.G, "provenance"), \
                    mock.patch.object(image.G, "report"), \
                    mock.patch.object(image, "generate", return_value=(payload, "model", "vertex")), \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    result = image.main()
                except SystemExit as exc:
                    result = exc

            self.assertEqual(result, 0)
            self.assertEqual(target.read_bytes(), payload)
            self.assertIn("unverified", stdout.getvalue().lower())
            attempt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
