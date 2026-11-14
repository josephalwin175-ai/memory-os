"""End-to-end smoke test: every command, hook event and MCP tool, run as a real subprocess.

The point is breadth, not depth. Each call must exit with the expected code
and must never print a traceback, in a project with memories and in an empty
folder with none.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOS = [sys.executable, str(REPO / "bin" / "mos.py")]


class SmokeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.env = {**os.environ, "MOS_HOME": str(Path(self.tmp.name) / "home"), "PYTHONIOENCODING": "utf-8"}
        for key in ("MOS_ROTATE_AT", "MOS_DEDUP", "MOS_TRIM", "MOS_GUARDS", "MOS_EXTRACT_CMD", "CLAUDE_PROJECT_DIR"):
            self.env.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_mos(self, *args, stdin="", code=0, cwd=None):
        done = subprocess.run(MOS + list(args), input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=cwd or self.root, env=self.env, timeout=120)
        label = "mos " + " ".join(args)
        self.assertNotIn("Traceback", done.stderr, label)
        self.assertNotIn("Traceback", done.stdout, label)
        if code is not None:
            self.assertEqual(done.returncode, code, f"{label}\n{done.stdout}\n{done.stderr}")
        return done.stdout

    def transcript(self):
        path = Path(self.tmp.name) / "session.jsonl"
        big = self.root / "big.py"
        rows = [
            {"type": "user", "message": {"content": "Let's use PostgreSQL instead of MongoDB for storage."}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {
                "id": "m1", "usage": {"input_tokens": 9, "cache_creation_input_tokens": 500,
                                      "cache_read_input_tokens": 150000, "output_tokens": 40},
                "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(big)}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path


class EmptyProjectTest(SmokeCase):
    def test_every_read_command_is_safe_without_memory(self):
        for args in (["list"], ["search", "anything"], ["brief"], ["brief", "a task"], ["verify"], ["inbox"],
                     ["ledger"], ["export"], ["export", "--format", "json"], ["map"], ["map", "query"],
                     ["map", "--around", "nope.py"], ["graph"], ["graph", "--json"], ["accept", "--all"],
                     ["reject", "--all"], ["--version"], ["--help"]):
            self.run_mos(*args)
        self.run_mos("get", "missing", code=1)
        self.run_mos("promote", "missing", "--edit", "x/**", code=1)
        self.run_mos("status", "missing", "archived", code=2)
        self.run_mos("accept", "missing", code=2)
        self.run_mos("import", "no-such-file.md", code=2)
        self.run_mos("save", "", code=2)
        self.run_mos("save", "text here", "--check", "bogus: x", code=2)
        self.run_mos("promote", "x", "--bash", "(unclosed", code=1)
        self.assertFalse((self.root / ".memory").exists(), "read commands must not create .memory")

    def test_hooks_never_fail_or_print_on_bad_input(self):
        for event in ("session-start", "prompt", "capture", "pre-tool", "post-tool"):
            for stdin in ("", "not json", "[]", "{}", '{"tool_name": "Read"}', '{"transcript_path": "nope.jsonl"}'):
                out = self.run_mos("hook", event, stdin=stdin)
                self.assertEqual(out.strip(), "", f"{event} with {stdin!r}")


class FullProjectTest(SmokeCase):
    def test_whole_workflow(self):
        root = self.root
        (root / "package.json").write_text('{"dependencies": {"pg": "8"}}', encoding="utf-8")
        (root / "big.py").write_text("def handler():\n    return 1\n" * 400, encoding="utf-8")
        (root / "AGENTS.md").write_text("- Always use pnpm, never npm, for installs\n", encoding="utf-8")
        self.run_mos("init")
        self.run_mos("save", "Database is PostgreSQL\nChosen for joins.", "--type", "decision", "--id", "db",
                     "--check", "json_has: package.json :: dependencies.pg", "--pin")
        self.run_mos("save", "Never force push", "--type", "failure", "--id", "no-force")
        self.run_mos("promote", "no-force", "--bash", r"git\s+push\s+.*--force")
        self.assertIn("db", self.run_mos("list"))
        self.assertIn("db", self.run_mos("search", "database postgres"))
        self.assertIn("checks:", self.run_mos("get", "db"))
        self.assertIn("1 of 1", self.run_mos("verify"))
        self.assertIn("all 1 checked", self.run_mos("verify", "--markdown"))
        self.assertIn("[db]", self.run_mos("brief"))
        self.assertIn("receipt", self.run_mos("brief", "database work", "--receipt"))
        self.assertIn("def handler", self.run_mos("map"))
        self.assertIn("big.py", self.run_mos("map", "handler"))
        self.run_mos("map", "--around", "big.py")
        self.assertIn("new in the inbox", self.run_mos("import", "AGENTS.md"))
        self.assertIn("pnpm", self.run_mos("inbox"))
        self.assertIn("Accepted", self.run_mos("accept", "--all"))
        self.assertIn("## Decisions", self.run_mos("export"))
        out_file = root / "out.json"
        self.run_mos("export", "--format", "json", "--out", str(out_file))
        self.assertEqual(len(json.loads(out_file.read_text(encoding="utf-8"))["memories"]), 3)
        self.run_mos("status", "db", "archived")
        self.run_mos("status", "db", "active")

        # A check that now fails: plain verify reports it, exit code 1, --fix marks it stale.
        (root / "package.json").write_text("{}", encoding="utf-8")
        self.assertIn("STALE", self.run_mos("verify", code=1))
        self.assertIn("makes 1 saved project memory false", self.run_mos("verify", "--markdown", code=1))
        self.run_mos("verify", "--fix", code=1)
        self.assertIn("stale", self.run_mos("list"))

        # Hooks with realistic payloads.
        t = self.transcript()
        base = {"session_id": "smoke", "cwd": str(root), "transcript_path": str(t)}
        brief = self.run_mos("hook", "session-start", stdin=json.dumps({**base, "source": "startup"}))
        self.assertIn("Project memory", brief)
        nudge = json.loads(self.run_mos("hook", "prompt", stdin=json.dumps(base)))
        self.assertIn("151K", nudge["systemMessage"])  # the transcript is old, so this is the cold-resume form
        self.assertIn("handoff", nudge["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_mos("hook", "prompt", stdin=json.dumps(base)).strip(), "")
        read = {**base, "tool_name": "Read", "tool_input": {"file_path": str(root / "big.py")}}
        self.assertEqual(self.run_mos("hook", "pre-tool", stdin=json.dumps(read)).strip(), "")
        self.assertIn("deny", self.run_mos("hook", "pre-tool", stdin=json.dumps(read)))
        push = {**base, "tool_name": "Bash", "tool_input": {"command": "git push --force"}}
        self.assertIn("no-force", self.run_mos("hook", "pre-tool", stdin=json.dumps(push)))
        long_out = {**base, "tool_name": "Bash", "tool_response": {
            "stdout": "line\n" * 9000, "stderr": "", "interrupted": False, "isImage": False}}
        trimmed = json.loads(self.run_mos("hook", "post-tool", stdin=json.dumps(long_out)))
        self.assertIn("Full output saved at", trimmed["hookSpecificOutput"]["updatedToolOutput"]["stdout"])
        self.run_mos("hook", "capture", stdin=json.dumps({**base, "hook_event_name": "SessionEnd"}))
        self.assertTrue((root / ".memory" / "last-session.md").is_file())
        self.assertIn("PostgreSQL instead of MongoDB", self.run_mos("inbox"))
        self.run_mos("reject", "--all")
        self.assertIn("Inbox is empty", self.run_mos("inbox"))
        self.run_mos("capture", "--transcript", str(t))
        self.run_mos("extract", "--transcript", str(t))
        self.run_mos("extract", "--transcript", str(t), "--llm", code=2)  # no MOS_EXTRACT_CMD set
        self.assertIn("no-force", self.run_mos("ledger"))
        json.loads(self.run_mos("ledger", "--json"))
        self.assertIn("usage profile", self.run_mos("meter", "--dir", str(t.parent)))
        json.loads(self.run_mos("meter", "--dir", str(t.parent), "--json"))
        self.assertIn("No Claude Code transcripts", self.run_mos("meter", "--dir", str(root / "nowhere")))
        self.assertIn("recall of needed", self.run_mos("bench"))

    def test_mcp_server_every_tool(self):
        (self.root / "app.py").write_text("def start():\n    pass\n", encoding="utf-8")
        calls = [
            ("memory_save", {"text": "API listens on port 4001", "id": "port", "checks": ["file_exists: app.py"],
                             "guards": ["edit: app.py"]}),
            ("memory_search", {"query": "port"}),
            ("memory_get", {"id": "port"}),
            ("memory_get", {"id": "../../etc/passwd"}),
            ("context_compile", {"task": "change the port"}),
            ("code_map", {"query": "start"}),
            ("code_map", {"around": "app.py"}),
            ("code_map", {}),
            ("memory_verify", {}),
            ("memory_save", {"text": ""}),
            ("no_such_tool", {}),
        ]
        lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 "this line is not json",
                 json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})]
        lines += [json.dumps({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
                  for i, (name, arguments) in enumerate(calls)]
        out = self.run_mos("mcp", stdin="\n".join(lines) + "\n")
        replies = {r["id"]: r for r in map(json.loads, out.splitlines())}
        self.assertEqual(len(replies), 2 + len(calls))
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)
        text = lambda i: replies[10 + i]["result"]["content"][0]["text"]  # noqa: E731
        error = lambda i: replies[10 + i]["result"]["isError"]  # noqa: E731
        self.assertIn("Saved [port]", text(0))
        self.assertIn("4001", text(1))
        self.assertIn("guard: edit: app.py", text(2))
        self.assertIn("No memory", text(3))
        self.assertIn("Project memory", text(4))
        self.assertIn("def start", text(5))
        self.assertIn("[port]", text(6))
        self.assertIn("app.py", text(7))
        self.assertIn("1 of 1", text(8))
        self.assertTrue(error(9))
        self.assertTrue(error(10))
        self.assertFalse(any(error(i) for i in range(9)))


class PackagingTest(unittest.TestCase):
    def test_sources_parse_as_python_3_10(self):
        for path in list((REPO / "memory_os").glob("*.py")) + [REPO / "bin" / "mos.py", REPO / "scripts" / "build_skill.py"]:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))

    def test_manifests_are_consistent(self):
        from memory_os import __version__
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        hooks = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], __version__)
        self.assertIn(f'version = "{__version__}"', (REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", plugin["mcpServers"]["memory-os"]["args"][0])
        events = {"session-start", "prompt", "capture", "pre-tool", "post-tool"}
        used = {h["command"].rsplit(" ", 1)[-1] for groups in hooks["hooks"].values() for g in groups for h in g["hooks"]}
        self.assertEqual(used, events)
        for name in ("memory", "handoff", "meter"):
            head = (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
            self.assertIn(f"name: {name}", head)
            self.assertIn("description:", head)

    def test_standalone_skill_builds_and_runs(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import build_skill
        archive = build_skill.build()
        self.assertTrue(archive.is_file())
        pyz = REPO / "dist" / "memory-os" / "mos.pyz"
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, str(pyz), "bench", "--json"], capture_output=True, text=True,
                                  cwd=tmp, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["methods"]["compiled"]["wrong"], 0)


if __name__ == "__main__":
    unittest.main()
