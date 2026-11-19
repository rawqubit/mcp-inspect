"""End-to-end tests: run the real CLI against the real fixture server."""

import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "mcp_inspect.py"
FIXTURE = ROOT / "tests" / "fixture_server.py"


def fixture_command(*args):
    """A quoted command line the CLI can shlex-split back into argv."""
    return " ".join(
        shlex.quote(part) for part in [sys.executable, str(FIXTURE)] + list(args)
    )


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(CLI)] + [str(arg) for arg in args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=120,
    )


class FixtureServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.human = run_cli("--no-color", fixture_command())
        cls.json_run = run_cli("--json", fixture_command())
        cls.report = json.loads(cls.json_run.stdout) if cls.json_run.stdout else {}
        cls.tools = {tool["name"]: tool for tool in cls.report.get("tools", [])}

    def test_handshake_reports_six_tools(self):
        self.assertEqual(self.human.returncode, 0, self.human.stderr)
        self.assertIn("6 tools", self.human.stdout)
        self.assertIn("fixture-server", self.human.stdout)
        self.assertIn("2024-11-05", self.human.stdout)
        self.assertEqual(len(self.tools), 6)

    def test_total_tokens_and_largest_tool(self):
        self.assertEqual(self.json_run.returncode, 0, self.json_run.stderr)
        self.assertGreater(self.report["totalTokens"], 500)
        self.assertEqual(self.tools[self.report["tools"][0]["name"]]["name"],
                         "bloated_report_generator")
        tokens = [tool["tokens"] for tool in self.report["tools"]]
        self.assertEqual(tokens, sorted(tokens, reverse=True))
        self.assertGreater(self.tools["bloated_report_generator"]["tokens"], 800)

    def test_json_shape(self):
        self.assertIsInstance(self.tools, dict)
        self.assertEqual(self.report["server"], "fixture-server")
        self.assertEqual(self.report["protocolVersion"], "2024-11-05")
        self.assertIsInstance(self.report["flags"], dict)
        self.assertIn("NO-DESC", self.report["flags"])
        for tool in self.report["tools"]:
            self.assertEqual(
                set(tool),
                {"name", "description", "tokens", "flags", "parameterCount"},
            )
            self.assertIsInstance(tool["tokens"], int)
            self.assertIsInstance(tool["parameterCount"], int)

    def test_max_tokens_gate_exits_two(self):
        result = run_cli("--max-tokens", "1", fixture_command())
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeds --max-tokens 1", result.stderr)

    def test_max_tools_gate_exits_two(self):
        result = run_cli("--max-tools", "1", fixture_command())
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeds --max-tools 1", result.stderr)

    def test_missing_command_exits_three_without_traceback(self):
        result = run_cli("mcp-inspect-no-such-command-3f9a")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(result.stderr.strip())
        self.assertIn("no such file or directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_json_rpc_error_exits_three(self):
        result = run_cli(fixture_command("--broken"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("JSON-RPC error", result.stderr)
        self.assertIn("tools/list", result.stderr)
        self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_flags_land_on_the_right_tools(self):
        do_flags = self.tools["do"]["flags"]
        self.assertIn("NO-DESC", do_flags)
        self.assertIn("NO-PARAM-DESC", do_flags)
        self.assertIn("VAGUE-NAME", do_flags)

        self.assertIn("DUP-DESC", self.tools["read_file"]["flags"])
        self.assertNotIn("DUP-DESC", self.tools["write_file"]["flags"])

        self.assertIn("NO-PARAM-DESC", self.tools["list_directory"]["flags"])
        self.assertEqual(
            self.tools["read_file"]["description"],
            self.tools["write_file"]["description"],
        )

        bloat = self.tools["bloated_report_generator"]["flags"]
        self.assertIn("VERBOSE-DESC", bloat)
        self.assertIn("HUGE-SCHEMA", bloat)

        self.assertEqual(self.tools["search"]["flags"], [])

    def test_flag_map_matches_tools(self):
        flags = self.report["flags"]
        self.assertEqual(flags["NO-DESC"], ["do"])
        self.assertIn("read_file", flags["DUP-DESC"])
        self.assertIn("bloated_report_generator", flags["HUGE-SCHEMA"])

    def test_top_limits_the_table(self):
        result = run_cli("--no-color", "--top", "1", fixture_command())
        self.assertEqual(result.returncode, 0)
        self.assertIn("bloated_report_generator", result.stdout)
        self.assertNotIn("list_directory", result.stdout)
        self.assertIn("showing 1 of 6 tools", result.stdout)

    def test_no_ansi_codes_when_stdout_is_not_a_tty(self):
        self.assertNotIn("\x1b[", self.human.stdout)
        coloured = run_cli(fixture_command())
        self.assertNotIn("\x1b[", coloured.stdout)


if __name__ == "__main__":
    unittest.main()
