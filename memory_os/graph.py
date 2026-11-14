"""Connections around a file: what it imports, what imports it, which saved
memories are about it, and, when a Graphify graph is present, its neighbours
there.

Import edges come from Python's `ast` and from import/require patterns in
JavaScript and TypeScript. Other languages are not linked yet.

Graphify (github.com/Graphify-Labs/graphify) is a separate, optional tool that
writes `graphify-out/graph.json` in NetworkX node-link form. If that file
exists it is read as an extra source of relationships. The reader follows the
fields Graphify's exporter writes (checked against its source at v0.9) and is
tested on a fixture in that format. MEMORY OS never runs or requires Graphify.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

from .codemap import list_files
from .store import Store

JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")
JS_IMPORT = re.compile(r"""(?:from\s+|import\s+|require\(\s*|import\(\s*)['"](\.{1,2}/[^'"]+)['"]""")
GRAPHIFY_FILE = Path("graphify-out") / "graph.json"
MAX_SHOWN = 20


def _python_imports(path: Path, root: Path, by_module: dict[str, str]) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError, RecursionError):
        return set()
    package = path.parent.relative_to(root).parts
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(package[:len(package) - node.level + 1]) if node.level else []
            module = ".".join(base + ([node.module] if node.module else []))
            if module:
                names.add(module)
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    found = set()
    for name in names:
        parts = name.split(".")
        for size in range(len(parts), 0, -1):
            hit = by_module.get(".".join(parts[:size]))
            if hit:
                found.add(hit)
                break
    return found


def _js_imports(path: Path, root: Path, known: set[str]) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    found = set()
    for spec in JS_IMPORT.findall(text):
        try:
            base = (path.parent / spec).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        for candidate in [base] + [base + s for s in JS_SUFFIXES] + [f"{base}/index{s}" for s in JS_SUFFIXES]:
            if candidate in known:
                found.add(candidate)
                break
    return found


def import_edges(root: Path) -> dict[str, set[str]]:
    """{file: files it imports}, for files inside the project only."""
    root = root.resolve()
    files = list_files(root)
    rels = {f.relative_to(root).as_posix(): f for f in files}
    by_module: dict[str, str] = {}
    for rel in rels:
        if rel.endswith(".py"):
            dotted = rel[:-3].replace("/", ".")
            if dotted.endswith(".__init__"):
                dotted = dotted[:-9]
            parts = dotted.split(".")
            # Register every suffix so `src/app/db.py` answers to `app.db` and `db` as well.
            for start in range(len(parts)):
                by_module.setdefault(".".join(parts[start:]), rel)
    known = set(rels)
    edges: dict[str, set[str]] = {}
    for rel, path in rels.items():
        if rel.endswith(".py"):
            targets = _python_imports(path, root, by_module)
        elif rel.endswith(JS_SUFFIXES):
            targets = _js_imports(path, root, known)
        else:
            continue
        targets.discard(rel)
        if targets:
            edges[rel] = targets
    return edges


def load_graphify(root: Path) -> dict | None:
    """Nodes, links and build details from a Graphify graph, or None when there is none or it cannot be read.

    Follows the format Graphify writes (NetworkX node-link data with a "links"
    list, plus `built_at_commit` and `graph.graphify_version`), and also
    accepts an "edges" list and a few older field names.
    """
    path = root / GRAPHIFY_FILE
    try:
        if not path.is_file() or path.stat().st_size > 50_000_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return None
    nodes = {}
    for node in data["nodes"]:
        if not isinstance(node, dict) or "id" not in node:
            continue
        source = str(node.get("source_file") or node.get("file") or node.get("path") or "").replace("\\", "/")
        location = str(node.get("source_location") or "")
        nodes[str(node["id"])] = {
            "label": str(node.get("label") or node.get("name") or node["id"]),
            "file": source,
            "where": f"{source}:{location[1:]}" if source and re.fullmatch(r"L\d+", location) else source,
            "kind": str(node.get("file_type") or "concept"),
            "community": str(node.get("community_name") or ""),
        }
    links = []
    for link in data.get("links") or data.get("edges") or []:
        if not isinstance(link, dict):
            continue
        source, target = link.get("source", link.get("from")), link.get("target", link.get("to"))
        if source is None or target is None:
            continue
        relation = str(link.get("relation") or link.get("type") or link.get("relationship") or "related_to")
        links.append((str(source), str(target), relation.replace("_", " "), str(link.get("confidence") or "").upper()))
    meta = data.get("graph") if isinstance(data.get("graph"), dict) else {}
    return {"nodes": nodes, "links": links, "path": path,
            "built_at_commit": str(data.get("built_at_commit") or ""),
            "version": str(meta.get("graphify_version") or "")}


def _certainty(confidence: str) -> str:
    """Graphify marks guessed links; pass that on so a guess is not read as a fact."""
    return {"INFERRED": " (inferred, not certain)", "AMBIGUOUS": " (ambiguous, verify first)"}.get(confidence, "")


def graphify_matches(root: Path, terms: list[str], limit: int = 8) -> list[str]:
    """Graphify nodes whose label matches a query. Documents and concepts come first:
    the code map already covers code symbols, so these are what it cannot see."""
    graph = load_graphify(root)
    if not graph or not terms:
        return []
    hits = [n for n in graph["nodes"].values() if any(t in n["label"].lower() for t in terms)]
    hits.sort(key=lambda n: (n["kind"] == "code", n["label"].lower()))
    return [f"{n['label']} [{n['kind']}]" + (f" ({n['where']})" if n["where"] else "") for n in hits[:limit]]


def graphify_status(root: Path) -> dict | None:
    """Size and freshness of the Graphify graph, or None when the project has none."""
    root = root.resolve()
    graph = load_graphify(root)
    if not graph:
        return None
    kinds: dict[str, int] = {}
    for node in graph["nodes"].values():
        kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
    status = {"nodes": len(graph["nodes"]), "links": len(graph["links"]), "kinds": kinds,
              "version": graph["version"], "built_at_commit": graph["built_at_commit"],
              "report": (root / GRAPHIFY_FILE.parent / "GRAPH_REPORT.md").is_file(),
              "behind": None, "changed_files": None, "stale": False}
    built = graph["built_at_commit"]
    if built and re.fullmatch(r"[0-9a-fA-F]{7,40}", built) and (root / ".git").exists():
        try:
            done = subprocess.run(["git", "-C", str(root), "rev-list", "--count", f"{built}..HEAD"],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode == 0 and done.stdout.strip().isdigit():
                status["behind"] = int(done.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        built_time = graph["path"].stat().st_mtime
        status["changed_files"] = sum(1 for f in list_files(root) if f.stat().st_mtime > built_time)
    except OSError:
        pass
    status["stale"] = bool(status["behind"]) or bool(status["changed_files"])
    return status


def render_status(status: dict | None) -> str:
    if status is None:
        return ("No Graphify graph in this project (graphify-out/graph.json not found).\n"
                "Graphify is a separate, optional tool: pip install graphifyy, then run `graphify extract` here.\n"
                "MEMORY OS works without it and reads the graph when it exists.")
    kinds = ", ".join(f"{count} {kind}" for kind, count in sorted(status["kinds"].items(), key=lambda kv: -kv[1]))
    lines = [f"Graphify graph: {status['nodes']} nodes ({kinds}), {status['links']} links"
             + (f", built by graphify {status['version']}" if status["version"] else "")]
    if status["built_at_commit"]:
        lines.append(f"Built at commit {status['built_at_commit'][:10]}"
                     + (f", {status['behind']} commit(s) behind HEAD" if status["behind"] else
                        (", up to date with HEAD" if status["behind"] == 0 else "")))
    if status["changed_files"]:
        lines.append(f"{status['changed_files']} source file(s) changed since the graph was written")
    lines.append("Out of date: refresh it with `graphify update .`" if status["stale"] else "The graph looks current.")
    if status["report"]:
        lines.append("Overview: graphify-out/GRAPH_REPORT.md")
    return "\n".join(lines)


def around(root: Path, target: str) -> str:
    """Everything connected to one file, as compact text."""
    root = root.resolve()
    wanted = target.replace("\\", "/").strip("/")
    edges = import_edges(root)
    known = {f.relative_to(root).as_posix() for f in list_files(root)}
    matches = sorted(f for f in known if f == wanted or f.endswith("/" + wanted) or f.endswith(wanted))
    if not matches:
        return f"No source file matching {target!r}."
    if len(matches) > 1 and wanted not in matches:
        return "Several files match; be more specific:\n  " + "\n  ".join(matches[:MAX_SHOWN])
    rel = wanted if wanted in matches else matches[0]
    lines = [f"Around {rel}"]

    def section(title: str, items: list[str]) -> None:
        if items:
            extra = f"\n  ...and {len(items) - MAX_SHOWN} more" if len(items) > MAX_SHOWN else ""
            lines.append(f"{title}:\n  " + "\n  ".join(items[:MAX_SHOWN]) + extra)

    section("Imports", sorted(edges.get(rel, ())))
    section("Imported by", sorted(src for src, targets in edges.items() if rel in targets))

    name = Path(rel).name
    about = []
    for mem in Store(root).all():
        if mem.status != "active":
            continue
        blob = " ".join([mem.text] + mem.checks + mem.guards)
        if rel in blob or re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w-])", blob):
            about.append(f"[{mem.id}] {mem.title}")
    section("Saved memories about this file", about)

    graph = load_graphify(root)
    if graph:
        mine = {nid for nid, node in graph["nodes"].items()
                if node["file"] and (node["file"] == rel or node["file"].endswith("/" + rel))}
        related = set()
        for source, dest, relation, confidence in graph["links"]:
            if (source in mine) == (dest in mine):
                continue  # both ends inside this file, or neither
            outgoing = source in mine
            here, there = (source, dest) if outgoing else (dest, source)
            other = graph["nodes"].get(there)
            if not other or relation == "contains":
                continue
            arrow = "->" if outgoing else "<-"
            related.add(f"{graph['nodes'][here]['label']} {arrow} {relation} {arrow} {other['label']}"
                        + (f" ({other['where']})" if other["where"] else "") + _certainty(confidence))
        section("Graphify relationships", sorted(related))
    if len(lines) == 1:
        lines.append("No imports, importers, memories or graph links found for this file.")
    return "\n".join(lines)
