import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_os import bench, extract, graph, guards, importers, inbox, ledger  # noqa: E402
from memory_os.brief import compile_brief  # noqa: E402
from memory_os.cli import verify_markdown  # noqa: E402
from memory_os.store import Store, parse  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.store = Store(self.root)
        self.store.init()
        self._old = {k: os.environ.get(k) for k in ("MOS_HOME", "MOS_EXTRACT_CMD", "MOS_GUARDS", "MOS_TRIM")}
        os.environ["MOS_HOME"] = str(Path(self.tmp.name) / "home")
        for key in ("MOS_EXTRACT_CMD", "MOS_GUARDS", "MOS_TRIM"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class InboxTest(Case):
    def test_candidates_stay_out_of_memory_until_accepted(self):
        cand = inbox.add(self.store, "We decided to use PostgreSQL instead of MongoDB", type="decision", source="t")
        self.assertIsNotNone(cand)
        self.assertEqual(self.store.all(), [])
        self.assertEqual(len(inbox.box(self.store).all()), 1)
        self.store.save("Something real")
        brief = compile_brief(self.store)
        self.assertNotIn("PostgreSQL", brief["text"])
        self.assertIn("1 candidate memory is waiting", brief["text"])
        mem = inbox.accept(self.store, cand.id)
        self.assertEqual(mem.trust, "user")
        self.assertEqual(inbox.box(self.store).all(), [])
        self.assertIn("PostgreSQL", compile_brief(self.store)["text"])

    def test_duplicates_and_rejections_are_not_proposed_again(self):
        first = inbox.add(self.store, "Always run the linter before committing")
        self.assertIsNone(inbox.add(self.store, "always run the linter before committing!"))
        inbox.reject(self.store, first.id)
        self.assertIsNone(inbox.add(self.store, "Always run the linter before committing"))
        self.store.save("The API listens on port 4001")
        self.assertIsNone(inbox.add(self.store, "The API listens on port 4001"))
        self.assertIsNone(inbox.add(self.store, "too short"))


class ExtractTest(Case):
    def transcript(self, messages):
        path = Path(self.tmp.name) / "t.jsonl"
        rows = [{"type": "user", "message": {"content": m}} for m in messages]
        rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done, switched it."}]}})
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return path

    def test_heuristic_finds_decisions_preferences_facts_and_skips_noise(self):
        found = dict((text, kind) for kind, text in extract.heuristic([
            "can you look at the login page?",
            "Let's use PostgreSQL instead of MongoDB for this. Thanks!",
            "Never commit directly to the main branch.",
            "The backend API listens on port 4001 in development.",
            "ok",
            "my token is ghp_abcdefghijklmnopqrstuvwxyz0123 and never share that value",
        ]))
        self.assertEqual(found["Let's use PostgreSQL instead of MongoDB for this."], "decision")
        self.assertEqual(found["Never commit directly to the main branch."], "preference")
        self.assertEqual(found["The backend API listens on port 4001 in development."], "fact")
        self.assertFalse(any("login page" in t for t in found))
        self.assertFalse(any("ghp_" in t for t in found))

    def test_extract_queues_to_inbox_only(self):
        path = self.transcript(["We decided to deploy with Docker rather than bare metal."])
        added = extract.extract(path, self.store)
        self.assertEqual(len(added), 1)
        self.assertEqual(self.store.all(), [])
        self.assertEqual(extract.extract(path, self.store), [])  # second run adds nothing

    def test_command_extractor(self):
        path = self.transcript(["please set things up"])
        script = Path(self.tmp.name) / "fake_llm.py"
        script.write_text(
            "import sys, json\nassert 'CONVERSATION' in sys.stdin.read()\n"
            "print('Here you go:')\n"
            "print(json.dumps([{'type': 'failure', 'text': 'Redis pub/sub lost messages under load'},"
            " {'type': 'nonsense', 'text': 'Queue uses a durable stream now'}, {'bad': 1}]))\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            extract.extract(path, self.store, use_command=True)
        os.environ["MOS_EXTRACT_CMD"] = f'"{sys.executable}" "{script}"'
        added = extract.extract(path, self.store, use_command=True)
        self.assertEqual([(m.type, m.title) for m in added],
                         [("failure", "Redis pub/sub lost messages under load"), ("fact", "Queue uses a durable stream now")])


class ImportTest(Case):
    def test_instruction_file(self):
        path = Path(self.tmp.name) / "CLAUDE.md"
        path.write_text("---\nx: 1\n---\n# Rules\n\n- Always use **pnpm**, never npm, for installs\n- ok\n"
                        "```\n- this bullet is inside code and must be ignored entirely\n```\n\n"
                        "The API server runs on port 4001 and\nthe frontend proxies to it.\n", encoding="utf-8")
        result = importers.import_file(self.store, path)
        self.assertEqual((result["kind"], result["added"]), ("instructions", 2))
        titles = {m.title: m.type for m in inbox.box(self.store).all()}
        self.assertEqual(titles["Always use pnpm, never npm, for installs"], "preference")
        self.assertIn("The API server runs on port 4001 and the frontend proxies to it.", titles)

    def test_chatgpt_export_and_own_export(self):
        chat = Path(self.tmp.name) / "conversations.json"
        chat.write_text(json.dumps([{"title": "x", "mapping": {
            "a": {"message": {"author": {"role": "user"}, "create_time": 2,
                              "content": {"parts": ["From now on reply in British English."]}}},
            "b": {"message": {"author": {"role": "assistant"}, "create_time": 3,
                              "content": {"parts": ["We decided to use tabs everywhere."]}}},
            "c": {"message": None}}}]), encoding="utf-8")
        result = importers.import_file(self.store, chat)
        self.assertEqual((result["kind"], result["added"]), ("chatgpt export", 1))
        own = Path(self.tmp.name) / "export.json"
        own.write_text(json.dumps({"memories": [{"type": "decision", "text": "Use SQLite for local storage",
                                                 "checks": ["file_exists: app.db"]}]}), encoding="utf-8")
        self.assertEqual(importers.import_file(self.store, own)["added"], 1)
        bad = Path(self.tmp.name) / "other.json"
        bad.write_text('{"hello": 1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            importers.import_file(self.store, bad)


class GuardTest(Case):
    def test_guard_round_trip_and_brief(self):
        self.store.save("Do not edit generated files by hand\nRun make gen instead.", type="procedure",
                        mem_id="gen-rule", guards=["edit: src/gen/**"])
        mem = parse((self.store.dir / "gen-rule.md").read_text(encoding="utf-8"))
        self.assertEqual(mem.guards, ["edit: src/gen/**"])
        self.store.save("Plain fact about the project")
        brief = compile_brief(self.store)
        self.assertNotIn("gen-rule", brief["included"])
        self.assertIn("gen-rule: Do not edit generated files by hand (guard", brief["text"])
        with self.assertRaises(ValueError):
            self.store.save("x" * 30, guards=["delete: everything"])

    def test_fires_once_for_matching_edit_and_command(self):
        self.store.save("Do not edit generated files by hand", mem_id="gen-rule", guards=["edit: src/gen/**"])
        self.store.save("Never force push; it wiped a release branch once", type="failure", mem_id="no-force",
                        guards=[r"bash: git\s+push\s+.*(--force|-f)\b"])

        def call(tool, **tool_input):
            return guards.pre_tool({"session_id": "s", "cwd": str(self.root), "tool_name": tool, "tool_input": tool_input})

        self.assertIsNone(call("Edit", file_path=str(self.root / "src" / "app.py")))
        out = call("Edit", file_path=str(self.root / "src" / "gen" / "deep" / "types.py"))
        self.assertIn("gen-rule", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(call("Write", file_path=str(self.root / "src" / "gen" / "types.py")))  # once per session
        self.assertIsNone(call("Bash", command="git push origin main"))
        self.assertIn("no-force", call("Bash", command="git push --force origin main")
                      ["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(call("Edit", file_path="/outside/project/src/gen/x.py"))
        self.assertEqual(ledger.report(self.store)["total_uses"], 2)

    def test_glob_semantics(self):
        self.assertTrue(guards._glob_match("src/auth/a/b.py", "src/auth/**"))
        self.assertTrue(guards._glob_match("migrations/001.sql", "migrations/*.sql"))
        self.assertFalse(guards._glob_match("migrations/old/001.sql", "migrations/*.sql"))
        self.assertTrue(guards._glob_match("a/b/c/schema.sql", "**/schema.sql"))
        self.assertTrue(guards._glob_match("schema.sql", "**/schema.sql"))


class TrimMoreToolsTest(Case):
    def test_grep_and_mcp_shapes(self):
        big = "\n".join(f"src/file{i}.py:10: match here" for i in range(3000))
        grep = guards.trim_output({"tool_name": "Grep", "session_id": "s", "tool_response":
                                   {"mode": "content", "numFiles": 3, "filenames": [], "content": big, "numLines": 3000}})
        new = grep["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(set(new), {"mode", "numFiles", "filenames", "content", "numLines"})
        self.assertLess(len(new["content"]), len(big) / 3)
        mcp = guards.trim_output({"tool_name": "mcp__db__query", "session_id": "s", "tool_response":
                                  [{"type": "text", "text": big}, {"type": "image", "data": "x"}]})
        blocks = mcp["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(blocks[1], {"type": "image", "data": "x"})
        self.assertIn("Full output saved at", blocks[0]["text"])
        own = {"tool_name": "mcp__plugin_memory-os_memory-os__code_map", "tool_response": [{"type": "text", "text": big}]}
        self.assertIsNone(guards.trim_output(own))
        self.assertIsNone(guards.trim_output({"tool_name": "Read", "tool_response": {"file": {"content": big}}}))


class LedgerTest(Case):
    def test_costs_uses_and_suggestions(self):
        self.store.save("Never touch the billing module without asking", type="preference", mem_id="billing", pin=True)
        self.store.save("API port is 4001", mem_id="port")
        for _ in range(ledger.UNUSED_AFTER):
            ledger.record_injection(self.root, compile_brief(self.store)["costs"])
        ledger.record_use(self.root, ["port"])
        rep = ledger.report(self.store)
        rows = {r["id"]: r for r in rep["rows"]}
        self.assertEqual(rows["port"]["injected"], ledger.UNUSED_AFTER)
        self.assertEqual(rows["port"]["used"], 1)
        self.assertGreater(rows["billing"]["tokens"], 0)
        self.assertEqual(len(rep["suggestions"]), 1)
        self.assertIn("mos promote billing", rep["suggestions"][0])
        self.assertIn("billing (pin)", ledger.render(rep))


class GraphTest(Case):
    def setUp(self):
        super().setUp()
        (self.root / "app").mkdir()
        (self.root / "web").mkdir()
        (self.root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app" / "db.py").write_text("def connect():\n    pass\n", encoding="utf-8")
        (self.root / "app" / "api.py").write_text("from . import db\nfrom app.db import connect\nimport os\n", encoding="utf-8")
        (self.root / "web" / "client.ts").write_text("export const get = () => 1\n", encoding="utf-8")
        (self.root / "web" / "page.tsx").write_text("import { get } from './client'\nconst x = require('../missing')\n",
                                                    encoding="utf-8")

    def test_import_edges_and_around(self):
        edges = graph.import_edges(self.root)
        self.assertIn("app/db.py", edges["app/api.py"])
        self.assertEqual(edges["web/page.tsx"], {"web/client.ts"})
        self.store.save("Database access goes through app/db.py only", mem_id="db-rule",
                        checks=["file_exists: app/db.py"])
        text = graph.around(self.root, "app/db.py")
        self.assertIn("Imported by:\n  app/api.py", text)
        self.assertIn("[db-rule]", text)
        self.assertIn("No source file matching", graph.around(self.root, "nope.py"))

    def test_reads_a_graphify_graph(self):
        out = self.root / "graphify-out"
        out.mkdir()
        # Field names and layout follow Graphify's exporter (node_link_data with a "links" list).
        (out / "graph.json").write_text(json.dumps({
            "built_at_commit": "", "directed": True, "multigraph": True,
            "graph": {"graphify_version": "0.9.108", "schema_version": 1}, "hyperedges": [],
            "nodes": [
                {"id": "app_db", "label": "db.py", "community": 0, "community_name": "Data layer",
                 "file_type": "code", "norm_label": "db.py", "source_file": "app/db.py", "source_location": "L1"},
                {"id": "app_db_connect", "label": "connect()", "community": 0, "file_type": "code",
                 "norm_label": "connect()", "source_file": "app/db.py", "source_location": "L1"},
                {"id": "app_api", "label": "api.py", "community": 1, "file_type": "code",
                 "source_file": "app/api.py", "source_location": "L1"},
                {"id": "docs_pooling", "label": "Connection pooling design", "community": 0,
                 "file_type": "document", "source_file": "docs/pooling.md", "source_location": None},
                {"id": "psycopg", "label": "psycopg", "file_type": "concept", "type": "external", "external": True,
                 "source_file": ""}],
            "links": [
                {"source": "app_db", "target": "app_db_connect", "relation": "contains", "confidence": "EXTRACTED",
                 "confidence_score": 1.0, "source_file": "app/db.py", "source_location": "L1", "weight": 1.0},
                {"source": "app_api", "target": "app_db_connect", "relation": "calls", "confidence": "EXTRACTED",
                 "confidence_score": 1.0, "source_file": "app/api.py", "source_location": "L2", "weight": 1.0},
                {"source": "app_db_connect", "target": "docs_pooling", "relation": "semantically_similar_to",
                 "confidence": "INFERRED", "confidence_score": 0.75, "weight": 1.0},
                {"source": "app_db", "target": "psycopg", "relation": "imports", "confidence": "AMBIGUOUS",
                 "confidence_score": 0.2}]}), encoding="utf-8")
        (out / "GRAPH_REPORT.md").write_text("# Graph Report - proj\n", encoding="utf-8")
        text = graph.around(self.root, "app/db.py")
        self.assertIn("Graphify relationships", text)
        self.assertIn("connect() <- calls <- api.py (app/api.py:1)", text)
        self.assertIn("connect() -> semantically similar to -> Connection pooling design (docs/pooling.md) "
                      "(inferred, not certain)", text)
        self.assertIn("db.py -> imports -> psycopg (ambiguous, verify first)", text)
        self.assertNotIn("contains", text)
        self.assertEqual(graph.graphify_matches(self.root, ["pooling"]),
                         ["Connection pooling design [document] (docs/pooling.md)"])

        from memory_os import codemap
        mapped = codemap.build(self.root, query="pooling")
        self.assertIn("No matching source symbols.", mapped)
        self.assertIn("Connection pooling design [document]", mapped)
        self.assertIn("From the Graphify graph", codemap.build(self.root, query="connect"))
        self.assertNotIn("Graphify", codemap.build(self.root))

        status = graph.graphify_status(self.root)
        self.assertEqual((status["nodes"], status["links"], status["version"]), (5, 4, "0.9.108"))
        self.assertEqual(status["kinds"]["code"], 3)
        self.assertFalse(status["stale"])
        self.assertIn("looks current", graph.render_status(status))
        import time
        future = time.time() + 60
        os.utime(self.root / "app" / "api.py", (future, future))
        stale = graph.graphify_status(self.root)
        self.assertEqual(stale["changed_files"], 1)
        self.assertIn("graphify update", graph.render_status(stale))

        self.store.save("Some project fact worth keeping")
        self.assertIn("GRAPH_REPORT.md", compile_brief(self.store)["text"])

        (out / "graph.json").write_text("not json", encoding="utf-8")
        self.assertIsNone(graph.load_graphify(self.root))
        self.assertIn("No Graphify graph", graph.render_status(graph.graphify_status(self.root)))


class BenchAndReportTest(Case):
    def test_bench_compiled_beats_baselines(self):
        result = bench.run(seed=3, budget=600)
        methods = result["methods"]
        self.assertEqual(methods["compiled"]["wrong"], 0)
        self.assertGreater(methods["everything"]["wrong"], 0)
        self.assertGreater(methods["compiled"]["recall"], methods["recent"]["recall"])
        self.assertLess(methods["compiled"]["tokens"], methods["everything"]["tokens"])
        self.assertIn("no model in the loop", bench.render(result))

    def test_verify_markdown(self):
        (self.root / "package.json").write_text("{}", encoding="utf-8")
        self.store.save("pg is a dependency", mem_id="pg", checks=["json_has: package.json :: dependencies.pg"])
        self.store.save("package.json exists", mem_id="pkg", checks=["file_exists: package.json"])
        text = verify_markdown(self.store.verify())
        self.assertIn("makes 1 saved project memory false", text)
        self.assertIn("| `pg` pg is a dependency |", text)
        self.assertIn("1 of 2 checked memories still hold", text)
        self.assertIn("all 1 checked", verify_markdown([r for r in self.store.verify() if r["ok"]]))


if __name__ == "__main__":
    unittest.main()
