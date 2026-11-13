import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_os import codemap  # noqa: E402
from memory_os.export import export  # noqa: E402
from memory_os.store import Store  # noqa: E402

PY = '''class Store:
    def save(self, text, kind="fact", *rest, **more):
        def inner():
            pass
        return inner

async def find_root(start):
    return start
'''
TS = '''export async function issueToken(user: User) {}
export const sign = (payload: object) => payload
export interface Session { id: string }
class Mailer {
  send() {}
}
const limit = 5
'''
GO = '''func (s *Server) Start(port int) error { return nil }
type Server struct {}
'''


class CodeMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "node_modules" / "lib").mkdir(parents=True)
        (self.root / "src" / "store.py").write_text(PY, encoding="utf-8")
        (self.root / "src" / "auth.ts").write_text(TS, encoding="utf-8")
        (self.root / "src" / "server.go").write_text(GO, encoding="utf-8")
        (self.root / "src" / "broken.py").write_text("def ok(:\n  pass\nclass Later:\n", encoding="utf-8")
        (self.root / "node_modules" / "lib" / "x.js").write_text("function hidden() {}", encoding="utf-8")
        (self.root / "notes.txt").write_text("def not_code(): pass", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_outline_has_symbols_with_lines_and_skips_dependencies(self):
        text = codemap.build(self.root)
        self.assertIn("src/store.py (9 lines)", text)
        self.assertIn("1  class Store", text)
        self.assertIn("2    def save(text, kind, *rest, **more)", text)
        self.assertIn("7  def find_root(start)", text)
        self.assertNotIn("inner", text)
        for expected in ("function issueToken", "const sign", "type Session", "class Mailer",
                         "func Start", "type Server", "def Later"):
            self.assertIn(expected, text)
        self.assertNotIn("const limit", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("notes.txt", text)

    def test_query_and_path_filter(self):
        text = codemap.build(self.root, query="token")
        self.assertIn("function issueToken", text)
        self.assertNotIn("store.py", text)
        self.assertIn("server.go", codemap.build(self.root, path="server"))
        self.assertNotIn("auth.ts", codemap.build(self.root, path="server"))
        self.assertIn("No matching", codemap.build(self.root, query="zzzznothing"))

    def test_budget_falls_back_to_file_list(self):
        for i in range(60):
            (self.root / "src" / f"mod{i:02d}.py").write_text(
                "\n".join(f"def function_number_{j}(a, b):\n    pass" for j in range(30)), encoding="utf-8")
        text = codemap.build(self.root, budget=400)
        self.assertIn("Not outlined", text)
        self.assertLess(len(text) / 4, 900)

    def test_cache_refreshes_on_change(self):
        target = self.root / "src" / "store.py"
        codemap.build(self.root)
        target.write_text(PY + "\ndef brand_new_one():\n    pass\n", encoding="utf-8")
        self.assertIn("brand_new_one", codemap.build(self.root))


class ExportTest(unittest.TestCase):
    def test_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.save("Database is PostgreSQL\nChosen for joins.", type="decision", pin=True,
                       checks=["file_absent: mongo.conf"])
            old = store.save("We use MongoDB", type="decision", mem_id="mongo")
            store.set_status(old.id, "archived")
            store.save("Tried Redis pub/sub, lost messages", type="failure")
            text = export(store)
            self.assertIn("## Decisions", text)
            self.assertIn("### Database is PostgreSQL (pinned)", text)
            self.assertIn("Chosen for joins.", text)
            self.assertIn("`file_absent: mongo.conf`", text)
            self.assertIn("## Things that did not work", text)
            self.assertNotIn("MongoDB", text)
            self.assertIn("MongoDB", export(store, include_all=True))
            data = json.loads(export(store, fmt="json"))
            self.assertEqual(len(data["memories"]), 2)
            with self.assertRaises(ValueError):
                export(store, fmt="xml")


if __name__ == "__main__":
    unittest.main()
