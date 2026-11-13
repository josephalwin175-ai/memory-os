"""`mos` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, meter
from .brief import DEFAULT_BUDGET, compile_brief
from .store import STATUSES, TYPES, Store


def _out(text: str) -> None:
    sys.stdout.buffer.write((text.rstrip("\n") + "\n").encode("utf-8", errors="replace"))


def cmd_meter(args) -> int:
    base = Path(args.dir) if args.dir else None
    prof = meter.profile(base, threshold=args.threshold, project=args.project)
    _out(json.dumps(prof, indent=2) if args.json else meter.render(prof))
    return 0


def cmd_init(args) -> int:
    store = Store(args.root)
    _out(f"Memory folder ready: {store.init()}")
    return 0


def cmd_save(args) -> int:
    store = Store(args.root)
    mem = store.save(text=args.text, type=args.type, mem_id=args.id or "", checks=args.check or [],
                     supersedes=args.supersedes or "", pin=args.pin, trust="user")
    _out(f"Saved [{mem.id}] -> {store.dir / (mem.id + '.md')}")
    return 0


def cmd_search(args) -> int:
    hits = Store(args.root).search(args.query, limit=args.limit, include_inactive=args.all)
    for mem in hits:
        _out(f"[{mem.id}] ({mem.type}, {mem.status}) {mem.title}")
    if not hits:
        _out("No matching memories.")
    return 0


def cmd_list(args) -> int:
    memories = Store(args.root).all()
    for mem in memories:
        flags = ("pin " if mem.pin else "") + (f"{len(mem.checks)} check(s)" if mem.checks else "no checks")
        _out(f"[{mem.id}] ({mem.type}, {mem.status}, {mem.trust}) {mem.title}  -- {flags}")
    if not memories:
        _out("No memories yet. Save one with: mos save \"...\"")
    return 0


def cmd_get(args) -> int:
    mem = Store(args.root).get(args.id)
    if mem is None:
        _out("No memory with that id.")
        return 1
    _out(mem.dump())
    return 0


def cmd_status(args) -> int:
    mem = Store(args.root).set_status(args.id, args.status)
    _out(f"[{mem.id}] is now {mem.status}")
    return 0


def cmd_verify(args) -> int:
    report = Store(args.root).verify(apply=args.fix)
    bad = [r for r in report if not r["ok"]]
    for r in report:
        _out(f"{'ok   ' if r['ok'] else 'STALE'} [{r['id']}] {r['title']}")
        for failure in r["failures"]:
            _out(f"        {failure}")
    _out(f"{len(report) - len(bad)} of {len(report)} checked memories still hold."
         + ("" if args.fix or not bad else "  Run with --fix to mark the failures stale."))
    return 1 if bad else 0


def cmd_brief(args) -> int:
    brief = compile_brief(Store(args.root), task=args.task or "", budget=args.budget)
    if not brief["total"]:
        _out("No memories saved for this project yet.")
        return 0
    _out(brief["text"])
    if args.receipt:
        _out(f"--- receipt (estimated at 4 chars/token) ---\n"
             f"brief: {brief['tokens']} tokens; all memories in full: {brief['all_memories_tokens']} tokens\n"
             f"included {len(brief['included'])}, titles only {len(brief['indexed'])}, stale {len(brief['stale'])}")
    return 0


def _hook_payload() -> dict:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    data = json.loads(raw) if raw.strip() else {}
    return data if isinstance(data, dict) else {}


def cmd_hook(args) -> int:
    """Hooks must never break the host session, so every failure is swallowed."""
    try:
        payload = _hook_payload()
        if args.event == "session-start":
            if payload.get("source") in ("compact", "clear"):
                from .guards import forget_reads
                forget_reads(payload)
            store = Store(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
            if store.exists():
                brief = compile_brief(store)
                if brief["text"]:
                    _out(brief["text"])
        elif args.event == "prompt":
            from .transcript import nudge
            output = nudge(payload)
            if output:
                _out(json.dumps(output))
        elif args.event == "capture":
            from .guards import forget_reads
            from .transcript import capture
            if payload.get("hook_event_name") == "PreCompact":
                forget_reads(payload)
            capture(payload)
        elif args.event in ("pre-tool", "post-tool"):
            from . import guards
            output = guards.read_guard(payload) if args.event == "pre-tool" else guards.trim_output(payload)
            if output:
                _out(json.dumps(output))
    except Exception:
        pass
    return 0


def cmd_capture(args) -> int:
    from .transcript import capture
    transcript = args.transcript
    if not transcript:
        store = Store(args.root)
        folder = meter.default_transcript_dir()
        files = sorted(folder.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime) if folder.exists() else []
        if not files:
            _out("No transcripts found; pass --transcript.")
            return 1
        transcript = str(files[-1])
    mem = capture({"transcript_path": transcript, "cwd": args.root or os.getcwd()})
    if mem is None:
        _out("Nothing captured. The project needs a .memory folder (mos init) and a transcript with activity.")
        return 1
    _out(mem.text)
    return 0


def cmd_map(args) -> int:
    from . import codemap
    from .store import find_root
    _out(codemap.build(find_root(args.root), query=args.query or "", path=args.path or "", budget=args.budget))
    return 0


def cmd_export(args) -> int:
    from .export import export
    text = export(Store(args.root), fmt=args.format, include_all=args.all)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        _out(f"Wrote {args.out}")
    else:
        _out(text)
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import serve
    return serve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mos", description="MEMORY OS: memory for AI agents that has to earn its place.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", help="project folder (default: found from the current folder)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("meter", help="show where your Claude Code tokens went")
    p.add_argument("--dir", help="transcript folder (default: ~/.claude/projects)")
    p.add_argument("--project", help="only projects whose folder name contains this text")
    p.add_argument("--threshold", type=int, default=80_000, help="context size treated as 'time to rotate'")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_meter)

    p = sub.add_parser("init", help="create the .memory folder here")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("save", help="save a memory")
    p.add_argument("text")
    p.add_argument("--type", choices=TYPES, default="fact")
    p.add_argument("--id")
    p.add_argument("--check", action="append", help="checkable claim, e.g. 'file_exists: src/db.py' (repeatable)")
    p.add_argument("--supersedes")
    p.add_argument("--pin", action="store_true")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("search", help="search memories")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--all", action="store_true", help="include stale, superseded and archived")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("list", help="list all memories")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="print one memory file")
    p.add_argument("id")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("status", help="change a memory's status")
    p.add_argument("id")
    p.add_argument("status", choices=STATUSES)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="re-run every memory's checks against the repo")
    p.add_argument("--fix", action="store_true", help="mark failing memories stale and refresh passing ones")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("brief", help="compile the session brief")
    p.add_argument("task", nargs="?")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    p.add_argument("--receipt", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("hook", help="entry point for Claude Code hooks")
    p.add_argument("event", choices=["session-start", "prompt", "capture", "pre-tool", "post-tool"])
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("map", help="outline the codebase: files, functions and classes with line numbers")
    p.add_argument("query", nargs="?", help="only symbols or files matching these words")
    p.add_argument("--path", help="only files whose path contains this text")
    p.add_argument("--budget", type=int, default=2000, help="approximate token budget for the output")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("export", help="write all memories to one portable file")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out", help="file to write (default: print)")
    p.add_argument("--all", action="store_true", help="include stale, superseded and archived memories")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("capture", help="record what the last session was working on")
    p.add_argument("--transcript", help="transcript file (default: the most recently changed one)")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("mcp", help="run the MCP server on stdio")
    p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"mos: {exc}", file=sys.stderr)
        return 2
