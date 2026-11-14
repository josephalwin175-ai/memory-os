"""Live-session helpers built on the Claude Code transcript file.

Two jobs, both free of model calls:

* nudge: notice when a session's context has grown large, or when it is being
  resumed after a long break, and suggest a handoff to a fresh session.
* capture: at session end or before compaction, record what was being worked
  on so the next session's brief can pick it up.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .store import Memory, Store

DEFAULT_ROTATE_AT = 100_000
COLD_GAP_SECONDS = 3600
COLD_MIN_CONTEXT = 50_000
LAST_SESSION_ID = "last-session"
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b(\s*[:=]\s*)\S+"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
]


def scrub(text: str) -> str:
    """Redact things that look like credentials before anything is stored."""
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _context(usage: dict) -> int:
    return sum(int(usage.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def last_call(path: Path | str) -> dict | None:
    """Context size and time of the most recent API call, read from the file's tail."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    for window in (400_000, 4_000_000, size):
        try:
            with open(path, "rb") as handle:
                handle.seek(max(size - window, 0))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if rec.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                if rec.get("isSidechain"):
                    continue
                return {"context": _context(msg["usage"]), "ts": _ts(rec.get("timestamp"))}
        if window >= size:
            break
    return None


def _state_path(session_id: str) -> Path:
    home = Path(os.environ.get("MOS_HOME") or Path.home() / ".memory-os")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")[:80]
    return home / "state" / f"{safe}.json"


def _k(tokens: int) -> str:
    return f"{round(tokens / 1000)}K"


def nudge(payload: dict, now: datetime | None = None) -> dict | None:
    """Return hook output when it is worth suggesting a fresh session, else None.

    Fires once each time context crosses the threshold and each doubling after
    it, and once per long break, so it never repeats on every prompt.
    """
    try:
        base = int(os.environ.get("MOS_ROTATE_AT", DEFAULT_ROTATE_AT))
    except ValueError:
        base = DEFAULT_ROTATE_AT
    transcript = payload.get("transcript_path")
    if base <= 0 or not transcript:
        return None
    call = last_call(transcript)
    if not call:
        return None
    ctx = call["context"]
    state_file = _state_path(str(payload.get("session_id") or Path(transcript).stem))
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    level = 0 if ctx < base else int(math.log2(ctx / base)) + 1
    now = now or datetime.now(timezone.utc)
    stamp = call["ts"].isoformat() if call["ts"] else ""
    gap = (now - call["ts"]).total_seconds() if call["ts"] else 0

    user_msg = model_msg = None
    if gap > COLD_GAP_SECONDS and ctx >= COLD_MIN_CONTEXT and state.get("cold") != stamp:
        state["cold"] = stamp
        state["level"] = max(level, state.get("level", 0))
        hours = gap / 3600
        user_msg = (f"MEMORY OS: resuming after {hours:.0f}h with about {_k(ctx)} tokens of context. The prompt cache "
                    f"has likely expired, so this turn re-sends all of it at full price. A fresh session would start small.")
        model_msg = (f"MEMORY OS: this session is being resumed after a {hours:.0f}h break with about {_k(ctx)} tokens "
                     f"of context, and the provider cache has likely expired. Before doing more work, offer the user a "
                     f"handoff (the handoff skill) so they can continue in a fresh session. Offer once; if they prefer "
                     f"to stay, carry on.")
    elif level > state.get("level", 0):
        state["level"] = level
        user_msg = (f"MEMORY OS: this session's context is about {_k(ctx)} tokens, and every turn re-sends it. "
                    f"Consider a handoff and a fresh session.")
        model_msg = (f"MEMORY OS: this session's context is about {_k(ctx)} tokens, so each turn now re-sends that "
                     f"much. At the next natural stopping point, offer the user a handoff (the handoff skill) to "
                     f"continue in a fresh session. Mention it once and do not interrupt the current task.")
    if not user_msg:
        return None
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return {
        "systemMessage": user_msg,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": model_msg},
    }


def _prompt_text(rec: dict) -> str:
    if rec.get("isMeta") or rec.get("isSidechain"):
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        text = " ".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    text = " ".join(text.split())
    # Lines starting with "<" are harness wrappers (reminders, command output), not the user's words.
    return "" if not text or text.startswith("<") else text


def summarize(path: Path | str, root: Path) -> dict:
    """What the session was about: last few requests, files changed, final context size."""
    prompts: list[str] = []
    files: dict[str, int] = {}
    order = 0
    context = 0
    root = root.resolve()
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {"prompts": [], "files": [], "context": 0}
    with handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("message"), dict):
                continue
            if rec.get("type") == "user":
                text = _prompt_text(rec)
                if text:
                    prompts.append(text)
            elif rec.get("type") == "assistant" and not rec.get("isSidechain"):
                msg = rec["message"]
                if isinstance(msg.get("usage"), dict):
                    context = _context(msg["usage"])
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                        raw = (block.get("input") or {}).get("file_path")
                        if not raw:
                            continue
                        try:
                            shown = Path(raw).resolve().relative_to(root).as_posix()
                        except (ValueError, OSError):
                            continue  # outside the project: not this project's business
                        order += 1
                        files[shown] = order
    recent_files = [name for name, _ in sorted(files.items(), key=lambda kv: -kv[1])[:8]]
    snippets = [scrub(p)[:160] + ("..." if len(p) > 160 else "") for p in prompts[-3:]]
    return {"prompts": snippets, "files": recent_files, "context": context}


def capture(payload: dict) -> Memory | None:
    """Write the auto-captured `last-session` memory. Only in projects that already use `.memory/`."""
    transcript = payload.get("transcript_path")
    store = Store(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if not transcript or not store.exists():
        return None
    summary = summarize(transcript, store.root)
    if not summary["prompts"] and not summary["files"]:
        return None
    today = date.today().isoformat()
    lines = [f"Last session, {today} (auto-captured from the transcript, not verified)"]
    if summary["prompts"]:
        lines.append("Last requests, quoted: " + " | ".join(f'"{p}"' for p in summary["prompts"]))
    if summary["files"]:
        lines.append("Files changed: " + ", ".join(summary["files"]))
    if summary["context"]:
        lines.append(f"Context at the end: about {_k(summary['context'])} tokens.")
    mem = Memory(id=LAST_SESSION_ID, text="\n".join(lines), type="status", trust="agent",
                 created=today, verified=today, source=f"transcript:{Path(transcript).stem}")
    store.write(mem)
    # Per-machine state: keep it out of the shared, reviewed memories.
    store.ignore(f"{LAST_SESSION_ID}.md")
    return mem
