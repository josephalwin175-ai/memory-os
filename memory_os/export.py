"""Portable export: your memories as one self-contained file.

Memories already live as plain Markdown in `.memory/`, so nothing here depends
on any AI provider or subscription. Export exists for convenience: one file to
paste into any assistant, attach to a chat, or commit as AGENTS.md-style notes.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

from .store import TYPES, Store

HEADINGS = {"decision": "Decisions", "fact": "Facts", "procedure": "Procedures", "preference": "Preferences",
            "failure": "Things that did not work", "status": "Status"}


def export(store: Store, fmt: str = "markdown", include_all: bool = False) -> str:
    memories = [m for m in store.all() if include_all or m.status == "active"]
    if fmt == "json":
        return json.dumps({"exported": date.today().isoformat(), "project": store.root.name,
                           "memories": [asdict(m) for m in memories]}, indent=2, ensure_ascii=False)
    if fmt != "markdown":
        raise ValueError("format must be markdown or json")
    lines = [f"# Project knowledge: {store.root.name}", "",
             f"Exported from MEMORY OS on {date.today().isoformat()}. {len(memories)} memories. "
             "Give this file to any AI assistant as project context.", ""]
    for kind in TYPES:
        group = sorted((m for m in memories if m.type == kind), key=lambda m: (not m.pin, m.id))
        if not group:
            continue
        lines += [f"## {HEADINGS[kind]}", ""]
        for mem in group:
            tags = [t for t in ("pinned" if mem.pin else "", mem.status if mem.status != "active" else "",
                                "unverified source" if mem.trust == "external" else "") if t]
            lines.append(f"### {mem.title}" + (f" ({', '.join(tags)})" if tags else ""))
            body = "\n".join(mem.text.strip().splitlines()[1:]).strip()
            if body:
                lines.append(body)
            if mem.checks:
                lines.append("Verified by: " + "; ".join(f"`{c}`" for c in mem.checks))
            lines.append(f"Last verified: {mem.verified or 'never'}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
