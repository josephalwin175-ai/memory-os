"""Importers: bring knowledge from other tools into the inbox for review.

Supported inputs:

* instruction files: CLAUDE.md, AGENTS.md, .cursorrules, Cursor .mdc rules,
  Copilot instructions, or any Markdown or text file. Each bullet or short
  paragraph becomes a candidate.
* a ChatGPT data export (conversations.json). The user's own messages are run
  through the same heuristic extractor used for Claude Code transcripts.
* a MEMORY OS JSON export (`mos export --format json`).

Everything lands in the inbox. Nothing is trusted until you accept it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import inbox
from .extract import RULES, heuristic
from .store import TYPES, Store
from .transcript import scrub

MAX_IMPORT = 100
MAX_BYTES = 200_000_000


def _classify(text: str) -> str:
    for kind, pattern in RULES:
        if pattern.search(text):
            return kind
    return "fact"


def from_instructions(text: str) -> list[tuple[str, str]]:
    """Bullets and short paragraphs from a rules or instructions file."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)  # drop front matter
    items: list[str] = []
    in_code = False
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            items.append(" ".join(paragraph))
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code:
            continue
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)", line)
        if bullet:
            flush()
            items.append(bullet.group(1))
        elif not line or line.startswith("#") or line.startswith("|"):
            flush()
        else:
            paragraph.append(line)
    flush()
    out = []
    for item in items:
        item = re.sub(r"[*_`]{1,2}", "", item).strip()
        if 20 <= len(item) <= 400:
            out.append((_classify(item), scrub(item)))
    return out[:MAX_IMPORT]


def _chatgpt_user_messages(data) -> list[str]:
    texts: list[str] = []
    for conversation in data if isinstance(data, list) else []:
        mapping = conversation.get("mapping") if isinstance(conversation, dict) else None
        if not isinstance(mapping, dict):
            continue
        nodes = []
        for node in mapping.values():
            msg = node.get("message") if isinstance(node, dict) else None
            if not isinstance(msg, dict) or (msg.get("author") or {}).get("role") != "user":
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            text = " ".join(p for p in parts if isinstance(p, str)).strip()
            if text:
                nodes.append((msg.get("create_time") or 0, text))
        texts += [t for _, t in sorted(nodes, key=lambda n: n[0])]
    return texts


def import_file(store: Store, path: Path | str) -> dict:
    """Queue candidates from a file. Returns {"kind", "found", "added"}."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("file is too large to import")
    raw = path.read_text(encoding="utf-8", errors="replace")
    source = f"import:{path.name}"
    kind = "instructions"
    pairs: list[tuple[str, str, list[str]]] = []
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"not valid JSON: {exc}") from None
        if isinstance(data, dict) and isinstance(data.get("memories"), list):
            kind = "memory-os export"
            for item in data["memories"][:MAX_IMPORT]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    checks = [c for c in item.get("checks") or [] if isinstance(c, str)]
                    pairs.append((item.get("type") if item.get("type") in TYPES else "fact", item["text"], checks))
        else:
            kind = "chatgpt export"
            pairs = [(k, t, []) for k, t in heuristic(_chatgpt_user_messages(data))]
            if not pairs and not (isinstance(data, list) and data and isinstance(data[0], dict) and "mapping" in data[0]):
                raise ValueError("JSON file is neither a MEMORY OS export nor a ChatGPT conversations.json")
    else:
        pairs = [(k, t, []) for k, t in from_instructions(raw)]
    added = 0
    for mem_type, text, checks in pairs:
        try:
            if inbox.add(store, text, type=mem_type, source=source, checks=checks):
                added += 1
        except ValueError:
            continue  # a candidate with a malformed check is skipped, not fatal
    return {"kind": kind, "found": len(pairs), "added": added}
