"""Tests for the discourse_explorer.cli subcommand dispatcher.

Run via:
    uv run python -m unittest tests.test_cli
"""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class HelpAndUnknown(unittest.TestCase):
    def test_no_args_prints_usage(self) -> None:
        from discourse_explorer.cli import main
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["discourse-explorer"]), \
             mock.patch.object(sys, "stdout", buf):
            main()
        out = buf.getvalue()
        self.assertIn("Usage:", out)
        for cmd in ("scrape", "discover-types", "stats", "query", "visualize"):
            self.assertIn(cmd, out)
        self.assertIn("viz", out)  # alias listed

    def test_dash_h_prints_usage(self) -> None:
        from discourse_explorer.cli import main
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["discourse-explorer", "--help"]), \
             mock.patch.object(sys, "stdout", buf):
            main()
        self.assertIn("Usage:", buf.getvalue())

    def test_unknown_command_exits_2(self) -> None:
        from discourse_explorer.cli import main
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["discourse-explorer", "bogus"]), \
             mock.patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unknown command", err.getvalue().lower())


class Routing(unittest.TestCase):
    """Each known command and the `viz` alias must dispatch to the right
    module's main() with sys.argv reshaped so the inner argparse sees a
    sensible prog name."""

    EXPECTED = {
        "scrape":         "discourse_explorer.scraper",
        "discover-types": "discourse_explorer.discover_types",
        "stats":          "discourse_explorer.stats",
        "query":          "discourse_explorer.query",
        "visualize":      "discourse_explorer.visualize",
    }

    def _assert_dispatches(self, invoked: str, canonical: str) -> None:
        from discourse_explorer import cli
        target_module_path = self.EXPECTED[canonical]

        # Capture what the inner main sees for sys.argv at call time.
        captured: dict[str, list[str]] = {}

        def fake_main():
            captured["argv"] = list(sys.argv)

        fake_module = type("M", (), {"main": staticmethod(fake_main)})()

        original_import = cli.import_module

        def import_only_target(name: str):
            self.assertEqual(name, target_module_path,
                             f"dispatch routed to {name!r}, expected {target_module_path!r}")
            return fake_module

        with mock.patch.object(sys, "argv",
                               ["discourse-explorer", invoked, "--foo", "bar"]), \
             mock.patch.object(cli, "import_module", side_effect=import_only_target):
            cli.main()

        # argv[0] reshaped to "discourse-explorer <canonical>", remaining args preserved
        self.assertEqual(captured["argv"], [f"discourse-explorer {canonical}", "--foo", "bar"])

    def test_each_known_command_routes(self) -> None:
        for canonical in self.EXPECTED:
            with self.subTest(command=canonical):
                self._assert_dispatches(canonical, canonical)

    def test_viz_alias_routes_to_visualize(self) -> None:
        self._assert_dispatches("viz", "visualize")


if __name__ == "__main__":
    unittest.main()
