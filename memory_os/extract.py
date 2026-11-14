"""Find candidate memories in a conversation and put them in the inbox.

Two extractors:

* heuristic: free and offline. It looks for sentences in the user's own
  messages that read like a decision, a standing preference or a project fact.
  It is deliberately simple and will propose some noise, which is why results
  go to the inbox instead of straight into memory.
* command: set MOS_EXTRACT_CMD to any program that reads a prompt on stdin and
  prints text, for example `ollama run llama3.2` (local, free) or
  `claude -p --model haiku` (uses your plan). It is asked for a JSON list.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import inbox
from .store import TYPES, Store
from .transcript import _prompt_text, scrub

MAX_CANDIDATES = 8
MAX_DIGEST_CHARS = 24_000

RULES = [
    ("decision", re.compile(r"(?i)\b(let'?s (use|go with|switch)|we(?:'ll| will| should)? (use|go with|switch to)|"
                            r"we(?:'ve| have)? decided|decided to|instead of|rather than|switch(?:ed|ing)? to|"
                            r"migrat(?:e|ed|ing) to|replace[ds]? .{2,40} with)\b")),
    ("preference", re.compile(r"(?i)\b(always|never|from now on|make sure (?:to|you)|do not|don'?t (?:ever|use|add|touch|"
                              r"change|commit|push)|i prefer|we prefer|must not|should not)\b")),
    ("fact", re.compile(r"(?i)\b(runs on|listens on|on port \d+|port \d{2,5}|is deployed (?:to|on)|lives in|is located in|"
                        r"the (?:api|database|db|backend|frontend|server) (?:is|uses|runs))\b")),
]
SKIP = re.compile(r"(?i)^(thanks|thank you|ok|okay|yes|no|hi|hello|please|can you|could you|what|why|how|is |are |do |does )")


def heuristic(texts: list[str]) -> list[tuple[str, str]]:
    """(type, sentence) pairs found in the user's messages, most recent first."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text in reversed(texts):
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            sentence = " ".join(sentence.split()).strip(" -*")
            if not 20 <= len(sentence) <= 240 or sentence.endswith("?") or SKIP.match(sentence):
                continue
            for kind, pattern in RULES:
                if pattern.search(sentence) and sentence.lower() not in seen:
                    seen.add(sentence.lower())
                    found.append((kind, scrub(sentence)))
                    break
            if len(found) >= MAX_CANDIDATES:
                return found
    return found


def read_dialogue(path: Path | str) -> tuple[list[str], str]:
    """(user messages, a scrubbed digest of the conversation's tail) from a Claude Code transcript."""
    users: list[str] = []
    turns: list[str] = []
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return [], ""
    with handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("message"), dict) or rec.get("isSidechain"):
                continue
            if rec.get("type") == "user":
                text = _prompt_text(rec)
                if text:
                    users.append(text)
                    turns.append("USER: " + text[:1500])
            elif rec.get("type") == "assistant":
                for block in rec["message"].get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                        turns.append("ASSISTANT: " + " ".join(block["text"].split())[:800])
    digest = scrub("\n".join(turns))[-MAX_DIGEST_CHARS:]
    return users, digest


PROMPT = """You extract durable project knowledge from a coding conversation.
Return ONLY a JSON array (no prose). Each item: {"type": one of decision|fact|procedure|preference|failure,
"text": "one-line title, then at most two sentences of reason or detail"}.
Include only things a future session would otherwise have to rediscover: decisions with their reason,
non-obvious facts about the project, confirmed fixes, approaches that failed and why, standing preferences.
Skip anything temporary, anything obvious from the code, and anything uncertain. At most 8 items; [] if none.

CONVERSATION:
"""


def by_command(digest: str, command: str, timeout: int = 180) -> list[tuple[str, str]]:
    if not digest.strip():
        return []
    # Windows parses a command string itself; elsewhere it has to be split into arguments first.
    argv = command if os.name == "nt" else shlex.split(command)
    try:
        done = subprocess.run(argv, input=PROMPT + digest, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"could not run MOS_EXTRACT_CMD: {exc}") from None
    match = re.search(r"\[.*\]", done.stdout or "", re.DOTALL)
    if not match:
        raise ValueError("the extract command did not print a JSON list")
    items = json.loads(match.group(0))
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
            kind = item.get("type") if item.get("type") in TYPES else "fact"
            out.append((kind, scrub(item["text"].strip())[:600]))
    return out[:MAX_CANDIDATES]


def extract(transcript: Path | str, store: Store, use_command: bool = False) -> list:
    """Queue candidates from a transcript into the inbox and return the ones that were new."""
    users, digest = read_dialogue(transcript)
    command = os.environ.get("MOS_EXTRACT_CMD", "").strip()
    if use_command:
        if not command:
            raise ValueError("set MOS_EXTRACT_CMD first, for example: ollama run llama3.2")
        pairs = by_command(digest, command)
        source = f"extract-command:{Path(transcript).stem}"
    else:
        pairs = heuristic(users)
        source = f"extract-heuristic:{Path(transcript).stem}"
    added = [inbox.add(store, text, type=kind, source=source) for kind, text in pairs]
    return [m for m in added if m]
