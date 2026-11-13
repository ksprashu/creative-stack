"""Regression tests for MCP config safety and check-only behavior."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import install_mcp  # noqa: E402


class InstallMcpTest(unittest.TestCase):
    def test_malformed_config_is_preserved_and_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / ".cursor" / "mcp.json"
            malformed.parent.mkdir()
            original = b'{"mcpServers": { /* JSONC comment */ }}\n'
            malformed.write_bytes(original)

            valid = root / ".mcp.json"
            valid.write_text(json.dumps({
                "otherSetting": True,
                "mcpServers": {"existing": {"command": "existing"}},
            }))

            harnesses = {
                "cursor": {
                    "label": "Cursor",
                    "mcp": {"scope": "project", "path": ".cursor/mcp.json",
                            "format": "mcpServers"},
                },
                "claude-code": {
                    "label": "Claude Code",
                    "mcp": {"scope": "project", "path": ".mcp.json",
                            "format": "mcpServers"},
                },
            }
            servers = {"gimp": {"command": "uv", "args": ["run", "server.py"]}}
            output = io.StringIO()

            with (
                mock.patch.object(install_mcp, "REPO_ROOT", root),
                mock.patch.object(install_mcp, "load_harnesses", return_value=harnesses),
                mock.patch.object(install_mcp, "installed", return_value=True),
                mock.patch.object(install_mcp, "build_config", return_value=servers) as build,
                mock.patch.object(sys, "argv", ["install_mcp.py", "--skip-plugins"]),
                contextlib.redirect_stdout(output),
            ):
                result = install_mcp.main()

            self.assertEqual(result, 1)
            build.assert_called_once_with(check_only=False)
            self.assertEqual(malformed.read_bytes(), original)
            self.assertIn("malformed", output.getvalue())
            self.assertIn("mcpServers", output.getvalue())

            merged = json.loads(valid.read_text())
            self.assertTrue(merged["otherSetting"])
            self.assertIn("existing", merged["mcpServers"])
            self.assertEqual(merged["mcpServers"]["gimp"], servers["gimp"])

    def test_check_does_not_clone_or_write_config_for_missing_vendor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vendor = root / "vendor"
            harnesses = {
                "cursor": {
                    "label": "Cursor",
                    "mcp": {"scope": "project", "path": ".cursor/mcp.json",
                            "format": "mcpServers"},
                },
            }
            servers = {
                "gimp": {
                    "kind": "git", "repo": "owner/gimp-mcp", "sha": "abc123",
                    "entry": "server.py", "stars": "1", "needs": "GIMP",
                },
                "anot8": {
                    "kind": "npx", "package": "@owner/anot8@1.0.0",
                    "repo": "owner/anot8", "stars": "1", "needs": "Node",
                },
            }
            output = io.StringIO()

            with (
                mock.patch.object(install_mcp, "REPO_ROOT", root),
                mock.patch.object(install_mcp, "VENDOR", vendor),
                mock.patch.object(install_mcp, "SERVERS", servers),
                mock.patch.object(install_mcp, "load_harnesses", return_value=harnesses),
                mock.patch.object(install_mcp, "installed", return_value=True),
                mock.patch.object(install_mcp.shutil, "which", return_value="npx.exe"),
                mock.patch.object(install_mcp, "run", return_value=(1, "network disabled")) as run,
                mock.patch.object(sys, "argv", ["install_mcp.py", "--check", "--skip-plugins"]),
                contextlib.redirect_stdout(output),
            ):
                result = install_mcp.main()

            self.assertEqual(result, 0)
            self.assertFalse(vendor.exists())
            self.assertFalse((root / ".cursor" / "mcp.json").exists())
            run.assert_not_called()
            self.assertIn("not cloned", output.getvalue())

    def test_check_reports_existing_pin_without_checkout_or_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "vendor"
            checkout = vendor / "gimp"
            checkout.mkdir(parents=True)
            (checkout / "server.py").write_text("pass\n")
            sha = "a" * 40
            spec = {
                "kind": "git", "repo": "owner/gimp-mcp", "sha": sha[:12],
                "entry": "server.py", "stars": "1",
            }

            with (
                mock.patch.object(install_mcp, "VENDOR", vendor),
                mock.patch.object(install_mcp, "SERVERS", {"gimp": spec}),
                mock.patch.object(install_mcp, "run", return_value=(0, sha)) as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = install_mcp.build_config(check_only=True)

            self.assertEqual(result["gimp"]["args"],
                             ["run", "--directory", str(checkout), "server.py"])
            run.assert_called_once_with(["git", "-C", str(checkout), "rev-parse", "HEAD"])

    def test_check_does_not_repair_a_drifted_vendor_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "vendor"
            checkout = vendor / "gimp"
            checkout.mkdir(parents=True)
            (checkout / "server.py").write_text("pass\n")
            spec = {
                "kind": "git", "repo": "owner/gimp-mcp", "sha": "a" * 40,
                "entry": "server.py", "stars": "1",
            }

            with (
                mock.patch.object(install_mcp, "VENDOR", vendor),
                mock.patch.object(install_mcp, "SERVERS", {"gimp": spec}),
                mock.patch.object(install_mcp, "run", return_value=(0, "b" * 40)) as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = install_mcp.build_config(check_only=True)

            self.assertEqual(result, {})
            run.assert_called_once_with(["git", "-C", str(checkout), "rev-parse", "HEAD"])


if __name__ == "__main__":
    unittest.main()
