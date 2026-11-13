"""Code map: a compact outline of a codebase, so an agent can jump to the right
lines instead of reading whole files to find things.

Python is parsed with the standard `ast` module. Other languages use
line-based patterns, which find most definitions but are not a real parser.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

from .brief import estimate_tokens

MAX_FILES = 4000
MAX_BYTES = 400_000
DEFAULT_BUDGET = 2000
SKIP_DIRS = {".git", "node_modules", "dist", "build", "out", "target", "vendor", ".venv", "venv", "env",
             "__pycache__", ".next", ".nuxt", ".cache", "coverage", ".idea", ".vscode", ".memory", "site-packages"}

_JS = [
    ("function", re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)")),
    ("class", re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)")),
    ("const", re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
                         r"(?:async\s*)?(?:\(|function\b|[A-Za-z_$][\w$]*\s*=>|<)")),
]
_GO = [
    ("func", re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
    ("type", re.compile(r"^type\s+([A-Za-z_]\w*)")),
]
_RUST = [
    ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")),
    ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|mod|union)\s+([A-Za-z_]\w*)")),
    ("impl", re.compile(r"^impl(?:<[^>]*>)?\s+([A-Za-z_][\w:<>, ]*?)\s*(?:\{|where|$)")),
]
_JVM = [
    ("type", re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|final|sealed|static|data|open|partial)\s+)*"
                        r"(?:class|interface|enum|record|object|struct)\s+([A-Za-z_]\w*)")),
    ("fun", re.compile(r"^\s*(?:(?:public|private|protected|internal|override|suspend|open|inline)\s+)*fun\s+"
                       r"(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)")),
]
_RUBY = [("def", re.compile(r"^\s*(?:def|class|module)\s+([A-Za-z_][\w.:?!]*)"))]
_PHP = [
    ("function", re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?([A-Za-z_]\w*)")),
    ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)")),
]
_C = [("type", re.compile(r"^(?:typedef\s+)?(?:class|struct|enum|union|namespace)\s+([A-Za-z_]\w*)"))]

LANGS = {
    ".js": _JS, ".jsx": _JS, ".mjs": _JS, ".cjs": _JS, ".ts": _JS, ".tsx": _JS, ".vue": _JS, ".svelte": _JS,
    ".go": _GO, ".rs": _RUST, ".java": _JVM, ".kt": _JVM, ".cs": _JVM, ".scala": _JVM, ".swift": _JVM,
    ".rb": _RUBY, ".php": _PHP, ".c": _C, ".h": _C, ".cpp": _C, ".hpp": _C, ".cc": _C,
}
EXTENSIONS = set(LANGS) | {".py"}

_cache: dict[str, tuple] = {}


def list_files(root: Path) -> list[Path]:
    """Source files under root, honouring .gitignore when the folder is a git repository."""
    files: list[Path] = []
    try:
        if (root / ".git").exists():
            done = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
                                  capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace")
            if done.returncode == 0:
                files = [root / line for line in done.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError):
        files = []
    if not files:
        for folder, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            files += [Path(folder) / name for name in names]
    picked = [f for f in files if f.suffix.lower() in EXTENSIONS
              and not any(part in SKIP_DIRS for part in f.relative_to(root).parts[:-1])]
    return sorted(picked)[:MAX_FILES]


def _python_symbols(text: str) -> list[tuple[int, int, str]]:
    tree = ast.parse(text)
    found: list[tuple[int, int, str]] = []

    def args_of(node) -> str:
        names = [a.arg for a in node.args.posonlyargs + node.args.args if a.arg not in ("self", "cls")]
        if node.args.vararg:
            names.append("*" + node.args.vararg.arg)
        names += [a.arg for a in node.args.kwonlyargs]
        if node.args.kwarg:
            names.append("**" + node.args.kwarg.arg)
        shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
        return f"({shown})"

    def visit(body, depth: int) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((node.lineno, depth, f"def {node.name}{args_of(node)}"))
            elif isinstance(node, ast.ClassDef):
                found.append((node.lineno, depth, f"class {node.name}"))
                if depth == 0:
                    visit(node.body, 1)

    visit(tree.body, 0)
    return found


def _pattern_symbols(text: str, patterns) -> list[tuple[int, int, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > 300:
            continue
        for kind, pattern in patterns:
            match = pattern.match(line)
            if match:
                depth = 1 if line[:1] in (" ", "\t") else 0
                found.append((number, depth, f"{kind} {match.group(1).strip()}"))
                break
    return found


def file_symbols(path: Path) -> tuple[int, list[tuple[int, int, str]]]:
    """(line count, [(line, depth, label)]) for one file, cached by modification time."""
    try:
        info = path.stat()
    except OSError:
        return 0, []
    key = str(path)
    stamp = (info.st_mtime_ns, info.st_size)
    cached = _cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    if info.st_size > MAX_BYTES:
        result = (0, [])
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0, []
        suffix = path.suffix.lower()
        if suffix == ".py":
            try:
                symbols = _python_symbols(text)
            except (SyntaxError, ValueError, RecursionError):
                symbols = _pattern_symbols(text, [("def", re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)"))])
        else:
            symbols = _pattern_symbols(text, LANGS[suffix])
        result = (text.count("\n") + 1, symbols)
    _cache[key] = (stamp, *result)
    return result


def build(root: Path, query: str = "", path: str = "", budget: int = DEFAULT_BUDGET) -> str:
    root = root.resolve()
    files = list_files(root)
    wanted = path.replace("\\", "/").strip("/").lower()
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_$]+", query or "")]
    entries = []
    for file in files:
        rel = file.relative_to(root).as_posix()
        if wanted and wanted not in rel.lower():
            continue
        lines, symbols = file_symbols(file)
        if terms:
            path_hit = any(t in rel.lower() for t in terms)
            hits = [s for s in symbols if any(t in s[2].lower() for t in terms)]
            if not hits and not path_hit:
                continue
            score = sum(t in s[2].lower() for s in hits for t in terms) * 2 + path_hit
            entries.append((-score, rel, lines, hits if hits else symbols[:12]))
        else:
            entries.append((0, rel, lines, symbols))
    if not entries:
        return "No matching source files or symbols." if (terms or wanted) else "No source files found."
    entries.sort(key=lambda e: (e[0], e[1]))

    def block(rel, lines, symbols) -> str:
        head = f"{rel} ({lines} lines)" if lines else f"{rel} (too large to outline)"
        return "\n".join([head] + [f"  {n}{'    ' if d else '  '}{label}" for n, d, label in symbols])

    out, used, shown = [], 0, 0
    for _, rel, lines, symbols in entries:
        text = block(rel, lines, symbols)
        cost = estimate_tokens(text) + 1
        if used + cost > budget:
            continue
        out.append(text)
        used += cost
        shown += 1
    left = [e for e in entries if not any(o.startswith(e[1] + " (") for o in out)]
    if left:
        # Over budget: fall back to one line per remaining file so nothing is hidden.
        names = [f"{rel} ({lines} lines, {len(symbols)} symbols)" for _, rel, lines, symbols in left]
        room = max(budget - used, 200) * 4
        listing, size = [], 0
        for name in names:
            if size + len(name) + 1 > room:
                break
            listing.append(name)
            size += len(name) + 1
        hidden = len(names) - len(listing)
        out.append("Not outlined (narrow with a query or path to see their symbols):\n  " + "\n  ".join(listing)
                   + (f"\n  ...and {hidden} more files" if hidden else ""))
    header = f"Code map: {shown} of {len(entries)} files outlined. Numbers are line numbers; Read with offset and limit."
    return header + "\n\n" + "\n".join(out)
