import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_os import transcript  # noqa: E402
from memory_os.brief import compile_brief  # noqa: E402
from memory_os.store import Store  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def assistant(ctx, ts=T0, blocks=None, sidechain=False):
    return {"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"), "isSidechain": sidechain,
            "message": {"id": f"m{ctx}", "content": blocks or [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": ctx - 10, "output_tokens": 5}}}


def user(text):
    return {"type": "user", "message": {"content": text}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.file = Path(self.tmp.name) / "session.jsonl"
        self._old = {k: os.environ.get(k) for k in ("MOS_HOME", "MOS_ROTATE_AT")}
        os.environ["MOS_HOME"] = str(Path(self.tmp.name) / "home")
        os.environ.pop("MOS_ROTATE_AT", None)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write(self, rows):
        self.file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def payload(self):
        return {"transcript_path": str(self.file), "session_id": "s1", "cwd": str(self.root)}


class NudgeTest(Case):
    def test_last_call_skips_subagents_and_finds_tail(self):
        self.write([assistant(50_000), {"type": "user", "message": {"content": "x" * 600_000}},
                    assistant(120_000), assistant(999_000, sidechain=True)])
        self.assertEqual(transcript.last_call(self.file)["context"], 120_000)

    def test_fires_once_per_level(self):
        now = T0 + timedelta(minutes=1)
        self.write([assistant(60_000)])
        self.assertIsNone(transcript.nudge(self.payload(), now))
        self.write([assistant(130_000)])
        out = transcript.nudge(self.payload(), now)
        self.assertIn("130K", out["systemMessage"])
        self.assertIn("handoff", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(transcript.nudge(self.payload(), now))  # same level: silent
        self.write([assistant(190_000)])
        self.assertIsNone(transcript.nudge(self.payload(), now))  # still below the next doubling
        self.write([assistant(210_000)])
        self.assertIsNotNone(transcript.nudge(self.payload(), now))

    def test_cold_resume_warns_once(self):
        self.write([assistant(70_000)])
        later = T0 + timedelta(hours=3)
        out = transcript.nudge(self.payload(), later)
        self.assertIn("resuming after 3h", out["systemMessage"])
        self.assertIsNone(transcript.nudge(self.payload(), later))

    def test_can_be_disabled_and_tolerates_missing_file(self):
        self.write([assistant(500_000)])
        os.environ["MOS_ROTATE_AT"] = "0"
        self.assertIsNone(transcript.nudge(self.payload(), T0))
        os.environ.pop("MOS_ROTATE_AT")
        self.assertIsNone(transcript.nudge({"transcript_path": str(self.root / "nope.jsonl")}, T0))


class CaptureTest(Case):
    def rows(self):
        inside = str(self.root / "src" / "app.py")
        return [
            user("build the notification system"),
            user("<system-reminder>ignore me</system-reminder>"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "out"}]}},
            assistant(80_000, blocks=[
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": inside}},
                {"type": "tool_use", "id": "t2", "name": "Write", "input": {"file_path": "/somewhere/else/x.py"}}]),
            user("now use api_key=abc123supersecret and token ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ]

    def test_scrub(self):
        cleaned = transcript.scrub("key sk-abcdefghijklmnopqrstuv and password: hunter2 ok")
        self.assertNotIn("abcdefghijklmnop", cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertIn("password: [redacted]", cleaned)

    def test_summarize(self):
        self.write(self.rows())
        summary = transcript.summarize(self.file, self.root)
        self.assertEqual(summary["files"], ["src/app.py"])
        self.assertEqual(len(summary["prompts"]), 2)
        self.assertEqual(summary["prompts"][0], "build the notification system")
        self.assertNotIn("abc123supersecret", summary["prompts"][1])
        self.assertNotIn("ghp_", summary["prompts"][1])
        self.assertEqual(summary["context"], 80_000)

    def test_capture_is_opt_in_and_feeds_the_brief(self):
        self.write(self.rows())
        self.assertIsNone(transcript.capture(self.payload()))  # no .memory yet
        store = Store(self.root)
        store.init()
        mem = transcript.capture(self.payload())
        self.assertEqual(mem.id, "last-session")
        self.assertIn("last-session.md", (store.dir / ".gitignore").read_text(encoding="utf-8"))
        transcript.capture(self.payload())  # second run must not duplicate the ignore entry
        self.assertEqual((store.dir / ".gitignore").read_text(encoding="utf-8").count("last-session.md"), 1)
        brief = compile_brief(store)
        self.assertIn("src/app.py", brief["text"])
        self.assertIn("notification system", brief["text"])


if __name__ == "__main__":
    unittest.main()
