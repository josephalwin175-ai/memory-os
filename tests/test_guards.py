import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_os import guards  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = {k: os.environ.get(k) for k in ("MOS_HOME", "MOS_DEDUP", "MOS_TRIM", "MOS_TRIM_CHARS")}
        os.environ["MOS_HOME"] = str(self.dir / "home")
        for key in ("MOS_DEDUP", "MOS_TRIM", "MOS_TRIM_CHARS"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class ReadGuardTest(Case):
    def read(self, path, **extra):
        payload = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(path)}}
        payload.update(extra)
        return guards.read_guard(payload)

    def big(self, name="a.py", body="x = 1\n" * 1000):
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_refuses_repeat_once_then_lets_it_through(self):
        path = self.big()
        self.assertIsNone(self.read(path))
        out = self.read(path)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("a.py", out["permissionDecisionReason"])
        self.assertIsNone(self.read(path))       # immediate retry goes through
        self.assertIsNotNone(self.read(path))    # and the cycle starts again

    def test_changed_file_is_read_again(self):
        path = self.big()
        self.assertIsNone(self.read(path))
        path.write_text("y = 2\n" * 1200, encoding="utf-8")
        self.assertIsNone(self.read(path))

    def test_different_range_subagent_small_and_missing_files_pass(self):
        path = self.big()
        self.assertIsNone(self.read(path))
        ranged = {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": str(path), "offset": 50}}
        self.assertIsNone(guards.read_guard(ranged))
        self.assertIsNone(self.read(path, agent_id="sub-1"))
        small = self.big("small.py", "x\n")
        self.assertIsNone(self.read(small))
        self.assertIsNone(self.read(small))
        self.assertIsNone(self.read(self.dir / "missing.py"))

    def test_forget_after_compaction_and_off_switch(self):
        path = self.big()
        self.assertIsNone(self.read(path))
        guards.forget_reads({"session_id": "s1"})
        self.assertIsNone(self.read(path))
        os.environ["MOS_DEDUP"] = "0"
        self.assertIsNone(self.read(path))

    def test_other_tools_ignored(self):
        self.assertIsNone(guards.read_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}}))


class TrimTest(Case):
    def payload(self, stdout, stderr=""):
        return {"session_id": "s1", "tool_name": "Bash",
                "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False,
                                  "noOutputExpected": False}}

    def test_short_output_untouched(self):
        self.assertIsNone(guards.trim_output(self.payload("ok\n" * 100)))

    def test_long_output_keeps_head_tail_errors_and_full_copy(self):
        lines = [f"line {i} compiling module" for i in range(3000)]
        lines[1500] = "ERROR: database connection refused"
        text = "\n".join(lines)
        out = guards.trim_output(self.payload(text))["hookSpecificOutput"]
        new = out["updatedToolOutput"]
        self.assertEqual(set(new), {"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"})
        self.assertLess(len(new["stdout"]), len(text) * 0.3)
        self.assertIn("line 0 compiling", new["stdout"])
        self.assertIn("line 2999 compiling", new["stdout"])
        self.assertIn("1501: ERROR: database connection refused", new["stdout"])
        saved = new["stdout"].split("Full output saved at ")[1].split(" ;")[0]
        self.assertEqual(Path(saved).read_text(encoding="utf-8"), text)

    def test_few_huge_lines_are_cut(self):
        text = "\n".join("A" * 9000 for _ in range(5))
        new = guards.trim_output(self.payload(text))["hookSpecificOutput"]["updatedToolOutput"]
        self.assertLess(len(new["stdout"]), 5000)

    def test_scratchpad_is_preferred_and_off_switch(self):
        pad = self.dir / "pad"
        pad.mkdir()
        payload = self.payload("row\n" * 20000)
        payload["scratchpad_dir"] = str(pad)
        self.assertIn(str(pad), guards.trim_output(payload)["hookSpecificOutput"]["updatedToolOutput"]["stdout"])
        os.environ["MOS_TRIM"] = "0"
        self.assertIsNone(guards.trim_output(payload))

    def test_unexpected_shapes_are_ignored(self):
        self.assertIsNone(guards.trim_output({"tool_name": "Bash", "tool_response": "plain string"}))
        self.assertIsNone(guards.trim_output({"tool_name": "Read", "tool_response": {"stdout": "x" * 99999}}))
        image = self.payload("x" * 99999)
        image["tool_response"]["isImage"] = True
        self.assertIsNone(guards.trim_output(image))


if __name__ == "__main__":
    unittest.main()
